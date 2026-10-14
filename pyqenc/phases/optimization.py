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
    FingerprintYaml,
    PhaseOutcome,
    QualityTarget,
    SizedFingerprint,
    SizedFingerprintYaml,
    Strategy,
    fingerprinter,
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
from pyqenc.summary_tables import (
    COMPARISON_STATS,
    ComparisonTable,
    Mark,
    MetricStatMap,
    Ruler,
    StrategyRow,
    render_comparison_table,
)
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.fs import safe_stat_size
from pyqenc.utils.log_format import fmt_key_value_table
from pyqenc.utils.visualization import QualityMeasurer
from pyqenc.utils.yaml_utils import save_model

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector
    from pyqenc.phases.encoding import ChunkEncoder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# optimization.yaml — per-mode discriminated-union sidecars (Req 18-21, 61)
# ---------------------------------------------------------------------------

class _OptimizationSidecarBase(BaseModel):
    """The common keys of both ``optimization.yaml`` variants.

    Fields are named for WHAT they identify (Req 61): ``source`` the source
    content identity, ``chunks`` the chunk-set fingerprint (a re-chunk wipes
    winners), ``strategies`` each strategy's resolved-args fingerprint,
    ``probe`` the structured facet, ``sampling`` the measurement factor.
    The key block is REQUIRED (every write path populates it) — a sidecar
    missing keys fails validation and loads as absent: the phase redoes its
    work (Req 32 amended 2026-10-10; per-key unknown semantics remain for
    the ``strategies`` map only). ``summary`` is the replay aggregate
    (freshness guaranteed by the pending gate), and ``test_chunks`` the
    persisted selection basis (checked for full-set presence at read —
    Req 44).
    """

    source:      SizedFingerprintYaml
    chunks:      SizedFingerprintYaml
    strategies:  dict[str, FingerprintYaml]  = Field(default_factory=dict)
    probe:       ProbeFacet
    sampling:    int
    test_chunks: list[str]                     = Field(default_factory=list)
    summary:     list[StrategyRow]      = Field(default_factory=list)


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
        The tagged sidecar, or ``None`` when absent or unparsable.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
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
    source:      SizedFingerprint,
    chunks:      SizedFingerprint,
    probe:       ProbeFacet,
    sampling:    int,
    test_chunks: list[str],
    summary:     list[StrategyRow],
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


def _comparison_metrics(metrics: MetricStatMap) -> MetricStatMap:
    """Project measured metrics onto the fixed-mode comparison stat set.

    Args:
        metrics: The worst-chunk aggregated measurement (pair-keyed).

    Returns:
        The subset restricted to the comparison statistics.
    """
    return {
        key: value for key, value in metrics.items()
        if key.statistic in COMPARISON_STATS
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
        self._strategy_results:  list[StrategyRow]          = []
        self._current_probe:     ProbeFacet | None                 = None
        self._chunks_fingerprint: SizedFingerprint | None           = None
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
            # §123: fixed runs always run the test population — single-
            # strategy and `--no-optimize` included (the latter with
            # dominance pruning disabled at selection); the anchor mechanics
            # and the ruler behave identically to any compared fixed run.
            return None

        # All-strategies mode: triggered by the optimize flag being off or a
        # single strategy given (nothing to optimize against) — search runs
        # only; the fixed case returned above.
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
        """Log the optimization phase key input params."""
        plan = self._deps[ProbePhase].plan

        kv: dict[str, object] = {
            "Tolerance:":  self._config.encoding.optimize_tolerance,
        }

        if plan.fixed_quality:
            kv["Quality:"]    = [f"{strategy.codec.quality_label}={strategy.codec.quality_range[0]} for {strategy.display_name()}" for strategy in plan.strategies]
        else:
            kv["Strategies:"] = [s.display_name() for s in plan.strategies]
            kv["Targets:"]    = [f"{t.metric}-{t.statistic}≥{t.value}" for t in plan.targets]

        fmt_key_value_table(kv)

    def _recovery_unit(self) -> str:
        """The recovery summary counts winning attempts (one per pair)."""
        return "attempt"

    def _invalidate(self) -> None:
        """Run the shared-namespace invalidation ladder (disk effects only).

        Delegates to :meth:`_invalidate_shared_namespace` — identity /
        probe facet / strategy-args fingerprints (fatal or permission-gated
        wipes), the winner-band keys (automatic wipe-winners + sidecar
        rewrite), and the §99 conservative wipe for a missing sidecar.
        Every effect lands on disk; the classification re-reads the sidecar
        afterwards (no in-memory handoff).

        Raises:
            RecoveryError: On a fatal-band condition without ``--force``.
        """
        plan     = self._deps[ProbePhase].plan
        work_dir = self._deps[JobPhase].work_dir
        self._invalidate_shared_namespace(work_dir, plan)

    def _recover(self) -> Recovery:
        """Classify the pair ledger from post-invalidation disk truth.

        Runs after :meth:`_invalidate` settled the sidecar on disk; the
        classification re-reads it. Steps:

        1. The per-pair ledger (one ``Artifact[EncodedChunk]`` row per
           (test chunk, strategy) winning attempt, presence-based). The
           to-test set is its projection: any strategy with a non-``COMPLETE``
           pair has work pending; complete pairs are the reuse substrate.
        2. The test-chunk selection: the persisted set reused only when it
           fully survives in the current chunking output (O-6).
        3. Nothing to test and a persisted table covering the plan → the
           fast exit seeds the display from the persisted rows (plan-scoped)
           and computes the selection live.

        Returns:
            The :class:`Recovery` single source of truth.

        Raises:
            RecoveryError: When ChunkingPhase produced no chunks.
        """
        work_dir  = self._deps[JobPhase].work_dir
        tolerance = self._config.encoding.optimize_tolerance
        plan      = self._deps[ProbePhase].plan

        strategies = plan.strategies

        # The invalidation ladder settled the sidecar on disk; the
        # classification re-reads it (current keys, or absent).
        persisted = load_optimization_sidecar(
            work_dir / OptimizationPhase.SIDECAR_NAME,
        )

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
                # Re-select live (pruning under the current tolerance — or
                # all measured rows for `--no-optimize` fixed runs, §123) and
                # re-derive the anchor ruler from the persisted measurements.
                if self._config.encoding.optimize:
                    self._selected_names = self._dominance_survivors(table_rows, tolerance)
                else:
                    self._selected_names = [r.strategy for r in table_rows if r.size_bytes > 0]
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
                    sorted(table_rows, key=lambda r: r.size_bytes), tolerance,
                )

        # Searched: fatal only when work is actually pending. Fixed runs now
        # always run the test population (§123) — without test chunks they
        # cannot elect an anchor and fail loudly.
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
        self._chunks_fingerprint = fingerprinter.of_ids(
            c.safe_name() for c in (a.payload for a in self._deps[ChunkingPhase].chunks)
        )
        self._current_probe = ProbeFacet.from_probe(self._deps[ProbePhase])
        current_probe = self._current_probe
        chunks_fps    = self._chunks_fingerprint
        assert current_probe is not None and chunks_fps is not None
        current    = current_optimization_sidecar(
            plan        = plan,
            source      = job_result.file.payload.fingerprint,
            chunks      = chunks_fps,
            probe       = current_probe,
            sampling    = self._config.measurement.sampling,
            test_chunks = [],   # the selection resolves after invalidation
            summary     = [],
        )

        # Step 1 — identity key: catastrophic for the shared namespace.
        persisted: OptimizationSidecar | None = load_optimization_sidecar(opt_yaml)
        if persisted is not None and not persisted.source.matches(
            job_result.file.payload.fingerprint,
        ):
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
                # ONLY the changed strategies' trees go (Req 39's letter).
                # The sidecar is rewritten immediately, so the §99
                # unknown-currency branch below must NOT fire: other
                # strategies' winners stay (their keys are current now).
                self._wipe_strategies(work_dir, plan, changed_strategies)
                save_optimization_sidecar(opt_yaml, current)
                persisted = current

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

        The key block is required on both sides (a sidecar missing keys
        never loaded); per-key unknown semantics remain for the
        ``strategies`` map only. An equal fixed pinned map contributes
        nothing (O-2).
        """
        reasons: list[str] = []
        if persisted.mode != current.mode:
            reasons.append(
                f"mode changed ({persisted.mode} → {current.mode})"
            )
        if not persisted.chunks.matches(current.chunks):
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
        if persisted.sampling != current.sampling:
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

            pending_pairs   = sum(1 for r in phase_recovery.pairs.values() if r.state != ArtifactState.COMPLETE)
            complete_pairs  = len(test_chunks) * len(strategies_to_test) - pending_pairs
            logger.info(
                "Starting optimization: %d pair(s) to encode, %d already complete",
                pending_pairs, complete_pairs,
            )

            with ProgressBar(total_seconds, title="Optimization", total_count=total_count) as advance:
                # Pre-advance bar for already-complete pairs
                chunks_by_id = {c.safe_name(): c for c in test_chunks}
                for r in phase_recovery.pairs.values():
                    if r.state == ArtifactState.COMPLETE:
                        advance((chunks_by_id[r.chunk_id].end_timestamp - chunks_by_id[r.chunk_id].start_timestamp), AdvanceState.SKIPPED)

                asyncio.run(
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
            # state — sizes from the winners, metrics worst-chunk-aggregated
            # from the winner sidecars.  Winners are always read from the live
            # pair ledger so that strategies completed on a prior run are
            # included alongside any newly-encoded ones.  Using enc_result
            # alone was wrong on partial reruns: it only carries the strategies
            # encoded this run, leaving the already-complete strategies with
            # empty winners → size_bytes=0 and blank metrics in the sidecar.
            winners_by_strategy: dict[str, list[EncodedChunk]] = {}
            for row in rows:
                if row.state == ArtifactState.COMPLETE and isinstance(row.payload, EncodedChunk):
                    winners_by_strategy.setdefault(
                        row.payload.strategy.display_name(), [],
                    ).append(row.payload)
            final_results = [
                self._build_strategy_row(work_dir, test_chunks, strategy, winners_by_strategy)
                for strategy in plan.strategies
            ]

            # Selection = tolerance-epsilon dominance pruning — disabled for
            # `--no-optimize` fixed runs (§123: every strategy is selected;
            # anchoring, not dominance, is the point); anchor = smallest
            # survivor (or smallest measured row unpruned); synthetic set =
            # the anchor's worst-chunk metrics. The sidecar persists facts
            # only — the ruler re-derives on read, so a changed comparison
            # stat set re-projects old measurements correctly.
            resolved_names = [s.display_name() for s in plan.strategies]
            if self._config.encoding.optimize:
                selected = self._dominance_survivors(final_results, tolerance)
            else:
                selected = [r.strategy for r in final_results if r.size_bytes > 0]
            self._anchor_name = self._select_anchor(final_results, resolved_names, selected)
            anchor_result = next(
                (r for r in final_results if r.strategy == self._anchor_name), None,
            )
            self._synthetic_targets = self._synthetic_targets_from(anchor_result)

            save_optimization_sidecar(
                opt_yaml,
                current_optimization_sidecar(
                    plan        = plan,
                    source      = self._deps[JobPhase].file.payload.fingerprint,
                    chunks      = self._chunks_fingerprint,
                    probe       = self._current_probe,
                    sampling    = current_sampling,
                    test_chunks = [c.safe_name() for c in test_chunks],
                    summary     = final_results,
                ),
            )

            self._selected_names   = selected
            self._strategy_results = final_results

            self._log_comparison(final_results, selected, self._anchor_name, resolved_names)

            return self._make_result(
                PhaseOutcome.COMPLETED,
                [r for r in rows if r.wanted],
                f"{len(selected)} survivor(s) selected",
            )

        # Searched: rows derive from the live ledger — the winner files of
        # the plan's strategies (never the persisted table). Sizes stat the
        # winners; metrics worst-chunk-aggregate from the winner sidecars.
        winners_by_strategy: dict[str, list[EncodedChunk]] = {}
        for row in rows:
            if row.state == ArtifactState.COMPLETE and isinstance(row.payload, EncodedChunk):
                winners_by_strategy.setdefault(row.payload.strategy.display_name(), []).append(row.payload)
        final_results = sorted(
            [
                self._build_strategy_row(work_dir, test_chunks, strategy, winners_by_strategy)
                for strategy in plan.strategies
            ],
            key=lambda r: r.size_bytes,
        )
        selected = self._apply_tolerance(final_results, tolerance)

        # The single sidecar save — after successful processing.
        save_optimization_sidecar(
            opt_yaml,
            current_optimization_sidecar(
                plan        = plan,
                source      = self._deps[JobPhase].file.payload.fingerprint,
                chunks      = self._chunks_fingerprint,
                probe       = self._current_probe,
                sampling    = current_sampling,
                test_chunks = [c.safe_name() for c in test_chunks],
                summary     = final_results,
            ),
        )

        self._selected_names   = selected
        self._strategy_results = final_results

        self._log_comparison(final_results, selected, None, [s.display_name() for s in plan.strategies])

        return self._make_result(
            PhaseOutcome.COMPLETED,
            [r for r in rows if r.wanted],
            f"{len(selected)} strategy(ies) selected",
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> OptimizationPhaseResult:
        """Build the reused result from the fast-exit stash (display + live selection)."""
        plan = self._deps[ProbePhase].plan

        self._log_comparison(
            self._strategy_results, self._selected_names, self._anchor_name,
            [s.display_name() for s in plan.strategies],
        )
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
                    source      = job_result.file.payload.fingerprint,
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
        results:       list[StrategyRow],
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
        successful = [r for r in results if r.size_bytes > 0]
        if not successful:
            return []

        best_size = successful[0].size_bytes
        threshold = best_size * (1.0 + tolerance_pct / 100.0)

        return [r.strategy for r in successful if r.size_bytes <= threshold]

    @staticmethod
    def _dominates(a: StrategyRow, b: StrategyRow, tolerance_pct: float) -> bool:
        """Whether *a* tolerance-epsilon-dominates *b* (the pruning predicate).

        Both dimensions tolerate (Req 9): size within ``tolerance_pct`` (the
        search-selection band reused) and every compared statistic within the
        metric's ``acceptance_delta`` — a median surplus never covers a p10
        deficit (per-statistic conjunction, no compensation). Given no
        meaningful deficit, SIZE breaks the tie: a strictly-cheaper strategy
        dominates (the pipeline's objective is size at equal quality); at
        equal size, domination needs strict superiority on EVERY compared
        statistic — near-equal ±stats duplicates coexist. The size tiebreak
        makes the relation antisymmetric (mutual near-ties would otherwise
        prune both). Evaluated over the fixed-mode comparison statistics
        only; identical non-empty compared key sets required.
        """
        a_metrics = _comparison_metrics(a.metrics)
        b_metrics = _comparison_metrics(b.metrics)
        if not a_metrics or set(a_metrics) != set(b_metrics):
            return False
        if a.size_bytes > b.size_bytes * (1.0 + tolerance_pct / 100.0):
            return False
        if any(
            a_metrics[key] < b_metrics[key] - key.metric.info.acceptance_delta
            for key in a_metrics
        ):
            return False
        if a.size_bytes < b.size_bytes:
            return True
        return a.size_bytes == b.size_bytes and all(
            a_metrics[key] > b_metrics[key] for key in a_metrics
        )

    @staticmethod
    def _dominance_survivors(results: list[StrategyRow], tolerance_pct: float) -> list[str]:
        """Survivor names after tolerance-epsilon dominance pruning (fixed runs).

        Dominated strategies are excluded; every survivor is selected and
        encoded. Strategies with ``size_bytes <= 0`` (failed test encodes)
        are not candidates and are never selected.

        Args:
            results:       Per-strategy test results (any order; survivors
                           return in input order).
            tolerance_pct: The size tolerance band (percent).

        Returns:
            Survivor strategy names.
        """
        candidates = [r for r in results if r.size_bytes > 0]
        return [
            r.strategy for r in candidates
            if not any(
                OptimizationPhase._dominates(other, r, tolerance_pct)
                for other in candidates if other is not r
            )
        ]

    @staticmethod
    def _select_anchor(
        results:        list[StrategyRow],
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
            key=lambda r: (r.size_bytes, order.get(r.strategy, len(order))),
        ).strategy

    @staticmethod
    def _synthetic_targets_from(anchor: StrategyRow | None) -> list[QualityTarget]:
        """The anchor's aggregated metrics as the synthetic target set.

        Restricted to the fixed-mode comparison statistics — the ruler pipes
        straight into the presentation machinery, so its breadth also bounds
        the chunk log lines and the winner-limiter table. Worst-chunk values
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
        stat_rank = {statistic: index for index, statistic in enumerate(COMPARISON_STATS)}
        return [
            QualityTarget(
                metric=key.metric.value, statistic=key.statistic.value, value=float(value),
            )
            for key, value in sorted(
                compared.items(),
                key=lambda kv: (kv[0].metric.value, stat_rank[kv[0].statistic]),
            )
        ]

    def _build_strategy_row(
        self,
        work_dir:            Path,
        test_chunks:         list[VideoStreamChunk],
        strategy:            Strategy,
        winners_by_strategy: dict[str, list[EncodedChunk]],
    ) -> StrategyRow:
        """Build one strategy's row from its test winners (both modes).

        Size stats the winner files that exist; metrics worst-chunk-aggregate
        from the winner sidecars. The single row-assembly site for processing
        runs (both modes) — the fast exit reads persisted rows instead.

        Args:
            work_dir:            Pipeline working directory.
            test_chunks:         The test-chunk set.
            strategy:            The strategy the row describes.
            winners_by_strategy: Winner payloads per strategy display name.

        Returns:
            The strategy's ``StrategyRow`` (worst-chunk values; empty metrics
            when nothing was measured).
        """
        winners = winners_by_strategy.get(strategy.display_name(), [])
        size_bytes = sum(safe_stat_size(w.stream.stream.file.path) or 0 for w in winners)
        return StrategyRow(
            strategy   = strategy.display_name(),
            size_bytes = int(size_bytes),
            metrics    = self._aggregate_strategy_metrics(work_dir, test_chunks, strategy, winners),
        )

    def _aggregate_strategy_metrics(
        self,
        work_dir:   Path,
        test_chunks: list[VideoStreamChunk],
        strategy:   Strategy,
        winners:    list[EncodedChunk],
    ) -> MetricStatMap:
        """Worst-chunk-aggregate a strategy's measured metrics across its test winners.

        Reads each winner's result sidecar in ``encoded/<strategy>/`` — the
        same data the sidecars persist for the winning attempts. Missing
        winners or sidecars simply contribute nothing; a key present on some
        chunks still aggregates over those chunks. Sidecar keys parse
        through the pair type — the sanctioned stringly→typed conversion at
        table assembly (Req 7.5).

        Args:
            work_dir:    Pipeline working directory.
            test_chunks: The test-chunk set.
            strategy:    The strategy being aggregated.
            winners:     The strategy's winner payloads.

        Returns:
            Worst-chunk values keyed by the metric-statistic pair type.
        """
        from pyqenc.phases.encoding import read_winner_sidecar
        from pyqenc.quality import MetricStatKey as _Key

        winners_by_chunk = {w.chunk.safe_name(): w for w in winners}
        mins: MetricStatMap = {}
        for chunk in test_chunks:
            winner = winners_by_chunk.get(chunk.safe_name())
            if winner is None:
                continue
            sidecar = read_winner_sidecar(work_dir, winner)
            if sidecar is None:
                continue
            for key, measured in sidecar.metrics.items():
                parsed = _Key.parse(key)
                mins[parsed] = min(mins[parsed], measured) if parsed in mins else measured
        return mins

    def _log_comparison(
        self,
        results:        list[StrategyRow],
        selected:       list[str],
        anchor_name:    str | None,
        resolved_names: list[str],
    ) -> None:
        """Emit the unified comparison table (both modes) plus detail lines.

        The table renders through the shared comparison renderer; below the
        table come the pruned-with-dominator detail lines (fixed mode with
        pruning). The concise outcome line (§108) is the caller's, not the
        table's.

        Args:
            results:        Per-strategy test results.
            selected:       Selected/survivor names.
            anchor_name:    The anchor's name (fixed mode), or ``None``.
            resolved_names: Strategy names in resolved order (dominator pick).
        """
        plan      = self._deps[ProbePhase].plan
        tolerance = self._config.encoding.optimize_tolerance
        all_rows  = list(results)
        if plan.fixed_quality:
            ruler: Ruler | None = (
                Ruler(kind="anchor", anchor=anchor_name)
                if anchor_name is not None else None
            )
        else:
            ruler = Ruler(kind="targets", targets=tuple(plan.targets))

        table = ComparisonTable(
            rows               = all_rows,
            ruler              = ruler,
            marks              = {name: Mark(selected=True) for name in selected},
            mark_legend        = "selected",
            size_tolerance_pct = tolerance,
        )
        for line in render_comparison_table(table):
            logger.info(line)

        if plan.fixed_quality and self._config.encoding.optimize:
            dominator_for = self._dominator_map(results, resolved_names, tolerance)
            for beaten, dominator in dominator_for.items():
                if beaten not in selected:
                    logger.info("pruned: %s — dominated by %s", beaten, dominator)

        if not selected:
            logger.critical("NO strategies selected (all failed).")
        logger.info("")

    @staticmethod
    def _dominator_map(
        results:        list[StrategyRow],
        resolved_names: list[str],
        tolerance_pct:  float,
    ) -> dict[str, str]:
        """Map each pruned strategy to one dominating strategy (first in resolved order).

        Args:
            results:        Per-strategy test results.
            resolved_names: Strategy names in resolved order (dominator pick).
            tolerance_pct:  The size tolerance band (percent).

        Returns:
            ``{pruned strategy: a strategy that dominated it}``.
        """
        order = {name: i for i, name in enumerate(resolved_names)}
        by_order = sorted(results, key=lambda r: order.get(r.strategy, len(order)))
        dominators: dict[str, str] = {}
        for beaten in by_order:
            if beaten.size_bytes <= 0:
                continue
            for dominator in by_order:
                if dominator is not beaten and OptimizationPhase._dominates(dominator, beaten, tolerance_pct):
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
        quality_evaluator = QualityMeasurer(work_dir),
        work_dir          = work_dir,
        collector         = collector,
        crop_params       = crop_params,
        cleanup_level     = cleanup_level,
        visual_hash       = visual_hash,
        metrics_sampling  = metrics_sampling,
        metric_prefix     = metric_prefix,
        measure_attempts  = True,
    )



