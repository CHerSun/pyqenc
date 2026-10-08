"""
Optimization phase for the quality-based encoding pipeline.

This module handles optimal strategy selection by testing representative chunks
with all strategies and comparing file sizes.

Two modes are supported:

* **All-strategies mode** (``config.optimize=False``): returns all configured
  strategies immediately without running any test encodes and without emitting
  any log messages.
* **Optimization mode** (``config.optimize=True``): runs test encodes on
  representative chunks, persists per-strategy results to ``optimization.yaml``,
  and selects strategies within the configured tolerance of the best result.
  The ledger counts winning ATTEMPTS — one ``Artifact[EncodedChunk]`` row per
  (test chunk, strategy) pair — not per-strategy records.
"""
# CHerSun 2026

import asyncio
import logging
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

import yaml
from pydantic import BaseModel, Field, TypeAdapter

from pyqenc.constants import (
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
    WARNING_SYMBOL,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import (
    CleanupLevel,
    CropParams,
    EncodingPlan,
    Fingerprint,
    PhaseOutcome,
    QualityTarget,
    Strategy,
    id_set_fingerprint,
    identity_changed,
    targets_as_strings,
)
from pyqenc.phase import (
    Artifact,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.chunking import ChunkingPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.probe import ProbePhase
from pyqenc.state import ArtifactState, ProbeFacet
from pyqenc.stream_model import DecimalYaml, EncodedChunk, VideoStreamChunk
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.log_format import fmt_size_mb
from pyqenc.utils.visualization import QualityEvaluator
from pyqenc.utils.yaml_utils import save_model

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector
    from pyqenc.phases.encoding import ChunkEncoder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# optimization.yaml — per-mode discriminated-union sidecars (Req 18-21, 61)
# ---------------------------------------------------------------------------

class StrategySummaryRow(BaseModel):
    """One per-strategy row of the ``summary`` replay table.

    Attributes:
        strategy:   Display name of the tested strategy.
        total_size: Total encoded size across the test chunks (bytes).
        metrics:    Min-across-test-chunks value for every measured
                    ``(metric, statistic)`` key — the fixed-mode dominance and
                    anchor inputs. Empty when nothing was measured.
    """

    strategy:   str
    total_size: int
    metrics:    dict[str, float] = Field(default_factory=dict)


class _OptimizationSidecarBase(BaseModel):
    """The common keys of both ``optimization.yaml`` variants.

    Fields are named for WHAT they identify (Req 61): ``source`` the source
    content identity, ``chunks`` the chunk-set fingerprint (a re-chunk wipes
    winners), ``strategies`` each strategy's resolved-args fingerprint,
    ``probe`` the structured facet, ``sampling`` the measurement factor.
    Every absent key is unknown, never a mismatch (Req 32). ``summary`` is
    the replay aggregate (freshness guaranteed by the pending gate), and
    ``test_chunks`` the persisted selection basis (checked for full-set
    presence at read — Req 44).
    """

    source:      Fingerprint | None            = None
    chunks:      Fingerprint | None            = None
    strategies:  dict[str, Fingerprint]        = Field(default_factory=dict)
    probe:       ProbeFacet | None             = None
    sampling:    int | None                    = None
    test_chunks: list[str]                     = Field(default_factory=list)
    summary:     list[StrategySummaryRow]      = Field(default_factory=list)

    def save(self, path: Path) -> None:
        """Write this sidecar to *path* atomically."""
        save_model(path, self)


class SearchOptimizationSidecar(_OptimizationSidecarBase):
    """The search variant: the quality-target set is the mode key."""

    mode:    Literal["search"] = "search"
    targets: list[str]         = Field(default_factory=list)


class FixedOptimizationSidecar(_OptimizationSidecarBase):
    """The fixed variant: the per-strategy pinned-quality map is the mode key.

    Keyed by strategy display name, valued by the strategy's quantized pinned
    quality — an equal map performs no invalidation (the pending gate alone
    decides reuse or resume, Req 40).
    """

    mode:   Literal["fixed"]        = "fixed"
    pinned: dict[str, DecimalYaml]  = Field(default_factory=dict)


OptimizationSidecar = SearchOptimizationSidecar | FixedOptimizationSidecar
"""The per-mode union; loading dispatches on the ``mode`` tag (never try-both)."""

_OPTIMIZATION_ADAPTER: TypeAdapter[OptimizationSidecar] = TypeAdapter(
    Annotated[OptimizationSidecar, Field(discriminator="mode")],
)


def load_optimization_sidecar(path: Path) -> OptimizationSidecar | None:
    """Load ``optimization.yaml`` dispatching on the persisted mode tag.

    Args:
        path: The ``optimization.yaml`` path.

    Returns:
        The tagged sidecar, or ``None`` when absent or unparseable.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return _OPTIMIZATION_ADAPTER.validate_python(data or {})
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("Could not load %s: %s", path, exc)
        return None


def save_optimization_sidecar(path: Path, sidecar: OptimizationSidecar) -> None:
    """Persist a tagged optimization sidecar atomically."""
    save_model(path, sidecar)


def current_optimization_sidecar(
    plan:        EncodingPlan,
    source:      Fingerprint,
    chunks:      Fingerprint,
    probe:       ProbeFacet,
    sampling:    int,
    test_chunks: list[str],
    summary:     list[StrategySummaryRow],
) -> OptimizationSidecar:
    """Build the sidecar describing the CURRENT run's inputs (union by mode).

    The single composition site both the invalidation comparisons and the
    post-success saves build on — the persisted file and the live inputs can
    never diverge in shape.
    """
    if plan.fixed_quality:
        return FixedOptimizationSidecar(
            source      = source,
            chunks      = chunks,
            strategies  = {s.display_name(): s.fingerprint for s in plan.strategies},
            probe       = probe,
            sampling    = sampling,
            test_chunks = test_chunks,
            summary     = summary,
            pinned = {
                s.display_name(): s.codec.quality_better.quantize(
                    s.codec.quality_granularity,
                )
                for s in plan.strategies
            },
        )
    return SearchOptimizationSidecar(
        source      = source,
        chunks      = chunks,
        strategies  = {s.display_name(): s.fingerprint for s in plan.strategies},
        probe       = probe,
        sampling    = sampling,
        test_chunks = test_chunks,
        summary     = summary,
        targets     = targets_as_strings(plan.targets),
    )


_FIXED_COMPARISON_STATS: tuple[str, ...] = ("p10", "median")
"""The fixed-mode comparison statistic set per metric.

The single definition of what fixed-mode comparison consumes: the anchor's
synthetic target set (the ruler), the dominance pruning input, and the
comparison-table columns. ``p10`` guards the worst decile, ``median`` the
central tendency; the remaining stats are stability/shape indicators
(``std``, ``min``, ``max``, other percentiles), not quality bars — comparing
or ruling on them silently skews selection. The full measured set stays
aggregated in ``StrategySummaryRow.metrics`` / sidecars (data retention), so
re-selecting the comparison set never re-measures.
"""


def _comparison_metrics(metrics: dict[str, float]) -> dict[str, float]:
    """Project measured metrics onto the fixed-mode comparison stat set.

    Args:
        metrics: ``{metric_statistic: value}`` — the aggregated measurement.

    Returns:
        The subset restricted to :data:`_FIXED_COMPARISON_STATS` statistics.
    """
    return {
        key: value for key, value in metrics.items()
        if key.rsplit("_", 1)[1] in _FIXED_COMPARISON_STATS
    }



# ---------------------------------------------------------------------------
# OptimizationPhaseResult
# ---------------------------------------------------------------------------

@dataclass
class OptimizationPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying optimization-specific payload.

    Attributes:
        winners: The winning test attempts — one ``Artifact[EncodedChunk]``
                 per (test chunk, strategy) pair. The single sanctioned
                 exception to the consumption-graph rule: nothing
                 downstream consumes them, but carrying the winners keeps
                 this phase structurally identical to EncodingPhase (same
                 result shape, same sort-into-fields step) so the base run
                 mechanics apply with zero special cases. The aggregated
                 per-strategy records stay in ``optimization.yaml``.
        selected_strategies: The settings subset — strategies selected as
                             optimal (or all strategies in all-strategies
                             mode); consumed by Encoding and Merge.
        synthetic_targets: The fixed-mode presentation ruler — the anchor's
                           min-aggregated metrics as quality targets. Empty
                           in searched runs and uncompared fixed runs;
                           presentation data only, never selection data.
        anchor:           The fixed-mode anchor's display name (empty in
                           searched and uncompared runs). Consumed by merge
                           as the fixed-run invalidation-key basis.
    """

    winners:             list[Artifact[EncodedChunk]] = field(default_factory=list)
    selected_strategies: list[Strategy]              = field(default_factory=list)
    synthetic_targets:   list[QualityTarget]         = field(default_factory=list)
    anchor:              str | None                  = None


# ---------------------------------------------------------------------------
# OptimizationPhase
# ---------------------------------------------------------------------------

class OptimizationPhase(Phase[OptimizationPhaseResult]):
    """Phase object for strategy optimization.

    In **all-strategies mode** (``config.optimize=False`` or a single
    configured strategy), skips test encodes entirely (no banner): resolves
    dependencies, performs the ``optimization.yaml`` target bookkeeping, and
    returns all configured strategies as selected.

    In **optimization mode** (``config.optimize=True``), runs test encodes on
    representative chunks, persists per-strategy results to
    ``optimization.yaml``, and selects strategies within the configured
    tolerance of the best result. Optimization IS encoding on a chunk subset
    and follows the encoding contract: its test encodes are timed under
    ``optimization.<strategy>`` / ``optimization.quality_measure`` (via the
    shared ``ChunkEncoder`` metric prefix) and receive the run's cleanup
    level for rolling attempt cleanup.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "optimization"
    SIDECAR_NAME = "optimization.yaml"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ProbePhase, ChunkingPhase)
    _METRIC_KEY: MetricKey = MetricKey.OPTIMIZATION

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        # Recovery stash — resolved during _recover(), consumed by
        # _execute()/_reused_result()/_make_result().
        self._strategies_to_test: list[Strategy]                   = []
        self._test_chunks:       list[VideoStreamChunk]            = []
        self._selected_names:    list[str]                         = []
        self._strategy_results:  list[StrategySummaryRow]          = []
        self._current_probe:     ProbeFacet | None                 = None
        self._chunks_fingerprint: Fingerprint | None               = None
        """The current chunk-set fingerprint (the chunks key's live side)."""
        self._anchor_name:       str | None                        = None
        """The fixed-mode measurement anchor's display name (fixed compared
        runs only; ``None`` in searched and uncompared runs)."""
        self._synthetic_targets: list[QualityTarget]               = []
        """The anchor-derived presentation ruler (fixed compared runs only)."""

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _skip_check(self, dry_run: bool) -> OptimizationPhaseResult | None:
        """Skip decision plus the fixed-mode banner.

        Dependencies are already resolved when this runs (the template
        resolves them before the skip check). The fixed-mode entry is
        PRESENTATION only now: the cleanup guard moved to the run boundary
        (plan-boundary construction validation, Req 55) and the unconditional
        winner wipe is replaced by the pinned-quality map key (an equal map
        performs no invalidation — the pending gate decides, Req 40/O-2).

        Args:
            dry_run: Unused (the banner is presentation).

        Returns:
            The all-strategies result, or ``None`` to proceed with the template.
        """
        plan = self._deps[ProbePhase].plan

        strategies = plan.strategies

        if plan.fixed_quality:
            self._log_fixed_quality_banner(strategies)

        # All-strategies mode: triggered by the optimize flag being off or a
        # single strategy given (nothing to optimize against).
        if self._config.encoding.optimize and len(strategies) > 1:
            return None

        return self._all_strategies(dry_run)

    def _log_fixed_quality_banner(self, strategies: list[Strategy]) -> None:
        """The fixed-mode WARNING banner (Req 5).

        One prominent block per fixed run on both optimization paths,
        stating the guarantees: search disabled, sizes compared at
        *nominally* equal knob (scales not comparable across encoder
        families), merged-output measurement as the final check.
        """
        logger.warning("")
        logger.warning(
            "%s FIXED QUALITY MODE — knob pinned (%s)",
            WARNING_SYMBOL, self._pinned_knob_description(strategies),
        )
        logger.warning(
            "  - per-chunk quality search disabled: every chunk encodes once at the pinned value"
        )
        logger.warning(
            "  - strategy sizes are compared at *nominally* equal knob; knob scales "
            "are NOT comparable across encoder families (h264 CRF ≠ h265 CRF ≠ AV1 CRF)"
        )
        if len(strategies) > 1:
            logger.warning(
                "  - every surviving strategy will fully encode the video"
            )
        logger.warning(
            "  - merged-output measurement remains the final quality check"
        )
        logger.warning("")

    @staticmethod
    def _pinned_knob_description(strategies: list[Strategy]) -> str:
        """The pinned knob as ``"CRF=18"``, or a per-strategy listing.

        Uniform label and value collapse to the compact form; anything else
        (possible without ``-q`` via collapsed config profiles of different
        codec families) lists each strategy's own pinned knob.
        """
        labels = {s.codec.quality_label for s in strategies}
        values = {s.codec.quality_better for s in strategies}
        if len(labels) == 1 and len(values) == 1:
            return f"{labels.pop()}={values.pop()}"
        return ", ".join(
            f"{s.display_name()}: {s.codec.quality_label}={s.codec.quality_better}"
            for s in strategies
        )

    def _log_key_params(self) -> None:
        """Log the strategy list and tolerance (key parameters)."""
        plan = self._deps[ProbePhase].plan

        logger.info("Strategies:  %s", ", ".join(s.display_name() for s in plan.strategies))
        logger.info("Tolerance:   %.1f%%", self._config.encoding.optimize_tolerance)

    def _recovery_unit(self) -> str:
        """The recovery summary counts winning attempts (one per pair)."""
        return "attempt"

    def _recover(self) -> Recovery:
        """Resolve optimization state currency: invalidations first, then the ledger.

        Steps:

        1. Identity key (Req 33): a persisted identity contradicting the live
           source is catastrophic for the whole shared namespace — fatal
           without the ``--force`` permission; with it, wipe attempts,
           winners, and the sidecar. An absent key (legacy sidecar) is
           unknown, never a mismatch.
        2. Probe mismatch against ``optimization.yaml`` — fatal without
           ``--force``.
        3. Quality-target / metrics-sampling change → wipe ``encoded/``
           result dirs and treat all cached strategy results as stale.
        4. Missing sidecar with winners present → wipe ``encoded/``: no file
           proves which parameters produced those winners, so they re-derive
           from the attempt workspace (near-free replay via attempt
           cache-hits).
        5. The per-pair ledger (one ``Artifact[EncodedChunk]`` row per
           (test chunk, strategy) winning attempt, presence-based). The
           to-test set is its projection: any strategy with a non-``COMPLETE``
           pair has work pending; complete pairs are the reuse substrate.
        6. Nothing to test and a persisted table covering the plan → the
           fast exit seeds the display from the persisted rows (plan-scoped)
           and computes the selection live.

        Returns:
            The :class:`Recovery` single source of truth.

        Raises:
            RecoveryError: On an identity or probe change without ``--force``,
                or when ChunkingPhase produced no chunks.
        """
        job_result   = self._deps[JobPhase]
        work_dir     = job_result.work_dir
        tolerance    = self._config.encoding.optimize_tolerance
        plan         = self._deps[ProbePhase].plan

        strategies   = plan.strategies

        # Steps 1-5 — the shared-namespace invalidation ladder (one helper
        # with the all-strategies path; also stashes the live facet and the
        # chunk-set fingerprint for the execute-path saves).
        persisted = self._invalidate_shared_namespace(work_dir, plan)

        # Step 6 — the per-pair ledger. The to-test set is a projection of
        # the ledger (presence-based, both modes): a strategy with any
        # non-COMPLETE (test chunk, strategy) pair re-tests. Re-test cost is
        # bounded by the attempt workspace — per-attempt cache-hits in
        # encoding/ make an unchanged-parameters replay near-free.
        self._test_chunks = self._resolve_test_chunks(persisted)
        rows = self._pair_ledger(work_dir, strategies)

        incomplete: set[str] = set()
        for row in rows:
            if isinstance(row.payload, EncodedChunk) and row.state != ArtifactState.COMPLETE:
                incomplete.add(row.payload.strategy.display_name())
        self._strategies_to_test = [
            s for s in strategies if s.display_name() in incomplete
        ]

        # Step 7 — the no-pending fast exit: the single sanctioned reader of
        # the persisted rows. The display seeds plan-scoped from the table;
        # the selection is computed LIVE (current tolerance / dominance) and
        # so can never be stale.
        plan_names   = [s.display_name() for s in strategies]
        table_covers = persisted is not None and (
            {r.strategy for r in persisted.summary} >= set(plan_names)
        )
        if not self._strategies_to_test and table_covers:
            assert persisted is not None
            table_rows = [
                r for r in persisted.summary if r.strategy in set(plan_names)
            ]
            self._strategy_results = table_rows
            if plan.fixed_quality:
                # Re-select by pruning (tolerance is void in fixed mode) and
                # re-derive the anchor ruler from the persisted measurements.
                self._selected_names = self._dominance_survivors(table_rows)
                self._anchor_name = self._select_anchor(
                    table_rows, plan_names, self._selected_names,
                )
                anchor_result = next(
                    (r for r in table_rows if r.strategy == self._anchor_name),
                    None,
                )
                self._synthetic_targets = self._synthetic_targets_from(anchor_result)
            else:
                self._selected_names = self._apply_tolerance(
                    sorted(table_rows, key=lambda r: r.total_size), tolerance,
                )

        # Searched: fatal only when work is actually pending. Fixed:
        # _recover only runs for compared runs (single-strategy and
        # optimize-off take the all-strategies skip path) — a compared run
        # without test chunks cannot prune and always fails loudly.
        if not self._test_chunks and (self._strategies_to_test or plan.fixed_quality):
            raise RecoveryError("No chunks available from ChunkingPhase")

        if not self._strategies_to_test and not table_covers:
            # Every pair is COMPLETE but the persisted table does not cover
            # the plan (sidecar absent, or rows missing for current
            # strategies) — derive sizes live and persist a fresh table.
            # Cheap: no encodes, the winners are already on disk. Settings
            # staleness, not artifact presence: pending is set explicitly
            # (the ledger alone would read current).
            return Recovery(artifacts=rows, pending=True)
        return Recovery.from_artifacts(rows)

    def _invalidate_shared_namespace(
        self,
        work_dir: Path,
        plan:     EncodingPlan,
    ) -> OptimizationSidecar | None:
        """The full shared-namespace invalidation ladder (Req 38-44).

        Optimization owns EVERY key-based invalidation over the shared
        attempt/winner namespace; both this phase's optimize path and the
        all-strategies skip path run this same ladder. Every effect is a
        disk effect (a wipe and/or a sidecar rewrite); nothing is cleared in
        memory. Order: identity → probe facet (catastrophic, whole
        namespace) → strategy-args fingerprints (catastrophic, per
        strategy) → winner-band keys (automatic: mode, chunk-set,
        mode-key, sampling) → missing sidecar (§99 conservative).

        Requires ``self._current_probe`` / ``self._chunks_fingerprint`` to
        be stashed first.

        Args:
            work_dir: The run's work dir.
            plan:     The run's resolved encoding plan.

        Returns:
            The surviving persisted sidecar (``None`` when invalidated or
            absent) — the caller's classification substrate.

        Raises:
            RecoveryError: On a fatal-band condition without ``--force``.
        """
        job_result = self._deps[JobPhase]
        opt_yaml   = work_dir / OptimizationPhase.SIDECAR_NAME
        strategies = plan.strategies
        self._chunks_fingerprint = id_set_fingerprint(
            c.safe_name() for c in (a.payload for a in self._deps[ChunkingPhase].chunks)
        )
        self._current_probe = ProbeFacet.from_probe(self._deps[ProbePhase])
        current_probe = self._current_probe
        chunks_fps    = self._chunks_fingerprint
        assert current_probe is not None and chunks_fps is not None
        current    = current_optimization_sidecar(
            plan        = plan,
            source      = job_result.source_fingerprint,
            chunks      = chunks_fps,
            probe       = current_probe,
            sampling    = self._config.measurement.sampling,
            test_chunks = [],   # the selection resolves after invalidation
            summary     = [],
        )

        # Step 1 — identity key: catastrophic for the shared namespace.
        persisted: OptimizationSidecar | None = load_optimization_sidecar(opt_yaml)
        if identity_changed(persisted.source if persisted is not None else None,
                            job_result.source_fingerprint):
            if not job_result.force:
                raise RecoveryError(
                    "Source content identity mismatch (optimization.yaml) — the "
                    "shared attempt/winner namespace belongs to a different source.  "
                    "Re-run with --force to grant permission to wipe attempts, "
                    "winners, and the sidecar, and re-derive from the new source."
                )
            logger.warning(
                "Source identity mismatch (--force granted — wiping the shared "
                "attempt/winner namespace and optimization.yaml)"
            )
            self._wipe_artifacts(work_dir, strategies)
            persisted = None

        # Step 2 — probe facet: catastrophic for the whole shared namespace
        # (explicit field-wise comparison — Req 23; human fields never leak).
        if (
            persisted is not None
            and persisted.probe is not None
            and not persisted.probe.matches(current_probe)
        ):
            if not job_result.force:
                raise RecoveryError(
                    "Probe params changed since the last optimization run "
                    f"(persisted facet={persisted.probe}, "
                    f"current={current_probe}) — every attempt's pixels "
                    "changed.  Re-run with --force to grant permission to wipe "
                    "the shared namespace and re-test."
                )
            logger.warning(
                "Probe facet changed (--force granted — wiping the shared "
                "attempt/winner namespace and optimization.yaml)"
            )
            self._wipe_artifacts(work_dir, strategies)
            persisted = None

        # Step 3 — strategy-args fingerprints: catastrophic PER STRATEGY
        # (a changed codec's attempts are stale products; unchanged
        # strategies keep theirs — Req 39).
        if persisted is not None:
            changed_strategies = [
                name for name, fp in current.strategies.items()
                if name in persisted.strategies
                and not persisted.strategies[name].matches(fp)
            ]
            if changed_strategies:
                if not job_result.force:
                    raise RecoveryError(
                        "Strategy arguments changed for: "
                        f"{', '.join(sorted(changed_strategies))} — their attempts "
                        "are stale products of the old codec configuration.  "
                        "Re-run with --force to grant permission to wipe those "
                        "strategies' attempts and winners."
                    )
                for name in sorted(changed_strategies):
                    logger.warning(
                        "Strategy args changed (--force granted — wiping %s's "
                        "attempts and winners)", name,
                    )
                self._wipe_strategies(work_dir, plan, changed_strategies)
                save_optimization_sidecar(opt_yaml, current)
                persisted = None

        # Step 4 — winner-band keys: automatic wipe-winners + sidecar rewrite
        # (a disk effect — no in-memory table clearing, Req 37-42). Mode,
        # chunk-set, mode-key (targets / pinned map), and sampling all fire
        # here; an equal fixed map performs NO invalidation (O-2) — the
        # pending gate alone decides.
        if persisted is not None:
            reasons = self._winner_band_reasons(persisted, current)
            if reasons:
                for reason in reasons:
                    logger.info(
                        "%s — wiping winners (they re-derive from the attempt "
                        "workspace); optimization.yaml rewritten", reason,
                    )
                _wipe_encoded_dir(work_dir, strategies)
                save_optimization_sidecar(opt_yaml, current)
                persisted = None

        # Step 5 — missing sidecar with winners present: unknown currency.
        # Nothing proves which parameters produced the winners, so they
        # are conservatively invalidated and re-derived from attempts (§99).
        if (
            persisted is None
            and (work_dir / ENCODED_OUTPUT_DIR).exists()
        ):
            logger.info(
                "optimization.yaml missing — winner currency unknown; "
                "wiping encoded/ (winners re-derive from the attempt workspace)"
            )
            _wipe_encoded_dir(work_dir, strategies)

        return persisted

    @staticmethod
    def _winner_band_reasons(
        persisted: OptimizationSidecar,
        current:   OptimizationSidecar,
    ) -> list[str]:
        """Why winners must be wiped (the automatic band's condition list).

        Every absent key is unknown, never a mismatch (Req 32). An equal
        fixed pinned map contributes nothing (O-2).
        """
        reasons: list[str] = []
        if persisted.mode != current.mode:
            reasons.append(
                f"mode changed ({persisted.mode} → {current.mode})"
            )
        if (
            persisted.chunks is not None
            and current.chunks is not None
            and not persisted.chunks.matches(current.chunks)
        ):
            reasons.append("chunk set changed (re-chunk)")
        if (
            persisted.mode == "search"
            and current.mode == "search"
            and persisted.targets
            and persisted.targets != current.targets
        ):
            reasons.append("quality targets changed")
        if (
            persisted.mode == "fixed"
            and current.mode == "fixed"
            and persisted.pinned != current.pinned
        ):
            reasons.append("pinned quality map changed")
        if (
            persisted.sampling is not None
            and persisted.sampling != current.sampling
        ):
            reasons.append(
                f"metrics sampling changed ({persisted.sampling} → {current.sampling})"
            )
        return reasons

    @staticmethod
    def _wipe_strategies(work_dir: Path, plan: EncodingPlan, names: list[str]) -> None:
        """Wipe the changed strategies' attempts and winners (permission band).

        Only the changed strategies' trees go — unchanged strategies keep
        their products (Req 39).
        """
        for strategy in plan.strategies:
            if strategy.display_name() not in names:
                continue
            for tree in (ENCODED_OUTPUT_DIR, ENCODING_WORKSPACE_DIR):
                shutil.rmtree(work_dir / tree / strategy.safe_name(), ignore_errors=True)

    def _resolve_test_chunks(self, persisted: OptimizationSidecar | None) -> list[VideoStreamChunk]:
        """The test-chunk set: the persisted selection reused only when the
        FULL set survives in the current chunking output (Req 44/O-6) —
        partial survival triggers a fresh full pick with a log line, never a
        silently shrunk test basis. The fresh selection is stashed here
        (recovery) so ``_execute`` uses the same set that produced the
        ledger counts.
        """
        chunks: list[VideoStreamChunk] = [
            a.payload for a in self._deps[ChunkingPhase].chunks
        ]

        test_ids = persisted.test_chunks if persisted is not None and persisted.test_chunks else []
        if test_ids:
            by_id = {c.safe_name(): c for c in chunks}
            if set(test_ids) <= set(by_id):
                return [by_id[i] for i in test_ids]
            logger.info(
                "Persisted test-chunk selection no longer fully present "
                "(re-chunk) — re-selecting a fresh full test basis"
            )
        return _select_test_chunks(chunks)

    def _pair_ledger(self, work_dir: Path, strategies: list[Strategy]) -> list[Artifact]:
        """The per-pair ledger plus orphaned-strategy rows."""
        from pyqenc.phases.encoding import _orphan_strategy_rows, _pair_rows

        rows: list[Artifact] = _pair_rows(work_dir, self._test_chunks, strategies)
        rows += _orphan_strategy_rows(work_dir, strategies)
        return rows

    def _execute(
        self,
        wanted:  list[Artifact],
        dry_run: bool,
    ) -> OptimizationPhaseResult:
        """Run test encodes for pending strategies, then derive and select.

        The top-level ``optimization`` span belongs to the template and covers
        everything here. Test encodes run through the shared encoder machinery
        with ``metric_prefix=optimization``, so their dotted keys land under
        ``optimization.<strategy>`` / ``optimization.quality_measure``.
        ``dry_run`` is never ``True`` here (optimization is not a
        readonly-execute phase; the template previews instead).

        The persisted table is WRITE-ONLY here: per-strategy sizes derive
        from the live pair ledger (the winners on disk), plan-scoped by
        construction — rows for strategies no longer configured can never
        reach selection or the saved sidecar. The sidecar is saved exactly
        once, after successful processing.

        Args:
            wanted:  The wanted artifact list from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``OptimizationPhaseResult`` with ``selected_strategies`` set.
        """
        job_result = self._deps[JobPhase]
        work_dir   = job_result.work_dir
        opt_yaml   = work_dir / OptimizationPhase.SIDECAR_NAME
        tolerance  = self._config.encoding.optimize_tolerance
        plan       = self._deps[ProbePhase].plan

        fixed      = plan.fixed_quality
        assert self._current_probe is not None, "_recover populates the probe state before execution"
        assert self._chunks_fingerprint is not None, "_recover populates the chunk-set key"
        crop       = self._current_probe.crop

        current_sampling = self._config.measurement.sampling

        from pyqenc.phases.encoding import (
            EncodingResult,
            _encode_chunks_parallel,
            _recover_encoding_attempts,
        )  # deferred: circular import (encoding <-> optimization)

        strategies_to_test = self._strategies_to_test

        # The test-chunk set was resolved by recovery (persisted selection or
        # the fresh pick that produced the ledger counts).
        test_chunks = self._test_chunks
        if strategies_to_test and not test_chunks:
            err = "No chunks available from ChunkingPhase"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        # Run test encodes for all pending strategies in parallel (unified
        # pool; the top-level span belongs to the template). Skipped entirely
        # on a derive-only run (all pairs complete, table not covering the
        # plan) — there is nothing to encode.
        enc_result: EncodingResult | None = None
        if strategies_to_test:
            encoder = _make_encoder(
                work_dir         = work_dir,
                collector        = self._collector,
                crop_params      = crop,
                visual_hash      = self._config.encoding.visual_hash,
                metrics_sampling = self._config.measurement.sampling,
                cleanup_level    = job_result.cleanup,
                metric_prefix    = MetricKey.OPTIMIZATION,
            )
            test_chunk_seconds = sum(c.end_timestamp - c.start_timestamp for c in test_chunks)
            total_seconds      = test_chunk_seconds * len(strategies_to_test)
            total_count        = len(test_chunks) * len(strategies_to_test)

            test_chunk_ids = [c.safe_name() for c in test_chunks]
            phase_recovery = _recover_encoding_attempts(work_dir, test_chunk_ids, strategies_to_test)

            with ProgressBar(total_seconds, title="Optimization", total_count=total_count) as advance:
                # Pre-advance bar for already-complete pairs
                chunks_by_id = {c.safe_name(): c for c in test_chunks}
                for r in phase_recovery.pairs.values():
                    if r.state == ArtifactState.COMPLETE:
                        advance((chunks_by_id[r.chunk_id].end_timestamp - chunks_by_id[r.chunk_id].start_timestamp), AdvanceState.SKIPPED)

                enc_result = asyncio.run(
                    _encode_chunks_parallel(
                        encoder           = encoder,
                        chunks            = test_chunks,
                        strategies        = strategies_to_test,
                        # Fixed mode presents the test encodes no ruler: config
                        # targets drive no verdict there, and the anchor's
                        # synthetic set does not exist until these results do.
                        quality_targets   = [] if fixed else plan.targets,
                        max_parallel      = self._config.encoding.concurrency,
                        force             = False,
                        collector         = self._collector,
                        phase_recovery    = phase_recovery,
                        advance           = advance,
                        metric_prefix     = MetricKey.OPTIMIZATION,
                    )
                )
                advance(0, AdvanceState.COMPLETE)

        # The live ledger after processing — one source for the derived
        # table, the logged summary, and the result rows.
        rows = self._pair_ledger(work_dir, plan.strategies)

        if fixed:
            # Fixed mode derives every strategy fresh from the current disk
            # state (the winners' result sidecars) — sizes from the just-run
            # encode pool, metrics from the sidecars.
            assert enc_result is not None, "fixed compared runs always re-test (the entry wiped the winners)"

            new_results: list[StrategySummaryRow] = []
            for strategy in plan.strategies:
                winners_by_chunk = {
                    w.chunk.safe_name(): w
                    for w in enc_result.encoded_chunks.get(strategy.display_name(), [])
                }
                file_sizes: list[float] = []
                for chunk in test_chunks:
                    encoded = winners_by_chunk.get(chunk.safe_name())
                    if encoded is not None and encoded.stream.stream.file.path.exists():
                        file_sizes.append(encoded.stream.stream.file.file_size_bytes or 0)
                new_results.append(StrategySummaryRow(
                    strategy     = strategy.display_name(),
                    total_size    = int(sum(file_sizes)),
                    metrics      = self._aggregate_strategy_metrics(
                        work_dir, test_chunks, strategy, enc_result.encoded_chunks,
                    ),
                ))

            # Selection = dominance pruning; anchor = smallest survivor (the
            # only front member selectable without a quality opinion);
            # synthetic set = the anchor's min-aggregated metrics. The sidecar
            # persists facts (strategy_results) only — the ruler re-derives on
            # read, so a changed comparison stat set re-projects old
            # measurements correctly.
            resolved_names  = [s.display_name() for s in plan.strategies]
            final_results   = new_results
            selected        = self._dominance_survivors(final_results)
            self._anchor_name = self._select_anchor(final_results, resolved_names, selected)
            anchor_result = next(
                (r for r in final_results if r.strategy == self._anchor_name), None,
            )
            self._synthetic_targets = self._synthetic_targets_from(anchor_result)

            save_optimization_sidecar(
                opt_yaml,
                current_optimization_sidecar(
                    plan        = plan,
                    source      = self._deps[JobPhase].source_fingerprint,
                    chunks      = self._chunks_fingerprint,
                    probe       = self._current_probe,
                    sampling    = current_sampling,
                    test_chunks = [c.safe_name() for c in test_chunks],
                    summary     = final_results,
                ),
            )

            self._selected_names   = selected
            self._strategy_results = final_results

            self._log_fixed_comparison(final_results, selected, self._anchor_name, resolved_names)

            return self._make_result(
                PhaseOutcome.COMPLETED,
                [r for r in rows if r.wanted],
                f"{len(selected)} survivor(s) selected",
            )

        # Searched: per-strategy sizes derive from the live ledger — the
        # winner files of the plan's strategies (never the persisted table).
        sizes: dict[str, int] = {}
        for row in rows:
            if row.state == ArtifactState.COMPLETE and isinstance(row.payload, EncodedChunk):
                name = row.payload.strategy.display_name()
                sizes[name] = sizes.get(name, 0) + (row.payload.stream.stream.file.file_size_bytes or 0)

        final_results = sorted(
            [
                StrategySummaryRow(
                    strategy    = s.display_name(),
                    total_size  = sizes.get(s.display_name(), 0),
                )
                for s in plan.strategies
            ],
            key=lambda r: r.total_size,
        )
        selected = self._apply_tolerance(final_results, tolerance)

        # The single sidecar save — after successful processing.
        save_optimization_sidecar(
            opt_yaml,
            current_optimization_sidecar(
                plan        = plan,
                source      = self._deps[JobPhase].source_fingerprint,
                chunks      = self._chunks_fingerprint,
                probe       = self._current_probe,
                sampling    = current_sampling,
                test_chunks = [c.safe_name() for c in test_chunks],
                summary     = final_results,
            ),
        )

        self._selected_names   = selected
        self._strategy_results = final_results

        self._log_optimization_summary(final_results, selected)

        return self._make_result(
            PhaseOutcome.COMPLETED,
            [r for r in rows if r.wanted],
            f"{len(selected)} strategy(ies) selected",
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> OptimizationPhaseResult:
        """Build the reused result from the fast-exit stash (display + live selection)."""
        plan = self._deps[ProbePhase].plan

        if plan.fixed_quality:
            resolved_names = [s.display_name() for s in plan.strategies]
            self._log_fixed_comparison(
                self._strategy_results, self._selected_names, self._anchor_name, resolved_names,
            )
        else:
            self._log_optimization_summary(self._strategy_results, self._selected_names)
        return self._make_result(
            PhaseOutcome.REUSED, wanted, "all strategy results reused",
        )

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact],
        message:   str,
    ) -> OptimizationPhaseResult:
        """Assemble an ``OptimizationPhaseResult`` from the payload stashes.

        Args:
            outcome:   The phase outcome.
            artifacts: Artifact list (empty on non-execute paths).
            message:   Human-readable summary — on ``FAILED``, the error
                       description.

        Returns:
            The populated result (``selected_strategies`` resolved from the
            live config; ``None``-safe on dep-failure paths where no stash
            exists).
        """
        # Resolve strategy name strings to Strategy objects from the live plan.
        plan = self._deps[ProbePhase].plan

        by_name = {s.display_name(): s for s in plan.strategies}
        unknown = [n for n in self._selected_names if n not in by_name]
        assert not unknown, (
            f"selection escaped the plan: {unknown} not among {sorted(by_name)}"
        )
        selected = [by_name[n] for n in self._selected_names]
        if outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED):
            # Logically impossible by construction (selection derives from
            # plan-scoped live data) — the assert catches runtime bugs at
            # the phase that caused them, not downstream at Encoding.
            assert selected, "successful optimization must select at least one strategy"
        return OptimizationPhaseResult(
            outcome             = outcome,
            message             = message,
            winners             = [
                r for r in artifacts
                if isinstance(r.payload, EncodedChunk) and r.state == ArtifactState.COMPLETE
            ],
            selected_strategies = selected,
            synthetic_targets   = list(self._synthetic_targets),
            anchor              = self._anchor_name,
        )

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def _all_strategies(self, dry_run: bool) -> OptimizationPhaseResult:
        """All-strategies mode: bookkeeping + the skip result (no banner).

        Always writes ``optimization.yaml`` with ``strategy_results=[]`` and
        the current quality targets (when not a dry-run) so that target-change
        detection works on the next run. If quality targets or sampling
        changed since the last run, deletes all result sidecars from
        ``encoded/`` before returning so ``EncodingPhase`` sees ``PARTIAL``
        pairs.

        Args:
            dry_run: When ``True``, skip writing ``optimization.yaml``.

        Returns:
            ``OptimizationPhaseResult`` with all configured strategies selected.
        """
        job_result       = self._deps[JobPhase]
        work_dir         = job_result.work_dir
        plan             = self._deps[ProbePhase].plan

        opt_yaml         = work_dir / OptimizationPhase.SIDECAR_NAME
        current_sampling = self._config.measurement.sampling

        if not dry_run:
            # The full shared-namespace invalidation ladder applies on this
            # path too (Req 38/20): identity, facet, fingerprints, mode,
            # chunk-set, mode-key, sampling — then the sidecar is rewritten
            # with ALL current keys exactly as the optimize paths do.
            self._invalidate_shared_namespace(work_dir, plan)
            assert (
                self._current_probe is not None
                and self._chunks_fingerprint is not None
            ), "the ladder stashes the live facet and chunk-set key"

            work_dir.mkdir(parents=True, exist_ok=True)
            save_optimization_sidecar(
                opt_yaml,
                current_optimization_sidecar(
                    plan        = plan,
                    source      = job_result.source_fingerprint,
                    chunks      = self._chunks_fingerprint,
                    probe       = self._current_probe,
                    sampling    = current_sampling,
                    test_chunks = [],
                    summary     = [],
                ),
            )

        assert plan.strategies, "all-strategies mode requires at least one strategy"
        return OptimizationPhaseResult(
            outcome             = PhaseOutcome.REUSED,
            message             = "all-strategies mode — skipping optimization",
            selected_strategies = list(plan.strategies),
        )

    def _wipe_artifacts(self, work_dir: Path, strategies: list[Strategy]) -> None:
        """Delete the shared namespace and ``optimization.yaml`` (permission band).

        Removes the ``encoded/`` winner tree, the ``encoding/`` attempt
        workspace, and the ``optimization.yaml`` parameter file — the whole
        shared namespace this phase owns invalidation over.

        Args:
            work_dir:   Pipeline working directory.
            strategies: The plan's strategies (winner-tree wipe scope).
        """
        opt_yaml = work_dir / OptimizationPhase.SIDECAR_NAME
        if opt_yaml.exists():
            opt_yaml.unlink()
            logger.debug("identity wipe: deleted %s", opt_yaml)

        _wipe_encoded_dir(work_dir, strategies)
        encoding_dir = work_dir / ENCODING_WORKSPACE_DIR
        if encoding_dir.exists():
            shutil.rmtree(encoding_dir)
            logger.debug("identity wipe: deleted %s", encoding_dir)

    @staticmethod
    def _apply_tolerance(
        results:       list[StrategySummaryRow],
        tolerance_pct: float,
    ) -> list[str]:
        """Select strategy names within *tolerance_pct* of the best (smallest) result.

        Args:
            results:       Per-strategy test results ordered by increasing total size.
            tolerance_pct: Percentage threshold; strategies within this percentage
                           of the best strategy's size are also selected.
                           ``0.0`` means exactly one strategy is selected.

        Returns:
            List of selected strategy name strings.
        """
        successful = [r for r in results if r.total_size > 0]
        if not successful:
            return []

        best_size = successful[0].total_size
        threshold = best_size * (1.0 + tolerance_pct / 100.0)

        return [r.strategy for r in successful if r.total_size <= threshold]

    @staticmethod
    def _dominates(a: StrategySummaryRow, b: StrategySummaryRow) -> bool:
        """Whether *a* Pareto-dominates *b* (the pruning predicate).

        Evaluated over the fixed-mode comparison statistics only
        (:data:`_FIXED_COMPARISON_STATS`) — stability/shape statistics are not
        quality bars and must not sway selection. Requires ``size(a) ≤
        size(b)``, ``a ≥ b`` on every compared statistic, at least one strict
        inequality (exact duplicates coexist), and identical non-empty
        compared key sets on both sides — strategies with missing or partial
        measurements are never honestly comparable.
        """
        a_metrics = _comparison_metrics(a.metrics)
        b_metrics = _comparison_metrics(b.metrics)
        if not a_metrics or set(a_metrics) != set(b_metrics):
            return False
        if a.total_size > b.total_size:
            return False
        if any(a_metrics[k] < b_metrics[k] for k in a_metrics):
            return False
        return (
            a.total_size < b.total_size
            or any(a_metrics[k] > b_metrics[k] for k in a_metrics)
        )

    @staticmethod
    def _dominance_survivors(results: list[StrategySummaryRow]) -> list[str]:
        """Survivor names after Pareto dominance pruning (fixed compared runs).

        Dominated strategies are excluded; every survivor is selected and
        encoded. Strategies with ``total_size <= 0`` (failed test encodes)
        are not candidates and are never selected.

        Args:
            results: Per-strategy test results (any order; survivors return
                     in input order).

        Returns:
            Survivor strategy names — the Pareto front.
        """
        candidates = [r for r in results if r.total_size > 0]
        return [
            r.strategy for r in candidates
            if not any(
                OptimizationPhase._dominates(other, r)
                for other in candidates if other is not r
            )
        ]

    @staticmethod
    def _select_anchor(
        results:        list[StrategySummaryRow],
        resolved_names: list[str],
        survivors:      list[str],
    ) -> str | None:
        """The measurement anchor: smallest-size survivor with metrics.

        Chosen strictly AFTER pruning — the ruler must never be a dominated
        (or about-to-be-pruned) size-tied duplicate. The anchor is the only
        front member selectable without a quality opinion; ties break
        deterministically by resolved-strategy order. Survivors without
        compared statistics cannot anchor (the ruler would be empty).

        Args:
            results:        Per-strategy test results.
            resolved_names: Strategy names in resolved order (tie-break).
            survivors:      Pruning survivor names.

        Returns:
            The anchor's strategy name, or ``None`` when no survivor carries
            compared measurements.
        """
        order = {name: i for i, name in enumerate(resolved_names)}
        eligible = [
            r for r in results
            if r.strategy in survivors and _comparison_metrics(r.metrics)
        ]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda r: (r.total_size, order.get(r.strategy, len(order))),
        ).strategy

    @staticmethod
    def _synthetic_targets_from(anchor: StrategySummaryRow | None) -> list[QualityTarget]:
        """The anchor's aggregated metrics as the synthetic target set.

        Restricted to the fixed-mode comparison statistics
        (:data:`_FIXED_COMPARISON_STATS`) — the ruler pipes straight into the
        presentation machinery, so its breadth also bounds the chunk log
        lines and the winner-limiter table. Min-across-test-chunks values
        match search-mode per-chunk strictness. Presentation data only —
        never search goals, pass/fail gates, or selection thresholds.

        Args:
            anchor: The anchor's test result, or ``None`` (empty set).

        Returns:
            Sorted quality targets mirroring the anchor's compared metrics.
        """
        if anchor is None:
            return []
        compared = _comparison_metrics(anchor.metrics)
        metrics_present = sorted({key.rsplit("_", 1)[0] for key in compared})
        targets: list[QualityTarget] = []
        for metric in metrics_present:
            for statistic in _FIXED_COMPARISON_STATS:
                key = f"{metric}_{statistic}"
                if key in compared:
                    targets.append(QualityTarget(
                        metric=metric, statistic=statistic, value=float(compared[key]),
                    ))
        return targets

    def _aggregate_strategy_metrics(
        self,
        work_dir:        Path,
        test_chunks:     list[VideoStreamChunk],
        strategy:        Strategy,
        encoded_chunks:  dict[str, list[EncodedChunk]],
    ) -> dict[str, float]:
        """Min-aggregate a strategy's measured metrics across its test winners.

        Reads each winner's result sidecar in ``encoded/<strategy>/`` — the
        same data the sidecars persist for the winning attempts (all measured
        metrics, not target-filtered). Missing winners or sidecars simply
        contribute nothing; a key present on some chunks still aggregates
        over those chunks.

        Args:
            work_dir:       Pipeline working directory.
            test_chunks:    The test-chunk set.
            strategy:       The strategy being aggregated.
            encoded_chunks: The encode result map (strategy display name ->
                            winner payloads).

        Returns:
            ``{metric_statistic: min_across_chunks}``.
        """
        from pyqenc.phases.encoding import read_winner_sidecar

        winners_by_chunk = {
            w.chunk.safe_name(): w
            for w in encoded_chunks.get(strategy.display_name(), [])
        }
        mins: dict[str, float] = {}
        for chunk in test_chunks:
            winner = winners_by_chunk.get(chunk.safe_name())
            if winner is None:
                continue
            sidecar = read_winner_sidecar(work_dir, winner)
            if sidecar is None:
                continue
            for key, value in sidecar.get("metrics", {}).items():
                measured = float(value)
                mins[key] = min(mins[key], measured) if key in mins else measured
        return mins

    def _log_optimization_summary(
        self,
        results:  list[StrategySummaryRow],
        selected: list[str],
    ) -> None:
        """Emit the optimization summary table to the log.

        Args:
            results:  All strategy test results ordered by size.
            selected: Selected strategies.
        """
        selected_names = set(selected)

        logger.info("")
        logger.info(
            "  %-30s  %12s  %8s",
            "Strategy", "Size (MB)", "Status",
        )
        logger.info(
            "  %-30s  %12s  %8s",
            "-" * 30, "-" * 12, "-" * 8,
        )

        for res in results:
            size_str = fmt_size_mb(res.total_size)
            marker   = " ◀ selected" if res.strategy in selected_names else ""
            status   = "passed" if res.total_size > 0 else "failed"
            logger.info(
                "  %-30s  %12s  %8s%s",
                res.strategy[:30], size_str, status, marker,
            )

        logger.info("")
        if selected:
            logger.info("Selected strategies: %s", ", ".join(selected))
        else:
            logger.critical("NO strategies selected (all failed).")

        logger.info("")

    def _log_fixed_comparison(
        self,
        results:        list[StrategySummaryRow],
        selected:       list[str],
        anchor_name:    str | None,
        resolved_names: list[str],
    ) -> None:
        """The fixed-mode comparison table: sizes and metric deltas vs the anchor.

        Compact layout: one Size column (MB, with the ×ratio vs the anchor
        folded in — the anchor row omits it, marking the baseline), one
        column per metric carrying both comparison statistics (anchor:
        ``p10..median`` range; others: ``Δp10/Δmedian``). The stat convention
        is stated once below the ruler, not in every column. Pruned rows name
        one dominator. Full stats live in ``optimization.yaml``.

        Args:
            results:        Per-strategy test results.
            selected:       Survivor names (the Pareto front).
            anchor_name:    The anchor's name, or ``None`` (no measurements).
            resolved_names: Strategy names in resolved order (dominator pick).
        """
        anchor_result = next((r for r in results if r.strategy == anchor_name), None)
        anchor_size   = anchor_result.total_size if anchor_result is not None else 0

        logger.info("")
        ruler = (
            f"{anchor_name} (smallest test size)" if anchor_name is not None
            else "none — no strategy carried measurements"
        )
        logger.info("Fixed-quality comparison — ruler: %s", ruler)
        logger.info(
            "  size: MB (× = vs anchor) · metrics: anchor p10..median, others Δp10/Δmedian vs anchor"
        )

        # Column keys per metric in comparison order (p10, median — present
        # ones only), metric-major.
        keys_by_metric: dict[str, list[str]] = {}
        for key in _headline_metric_keys([r.metrics for r in results if r.metrics]):
            keys_by_metric.setdefault(key.rsplit("_", 1)[0], []).append(key)

        name_width  = max((len(r.strategy) for r in results), default=30) + 2
        metric_cols = len(keys_by_metric) if anchor_result is not None else 0
        header_cells = [
            f"{'Strategy':<{name_width}}",
            f"{'Size (MB)':>12}",
        ]
        if anchor_result is not None:
            header_cells += [f"{metric:>11}" for metric in keys_by_metric]
        logger.info("  " + "  ".join(header_cells))
        logger.info("  " + "  ".join(
            ["-" * name_width, "-" * 12]
            + (["-" * 11] * metric_cols if anchor_result is not None else [])
        ))

        selected_set  = set(selected)
        dominator_for = self._dominator_map(results, resolved_names)

        for res in sorted(results, key=lambda r: (r.total_size, r.strategy)):
            size_str = fmt_size_mb(res.total_size)
            if res.strategy == anchor_name:
                size_cell = size_str  # baseline: no ratio against itself
            elif anchor_size > 0:
                size_cell = f"{size_str} ({res.total_size / anchor_size:.2f}×)"
            else:
                size_cell = f"{size_str} (N/A)"
            cells = [
                f"{res.strategy[:name_width - 2]:<{name_width}}",
                f"{size_cell:>12}",
            ]
            if anchor_result is not None:
                for keys in keys_by_metric.values():
                    if res.strategy == anchor_name:
                        values = [anchor_result.metrics.get(key) for key in keys]
                        cell = "..".join(f"{v:.1f}" for v in values if v is not None) or "-"
                    else:
                        parts: list[str] = []
                        for key in keys:
                            value = res.metrics.get(key)
                            anchor_value = anchor_result.metrics.get(key)
                            parts.append(
                                f"{value - anchor_value:+.1f}"
                                if value is not None and anchor_value is not None
                                else "-"
                            )
                        cell = "/".join(parts)
                    cells.append(f"{cell:>11}")
            if res.strategy not in selected_set:
                dominator = dominator_for.get(res.strategy)
                cells.append(
                    f"  ← dominated by {dominator} → pruned" if dominator
                    else "  ← not selected"
                )
            logger.info("  " + "  ".join(cells))

        if selected:
            logger.info("")
            logger.info(
                "Survivors (Pareto front): %s — all will be encoded",
                ", ".join(selected),
            )
        else:
            logger.critical("NO survivors selected (all failed).")
        logger.info("")

    @staticmethod
    def _dominator_map(
        results:        list[StrategySummaryRow],
        resolved_names: list[str],
    ) -> dict[str, str]:
        """Map each pruned strategy to one dominating strategy (first in resolved order).

        Args:
            results:        Per-strategy test results.
            resolved_names: Strategy names in resolved order (dominator pick).

        Returns:
            ``{pruned strategy: a strategy that dominated it}``.
        """
        order = {name: i for i, name in enumerate(resolved_names)}
        by_order = sorted(results, key=lambda r: order.get(r.strategy, len(order)))
        dominators: dict[str, str] = {}
        for beaten in by_order:
            if beaten.total_size <= 0:
                continue
            for dominator in by_order:
                if dominator is not beaten and OptimizationPhase._dominates(dominator, beaten):
                    dominators[beaten.strategy] = dominator.strategy
                    break
        return dominators

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _wipe_encoded_dir(work_dir: Path, strategies: list[Strategy]) -> None:
    """Delete the entire ``encoded/<strategy>/`` directory for each strategy.

    All contents are hard-linked winning attempts and result sidecars — no
    unique data lives here.  ``EncodingPhase._recover()`` will re-discover
    attempts from ``encoding/`` and re-evaluate them from scratch.

    Called when quality targets or ``metrics_sampling`` change, since both
    invalidate the selected winners and their recorded metrics.

    If the strategy list does not cover all subdirs present (e.g. strategies
    were renamed), wipes the entire ``encoded/`` base directory as a fallback
    to guarantee no stale data remains.

    Args:
        work_dir:   Pipeline working directory.
        strategies: All configured strategies (name used for directory lookup).
    """
    encoded_base = work_dir / ENCODED_OUTPUT_DIR
    if not encoded_base.exists():
        return

    # Collect all existing strategy subdirs
    existing_dirs = [d for d in encoded_base.iterdir() if d.is_dir()]
    expected_names = {s.display_name() for s in strategies}
    unexpected = [d for d in existing_dirs if d.name not in expected_names]

    if unexpected:
        # Stale dirs from old/renamed strategies present — wipe the whole base
        logger.debug(
            "Wiping entire encoded/ base dir (unexpected subdirs: %s)",
            ", ".join(d.name for d in unexpected),
        )
        try:
            shutil.rmtree(encoded_base)
            logger.debug("Wiped encoded/ base dir: %s", encoded_base)
        except OSError as exc:
            logger.warning("Could not wipe encoded/ base dir %s: %s", encoded_base, exc)
        return

    for strategy in strategies:
        strategy_dir = encoded_base / strategy.safe_name()
        if not strategy_dir.exists():
            continue
        try:
            shutil.rmtree(strategy_dir)
            logger.debug("Wiped stale encoded dir: %s", strategy_dir)
        except OSError as exc:
            logger.warning("Could not wipe encoded dir %s: %s", strategy_dir, exc)


def _headline_metric_keys(metric_maps: list[dict[str, float]]) -> list[str]:
    """The fixed-mode comparison-table column set.

    Exactly what the ruler judges (:data:`_FIXED_COMPARISON_STATS` — the
    table must not imply a different comparison basis than pruning used): for
    every distinct metric, its ``p10`` and ``median`` statistics when
    measured, ordered metric-major. Full statistics live in
    ``optimization.yaml``.

    Args:
        metric_maps: The measured metric maps to derive the column set from.

    Returns:
        Headline keys as ``"{metric}_{statistic}"``, metric-major ordered.
    """
    present: set[str] = set()
    for metrics in metric_maps:
        present.update(_comparison_metrics(metrics))
    return [
        f"{metric}_{statistic}"
        for metric in sorted({key.rsplit("_", 1)[0] for key in present})
        for statistic in _FIXED_COMPARISON_STATS
        if f"{metric}_{statistic}" in present
    ]


def _select_test_chunks(
    chunks:                list[VideoStreamChunk],
    percentage:            float = 0.01,
    min_chunks:            int   = 3,
    exclude_start_percent: float = 0.10,
    exclude_end_percent:   float = 0.10,
) -> list[VideoStreamChunk]:
    """Select representative test chunks for optimization.

    Selects approximately 1% of chunks (minimum 3) from the middle 80% of
    the video, excluding the first 10% and last 10% which may not be
    representative.

    Args:
        chunks:                List of all chunks.
        percentage:            Percentage of chunks to select (default 1%).
        min_chunks:            Minimum number of chunks to select.
        exclude_start_percent: Percentage to exclude from start (default 10%).
        exclude_end_percent:   Percentage to exclude from end (default 10%).

    Returns:
        List of selected test chunks sorted by chunk ID.
    """
    total = len(chunks)
    start_idx = int(total * exclude_start_percent)
    end_idx   = int(total * (1.0 - exclude_end_percent))
    eligible  = chunks[start_idx:end_idx]

    if not eligible:
        logger.warning("No eligible chunks after exclusion — using all chunks")
        eligible = chunks

    num = max(min_chunks, int(total * percentage))
    num = min(num, len(eligible))

    selected = random.sample(eligible, num)
    selected.sort(key=lambda c: c.safe_name())
    return selected


def _make_encoder(
    work_dir:         Path,
    collector:        MetricsCollector,
    crop_params:      CropParams | None,
    visual_hash:      bool = True,
    metrics_sampling: int  = 3,
    cleanup_level:    CleanupLevel = CleanupLevel.NONE,
    metric_prefix:    MetricKey = MetricKey.OPTIMIZATION,
) -> ChunkEncoder:
    """Construct a ``ChunkEncoder`` for test encodes.

    The encoder follows the full encoding contract on the optimization
    subset: dotted timing keys are prefixed by ``metric_prefix``
    (``optimization.<strategy>``, ``optimization.quality_measure``) and the
    run's ``cleanup_level`` enables rolling attempt cleanup after each pair
    converges. Test encodes are always measured — the anchor's synthetic
    target set is derived from them (fixed-quality spec Req 8.1), regardless
    of the encoding phase's measurement control.

    Args:
        work_dir:         Pipeline working directory.
        crop_params:      Crop parameters to apply.
        visual_hash:      Whether to prepend emoji hash to chunk log lines.
        metrics_sampling: Frame subsampling factor for quality metric generation.
        cleanup_level:    Run cleanup level for rolling attempt cleanup.
        metric_prefix:    Top-level key prefixing the encoder's dotted keys.

    Returns:
        Configured ``ChunkEncoder`` instance.
    """
    from pyqenc.phases.encoding import ChunkEncoder
    return ChunkEncoder(
        quality_evaluator = QualityEvaluator(work_dir),
        work_dir          = work_dir,
        collector         = collector,
        crop_params       = crop_params,
        cleanup_level     = cleanup_level,
        visual_hash       = visual_hash,
        metrics_sampling  = metrics_sampling,
        metric_prefix     = metric_prefix,
        measure_attempts  = True,
    )



