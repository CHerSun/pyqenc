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
from typing import TYPE_CHECKING, ClassVar

from pyqenc.constants import (
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
    WARNING_SYMBOL,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import (
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    Strategy,
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
from pyqenc.state import (
    ArtifactState,
    OptimizationParams,
    ProbeState,
    StrategyTestResult,
)
from pyqenc.stream_model import EncodedChunk, VideoStreamChunk
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.log_format import fmt_size_mb
from pyqenc.utils.visualization import QualityEvaluator

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector
    from pyqenc.phases.encoding import ChunkEncoder

logger = logging.getLogger(__name__)


_FIXED_COMPARISON_STATS: tuple[str, ...] = ("p10", "median")
"""The fixed-mode comparison statistic set per metric.

The single definition of what fixed-mode comparison consumes: the anchor's
synthetic target set (the ruler), the dominance pruning input, and the
comparison-table columns. ``p10`` guards the worst decile, ``median`` the
central tendency; the remaining stats are stability/shape indicators
(``std``, ``min``, ``max``, other percentiles), not quality bars — comparing
or ruling on them silently skews selection. The full measured set stays
aggregated in ``StrategyTestResult.metrics`` / sidecars (data retention), so
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
        self._persisted:         OptimizationParams | None          = None
        self._cached_results:    dict[str, StrategyTestResult]     = {}
        self._strategies_to_test: list[Strategy]                   = []
        self._test_chunks:       list[VideoStreamChunk]            = []
        self._tolerance_reapply: bool                              = False
        self._selected_names:    list[str]                         = []
        self._strategy_results:  list[StrategyTestResult]          = []
        self._current_probe:     ProbeState | None                 = None
        self._anchor_name:       str | None                        = None
        """The fixed-mode measurement anchor's display name (fixed compared
        runs only; ``None`` in searched and uncompared runs)."""
        self._synthetic_targets: list[QualityTarget]               = []
        """The anchor-derived presentation ruler (fixed compared runs only)."""

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _skip_check(self, dry_run: bool) -> OptimizationPhaseResult | None:
        """Skip decision plus the fixed-mode entry (guard, wipe, banner).

        Dependencies are already resolved when this runs (the template
        resolves them before the skip check), so both the fixed-mode entry
        and the all-strategies path read dependency results directly.

        The fixed-mode entry lives here because ``_skip_check`` is the single
        always-executed point the template's ``run()`` crosses on BOTH the
        optimize path and the all-strategies path — the wipe, guard, and
        banner must fire on every fixed start regardless of mode.

        Args:
            dry_run: When ``True``, skip the ``optimization.yaml`` write and
                the winner-layer wipe (a dry run changes nothing).

        Returns:
            The all-strategies result, a FAILED result when the cleanup guard
            stops the run — or ``None`` to proceed with the template.
        """
        strategies = self._dep_result(JobPhase).plan.strategies

        if self._dep_result(JobPhase).plan.fixed_quality:
            entry = self._fixed_mode_entry(dry_run, strategies)
            if entry is not None:
                return entry

        # All-strategies mode: triggered by the optimize flag being off or a
        # single strategy given (nothing to optimize against).
        if self._config.encoding.optimize and len(strategies) > 1:
            return None

        return self._all_strategies(dry_run)

    def _fixed_mode_entry(
        self,
        dry_run:    bool,
        strategies: list[Strategy],
    ) -> OptimizationPhaseResult | None:
        """Every fixed-mode start: cleanup guard, winner wipe, banner (Req 5–7).

        Order matters: the guard hard-stops before any destructive or
        expensive work; the wipe deletes the winner layer unconditionally
        (winners re-derive from the attempt workspace during execution);
        the banner announces what a fixed run does and does not guarantee.

        Args:
            dry_run:    When ``True``, skip the wipe (a dry run changes nothing).
            strategies: The resolved strategies (wipe scope, banner content).

        Returns:
            A FAILED result when the cleanup guard stops the run; otherwise
            ``None`` to continue into the mode branch exactly as a searched
            run would.
        """
        job_result = self._dep_result(JobPhase)

        if job_result.cleanup >= CleanupLevel.INTERMEDIATE:
            err = (
                f"Fixed-quality run refuses cleanup level {job_result.cleanup.name} "
                f"(>= INTERMEDIATE): attempts in encoding/ are the re-derivation "
                f"substrate for fixed re-runs — cleanup deletes them, and winners "
                f"alone cannot re-derive after a value change or interruption, so "
                f"an interrupted run resumed under cleanup would re-encode "
                f"completed chunks. Re-run without --cleanup."
            )
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        if not dry_run:
            # Unconditional winner-layer invalidation: no q-value or mode is
            # persisted for comparison — winners re-derive from attempts.
            _wipe_encoded_dir(job_result.work_dir, strategies)

        self._log_fixed_quality_banner(strategies)
        return None

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
        logger.info("Strategies:  %s", ", ".join(s.display_name() for s in self._dep_result(JobPhase).plan.strategies))
        logger.info("Tolerance:   %.1f%%", self._config.encoding.optimize_tolerance)

    def _recovery_unit(self) -> str:
        """The recovery summary counts winning attempts (one per pair)."""
        return "attempt"

    def _recover(self) -> Recovery:
        """Resolve optimization state currency: invalidations first, then caches.

        Steps:

        1. ``force_wipe`` (from JobPhase) → delete ``optimization.yaml`` and
           the ``encoding/`` test workspace.
        2. Probe mismatch against ``optimization.yaml`` — fatal without
           ``--force`` (handled in step 1 when forced).
        3. Quality-target / metrics-sampling change → wipe ``encoded/``
           result dirs and treat all cached strategy results as stale.
        4. The per-pair ledger (one ``Artifact[EncodedChunk]`` row per
           (test chunk, strategy) winning attempt, presence-based) plus the
           to-test decision: searched mode keys on cached results; fixed
           mode keys on pair presence (the fixed start wiped the winners —
           re-promotion via attempt cache-hits is near-free on unchanged q).
        5. Cached tolerance mismatch → cheap pending re-select (searched only).

        Returns:
            The :class:`Recovery` single source of truth.

        Raises:
            RecoveryError: On a probe change without ``--force``, or when
                ChunkingPhase produced no chunks.
        """
        job_result   = self._dep_result(JobPhase)
        probe_result = self._dep_result(ProbePhase)
        work_dir     = job_result.work_dir
        opt_yaml     = work_dir / OptimizationPhase.SIDECAR_NAME
        tolerance    = self._config.encoding.optimize_tolerance
        strategies   = self._dep_result(JobPhase).plan.strategies
        force_wipe   = job_result.force_wipe

        current_probe      = ProbeState.from_probe(probe_result)
        self._current_probe = current_probe

        # Step 1 — force wipe (before any currency decision, so --force
        # always re-tests).
        persisted: OptimizationParams | None = OptimizationParams.load(opt_yaml)
        if force_wipe:
            self._wipe_artifacts(work_dir)
            persisted = None

        # Step 2 — probe mismatch invalidation.
        if persisted is not None and persisted.strategy_results and persisted.probe != current_probe:
            raise RecoveryError(
                "Probe params changed since last optimization run "
                f"(persisted={persisted.probe}, current={current_probe}). "
                "Re-run with --force to delete stale optimization artifacts and continue."
            )

        # Step 3 — quality-target / sampling change detection.
        current_targets  = targets_as_strings(self._dep_result(JobPhase).plan.targets)
        current_sampling = self._config.measurement.sampling
        targets_changed = (
            persisted is not None
            and bool(persisted.quality_targets)
            and persisted.quality_targets != current_targets
        )
        sampling_changed = (
            persisted is not None
            and persisted.sampling is not None
            and persisted.sampling != current_sampling
        )
        if (targets_changed or sampling_changed) and persisted is not None and persisted.strategy_results:
            if sampling_changed:
                logger.debug(
                    "metrics sampling changed (%d → %d) — wiping encoded/ dirs",
                    persisted.sampling, current_sampling,
                )
            # Wipe encoded/ for every strategy — contents are hard-linked attempts
            # and result sidecars; EncodingPhase will re-discover from encoding/.
            _wipe_encoded_dir(work_dir, strategies)
            # Treat all cached strategy results as stale — force re-encoding.
            persisted = OptimizationParams(
                probe            = persisted.probe,
                test_chunks      = persisted.test_chunks,
                strategy_results = [],
                tolerance_pct    = persisted.tolerance_pct,
                selected         = [],
                quality_targets  = persisted.quality_targets,
                sampling         = persisted.sampling,
            )
        self._persisted = persisted

        # Step 4 — the per-pair ledger first (fixed mode derives its to-test
        # set from pair presence), then the cached-results / to-test decision.
        cached_results: dict[str, StrategyTestResult] = {}
        if persisted is not None:
            for r in persisted.strategy_results:
                cached_results[r.strategy] = r
        self._cached_results = cached_results

        self._test_chunks = self._resolve_test_chunks(persisted)
        rows = self._pair_ledger(work_dir, strategies)
        fixed = self._dep_result(JobPhase).plan.fixed_quality

        if fixed:
            # Presence-based re-test decision: the fixed start wiped encoded/,
            # so any strategy with an incomplete test pair re-tests. Attempt
            # cache-hits (crf-embedded filenames) make an unchanged-q re-test
            # near-free re-promotion; a q change encodes only the new value —
            # no q is persisted for comparison (attempts are the substrate).
            incomplete: set[str] = set()
            for row in rows:
                if isinstance(row.payload, EncodedChunk) and row.state != ArtifactState.COMPLETE:
                    incomplete.add(row.payload.strategy.display_name())
            self._strategies_to_test = [
                s for s in strategies if s.display_name() in incomplete
            ]
        else:
            self._strategies_to_test = [
                s for s in strategies if s.display_name() not in cached_results
            ]

        if (
            not fixed
            and not self._strategies_to_test
            and cached_results
            and persisted is not None
            and persisted.tolerance_pct != tolerance
        ):
            self._tolerance_reapply = True

        # All cached with nothing to test → current; seed the reused payload.
        if not self._strategies_to_test and cached_results and persisted is not None:
            self._strategy_results = persisted.strategy_results
            if fixed:
                # Re-select by pruning (tolerance is void in fixed mode) and
                # re-derive the anchor ruler from the persisted measurements.
                resolved_names = [s.display_name() for s in strategies]
                self._selected_names = self._dominance_survivors(persisted.strategy_results)
                self._anchor_name = self._select_anchor(
                    persisted.strategy_results, resolved_names, self._selected_names,
                )
                anchor_result = next(
                    (r for r in persisted.strategy_results if r.strategy == self._anchor_name),
                    None,
                )
                self._synthetic_targets = self._synthetic_targets_from(anchor_result)
            else:
                self._selected_names = persisted.selected or self._apply_tolerance(
                    persisted.strategy_results, tolerance,
                )

        # Searched: fatal only when work is actually pending. Fixed:
        # _recover only runs for compared runs (single-strategy and
        # optimize-off take the all-strategies skip path) — a compared run
        # without test chunks cannot prune and always fails loudly.
        if not self._test_chunks and (self._strategies_to_test or fixed):
            raise RecoveryError("No chunks available from ChunkingPhase")
        if self._tolerance_reapply:
            # Every pair is COMPLETE but the tolerance is stale — cheap
            # re-select work. Settings staleness, not artifact presence:
            # pending is set explicitly (the ledger alone would read current).
            return Recovery(artifacts=rows, pending=True)
        return Recovery.from_artifacts(rows)

    def _resolve_test_chunks(self, persisted: OptimizationParams | None) -> list[VideoStreamChunk]:
        """The test-chunk set: the persisted selection, or a fresh pick.

        The fresh selection is stashed here (recovery) so ``_execute`` uses
        the same set that produced the ledger counts.
        """
        chunks: list[VideoStreamChunk] = [
            a.payload for a in self._dep_result(ChunkingPhase).chunks
        ]

        test_ids = persisted.test_chunks if persisted is not None and persisted.test_chunks else []
        if test_ids:
            by_id = {c.safe_name(): c for c in chunks}
            selected = [by_id[i] for i in test_ids if i in by_id]
            if selected:
                return selected
            logger.warning("Persisted test chunk IDs not found — re-selecting")
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
        """Run test encodes for pending strategies, or re-apply the tolerance.

        The top-level ``optimization`` span belongs to the template and covers
        everything here. Test encodes run through the shared encoder machinery
        with ``metric_prefix=optimization``, so their dotted keys land under
        ``optimization.<strategy>`` / ``optimization.quality_measure``.
        ``dry_run`` is never ``True`` here (optimization is not a
        readonly-execute phase; the template previews instead).

        Args:
            wanted:  The wanted artifact list from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``OptimizationPhaseResult`` with ``selected_strategies`` set.
        """
        job_result = self._dep_result(JobPhase)
        work_dir   = job_result.work_dir
        opt_yaml   = work_dir / OptimizationPhase.SIDECAR_NAME
        tolerance  = self._config.encoding.optimize_tolerance
        persisted  = self._persisted
        fixed      = self._dep_result(JobPhase).plan.fixed_quality
        assert self._current_probe is not None, "_recover populates the probe state before execution"
        crop       = self._current_probe.crop

        current_targets  = targets_as_strings(self._dep_result(JobPhase).plan.targets)
        current_sampling = self._config.measurement.sampling

        # Cheap path: all results cached, only the tolerance changed —
        # re-select without re-encoding.
        if self._tolerance_reapply and persisted is not None:
            logger.info(
                "All strategy results cached; tolerance changed (%.1f%% → %.1f%%) — re-selecting without re-encoding",
                persisted.tolerance_pct, tolerance,
            )
            selected = self._apply_tolerance(persisted.strategy_results, tolerance)
            OptimizationParams(
                probe            = persisted.probe,
                test_chunks      = persisted.test_chunks,
                strategy_results = persisted.strategy_results,
                tolerance_pct    = tolerance,
                selected         = selected,
                quality_targets  = current_targets,
                sampling = current_sampling,
            ).save(opt_yaml)
            self._selected_names   = selected
            self._strategy_results = persisted.strategy_results
            self._log_optimization_summary(persisted.strategy_results, selected)
            rows = self._pair_ledger(work_dir, self._dep_result(JobPhase).plan.strategies)
            return self._make_result(
                PhaseOutcome.COMPLETED,
                [r for r in rows if r.wanted],
                "tolerance re-applied from cached results",
            )

        cached_results     = self._cached_results
        strategies_to_test = self._strategies_to_test

        # The test-chunk set was resolved by recovery (persisted selection or
        # the fresh pick that produced the ledger counts).
        test_chunks = self._test_chunks
        if strategies_to_test and not test_chunks:
            err = "No chunks available from ChunkingPhase"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        # Persist test chunk selection early (before encoding starts).
        OptimizationParams(
            probe            = self._current_probe,
            test_chunks      = [c.safe_name() for c in test_chunks],
            strategy_results = list(cached_results.values()),
            tolerance_pct    = tolerance,
            selected         = [],
            quality_targets  = current_targets,
            sampling = current_sampling,
        ).save(opt_yaml)

        # Run test encodes for all pending strategies in parallel (unified
        # pool; the top-level span belongs to the template).
        encoder       = _make_encoder(
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
        from pyqenc.phases.encoding import (
            _encode_chunks_parallel,
            _recover_encoding_attempts,
        )  # deferred: circular import (encoding <-> optimization)

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
                    quality_targets   = [] if fixed else self._dep_result(JobPhase).plan.targets,
                    max_parallel      = self._config.encoding.concurrency,
                    force             = False,
                    collector         = self._collector,
                    phase_recovery    = phase_recovery,
                    advance           = advance,
                    metric_prefix     = MetricKey.OPTIMIZATION,
                )
            )
            advance(0, AdvanceState.COMPLETE)

        # Derive per-strategy results from encoded output. Fixed mode derives
        # every strategy fresh from the current disk state (the winners'
        # result sidecars) — persisted results are never trusted there,
        # because no q value is recorded to prove them current.
        result_strategies = (
            self._dep_result(JobPhase).plan.strategies if fixed else strategies_to_test
        )
        new_results: list[StrategyTestResult] = []
        for strategy in result_strategies:
            winners_by_chunk = {
                w.chunk.safe_name(): w
                for w in enc_result.encoded_chunks.get(strategy.display_name(), [])
            }
            file_sizes: list[float] = []
            for chunk in test_chunks:
                encoded = winners_by_chunk.get(chunk.safe_name())
                if encoded is not None and encoded.stream.stream.file.path.exists():
                    file_sizes.append(encoded.stream.stream.file.file_size_bytes or 0)
            new_results.append(StrategyTestResult(
                strategy     = strategy.display_name(),
                total_size    = int(sum(file_sizes)),
                metrics      = self._aggregate_strategy_metrics(
                    work_dir, test_chunks, strategy, enc_result.encoded_chunks,
                ) if fixed else {},
            ))

        resolved_names = [s.display_name() for s in self._dep_result(JobPhase).plan.strategies]

        if fixed:
            # Selection = dominance pruning; anchor = smallest survivor (the
            # only front member selectable without a quality opinion);
            # synthetic set = the anchor's min-aggregated metrics. The sidecar
            # persists facts (strategy_results) + decisions (selected, anchor)
            # only — the ruler re-derives on read, so a changed comparison
            # stat set re-projects old measurements correctly.
            final_results = new_results
            selected      = self._dominance_survivors(final_results)
            self._anchor_name = self._select_anchor(final_results, resolved_names, selected)
            anchor_result = next(
                (r for r in final_results if r.strategy == self._anchor_name), None,
            )
            self._synthetic_targets = self._synthetic_targets_from(anchor_result)

            OptimizationParams(
                probe            = self._current_probe,
                test_chunks      = [c.safe_name() for c in test_chunks],
                strategy_results = final_results,
                tolerance_pct    = tolerance,
                selected         = selected,
                quality_targets  = current_targets,
                sampling         = current_sampling,
                anchor           = self._anchor_name,
            ).save(opt_yaml)

            self._selected_names   = selected
            self._strategy_results = final_results

            self._log_fixed_comparison(final_results, selected, self._anchor_name, resolved_names)

            rows = self._pair_ledger(work_dir, self._dep_result(JobPhase).plan.strategies)
            return self._make_result(
                PhaseOutcome.COMPLETED,
                [r for r in rows if r.wanted],
                f"{len(selected)} survivor(s) selected",
            )

        all_results: list[StrategyTestResult] = list(cached_results.values()) + new_results

        # Sort final results by size and select strategies.
        final_results = sorted(all_results, key=lambda r: r.total_size)
        selected      = self._apply_tolerance(final_results, tolerance)

        # Persist final state with current quality targets and sampling.
        OptimizationParams(
            probe            = self._current_probe,
            test_chunks      = [c.safe_name() for c in test_chunks],
            strategy_results = final_results,
            tolerance_pct    = tolerance,
            selected         = selected,
            quality_targets  = current_targets,
            sampling = current_sampling,
        ).save(opt_yaml)

        self._selected_names   = selected
        self._strategy_results = final_results

        self._log_optimization_summary(final_results, selected)

        rows = self._pair_ledger(work_dir, self._dep_result(JobPhase).plan.strategies)
        return self._make_result(
            PhaseOutcome.COMPLETED,
            [r for r in rows if r.wanted],
            f"{len(selected)} strategy(ies) selected",
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> OptimizationPhaseResult:
        """Build the reused result from the cached strategy results stash."""
        if self._dep_result(JobPhase).plan.fixed_quality:
            resolved_names = [s.display_name() for s in self._dep_result(JobPhase).plan.strategies]
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
        # Resolve strategy name strings to Strategy objects from the live config.
        by_name = {s.display_name(): s for s in self._dep_result(JobPhase).plan.strategies}
        return OptimizationPhaseResult(
            outcome             = outcome,
            message             = message,
            winners             = [
                r for r in artifacts
                if isinstance(r.payload, EncodedChunk) and r.state == ArtifactState.COMPLETE
            ],
            selected_strategies = [by_name[n] for n in self._selected_names if n in by_name],
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
        work_dir         = self._dep_result(JobPhase).work_dir
        opt_yaml         = work_dir / OptimizationPhase.SIDECAR_NAME
        current_targets  = targets_as_strings(self._dep_result(JobPhase).plan.targets)
        current_sampling = self._config.measurement.sampling

        if not dry_run:
            persisted = OptimizationParams.load(opt_yaml)
            params_stale = (
                persisted is not None
                and (
                    (bool(persisted.quality_targets) and persisted.quality_targets != current_targets)
                    or (persisted.sampling is not None and persisted.sampling != current_sampling)
                )
            )
            if params_stale:
                logger.info(
                    "All-strategies mode: quality targets or metrics sampling changed"
                    " — wiping encoded/ dirs"
                )
                _wipe_encoded_dir(work_dir, self._dep_result(JobPhase).plan.strategies)
            elif persisted is not None:
                logger.debug(
                    "All-strategies mode: params unchanged (sampling=%s, targets=%s) — encoded/ kept",
                    persisted.sampling, persisted.quality_targets,
                )

            # Always write optimization.yaml with current targets and sampling
            work_dir.mkdir(parents=True, exist_ok=True)
            OptimizationParams(
                probe            = None,
                test_chunks      = [],
                strategy_results = [],
                tolerance_pct    = 0.0,
                selected         = [s.display_name() for s in self._dep_result(JobPhase).plan.strategies],
                quality_targets  = current_targets,
                sampling = current_sampling,
            ).save(opt_yaml)

        return OptimizationPhaseResult(
            outcome             = PhaseOutcome.REUSED,
            message             = "all-strategies mode — skipping optimization",
            selected_strategies = list(self._dep_result(JobPhase).plan.strategies),
        )

    def _wipe_artifacts(self, work_dir: Path) -> None:
        """Delete optimization test artifacts and ``optimization.yaml``.

        Removes the ``encoded/`` directory (test encode workspace) and the
        ``optimization.yaml`` parameter file.

        Args:
            work_dir: Pipeline working directory.
        """
        opt_yaml = work_dir / OptimizationPhase.SIDECAR_NAME
        if opt_yaml.exists():
            opt_yaml.unlink()
            logger.debug("force_wipe: deleted %s", opt_yaml)

        # Delete test encode artifacts (stored under encoding/ per strategy)
        encoding_dir = work_dir / ENCODING_WORKSPACE_DIR
        if encoding_dir.exists():
            shutil.rmtree(encoding_dir)
            logger.debug("force_wipe: deleted %s", encoding_dir)

    @staticmethod
    def _apply_tolerance(
        results:       list[StrategyTestResult],
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
    def _dominates(a: StrategyTestResult, b: StrategyTestResult) -> bool:
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
    def _dominance_survivors(results: list[StrategyTestResult]) -> list[str]:
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
        results:        list[StrategyTestResult],
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
    def _synthetic_targets_from(anchor: StrategyTestResult | None) -> list[QualityTarget]:
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
        from pyqenc.constants import ENCODED_ATTEMPT_NAME_PATTERN
        from pyqenc.phases.encoding import _encoded_dir, _read_sidecar_yaml

        winners_by_chunk = {
            w.chunk.safe_name(): w
            for w in encoded_chunks.get(strategy.display_name(), [])
        }
        mins: dict[str, float] = {}
        for chunk in test_chunks:
            winner = winners_by_chunk.get(chunk.safe_name())
            if winner is None:
                continue
            name_match = ENCODED_ATTEMPT_NAME_PATTERN.match(winner.stream.stream.file.path.name)
            if name_match is None:
                continue
            sidecar = _read_sidecar_yaml(
                _encoded_dir(work_dir, strategy)
                / f"{name_match.group('chunk_id')}.{name_match.group('resolution')}.yaml"
            )
            if sidecar is None:
                continue
            for key, value in sidecar.get("metrics", {}).items():
                measured = float(value)
                mins[key] = min(mins[key], measured) if key in mins else measured
        return mins

    def _log_optimization_summary(
        self,
        results:  list[StrategyTestResult],
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
        results:        list[StrategyTestResult],
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
        results:        list[StrategyTestResult],
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



