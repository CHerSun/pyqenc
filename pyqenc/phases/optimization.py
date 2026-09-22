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
"""
# CHerSun 2026

from __future__ import annotations

import asyncio
import logging
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from alive_progress import config_handler

from pyqenc.constants import (
    CHUNKS_DIR,
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import (
    ChunkMetadata,
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    Strategy,
)
from pyqenc.phase import (
    Artifact,
    Phase,
    PhaseBase,
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
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.visualization import QualityEvaluator

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector
    from pyqenc.phases.encoding import ChunkEncoder

config_handler.set_global(enrich_print=False)  # type: ignore
logger = logging.getLogger(__name__)

_OPTIMIZATION_YAML    = "optimization.yaml"


# ---------------------------------------------------------------------------
# OptimizationPhaseResult
# ---------------------------------------------------------------------------

@dataclass
class OptimizationPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying optimization-specific payload.

    Attributes:
        selected_strategies: Strategies selected as optimal (or all strategies
                             in all-strategies mode).
        strategy_results:    Per-strategy test results; empty in all-strategies mode.
    """

    selected_strategies: list[Strategy] = field(default_factory=list)
    strategy_results:    list[StrategyTestResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# OptimizationPhase
# ---------------------------------------------------------------------------

class OptimizationPhase(PhaseBase):
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
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ProbePhase, ChunkingPhase)
    _METRIC_KEY: MetricKey = MetricKey.OPTIMIZATION

    def __init__(
        self,
        config:    AppConfig,
        phases:    dict[type[Phase], Phase] | None = None,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        # Recovery stash — resolved during _recover(), consumed by
        # _execute()/_reused_result()/_make_result().
        self._persisted:         OptimizationParams | None          = None
        self._cached_results:    dict[str, StrategyTestResult]     = {}
        self._strategies_to_test: list[Strategy]                   = []
        self._tolerance_reapply: bool                              = False
        self._selected_names:    list[str]                         = []
        self._strategy_results:  list[StrategyTestResult]          = []
        self._current_probe:     ProbeState | None                 = None

    # ------------------------------------------------------------------
    # PhaseBase hooks
    # ------------------------------------------------------------------

    def _skip_check(self, dry_run: bool) -> OptimizationPhaseResult | None:
        """All-strategies mode: skip test encodes entirely (no banner).

        The mode decision reads constructor state (config) only. The skip
        path itself needs dependency state for its ``optimization.yaml``
        bookkeeping, so it resolves dependencies itself (memoized — when the
        phase is reached through a dependency chain they have already run).

        Args:
            dry_run: When ``True``, skip the ``optimization.yaml`` write.

        Returns:
            The all-strategies result, a FAILED result when no strategies are
            configured or a dependency short-circuits, or ``None`` in
            optimization mode (proceed with the template).
        """
        strategies = self._config.encoding.resolved_strategies
        if not strategies:
            err = "No strategies configured"
            logger.error(err)
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)

        # All-strategies mode: triggered by the optimize flag being off or a
        # single strategy given (nothing to optimize against).
        if self._config.encoding.optimize and len(strategies) > 1:
            return None

        dep_result = self._ensure_dependencies(dry_run=dry_run)
        if dep_result is not None:
            return dep_result
        return self._all_strategies(dry_run)

    def _log_key_params(self) -> None:
        """Log the strategy list and tolerance (key parameters)."""
        logger.info("Strategies:  %s", ", ".join(s.name for s in self._config.encoding.resolved_strategies))
        logger.info("Tolerance:   %.1f%%", self._config.encoding.optimize_tolerance)

    def _recovery_unit(self) -> str:
        """The recovery summary counts strategy results."""
        return "strategy result"

    def _recover(self) -> Recovery:
        """Resolve optimization state currency: invalidations first, then caches.

        Steps:

        1. ``force_wipe`` (from JobPhase) → delete ``optimization.yaml`` and
           the ``encoding/`` test workspace.
        2. Probe mismatch against ``optimization.yaml`` — fatal without
           ``--force`` (handled in step 1 when forced).
        3. Quality-target / ``metrics_sampling`` change → wipe ``encoded/``
           result dirs and treat all cached strategy results as stale.
        4. All results cached with a differing tolerance → cheap pending work
           (re-select without re-encoding).
        5. Build one artifact per strategy result: cached → COMPLETE,
           still-to-test → ABSENT.

        Returns:
            The :class:`Recovery` single source of truth.

        Raises:
            RecoveryError: On a probe change without ``--force``, or when
                ChunkingPhase produced no chunks.
        """
        job_result   = self._dep(JobPhase).result  # type: ignore[union-attr]
        probe_result = self._dep(ProbePhase).result  # type: ignore[union-attr]
        work_dir     = job_result.work_dir
        opt_yaml     = work_dir / _OPTIMIZATION_YAML
        tolerance    = self._config.encoding.optimize_tolerance
        strategies   = self._config.encoding.resolved_strategies
        force_wipe   = getattr(job_result, "force_wipe", False)

        crop           = probe_result.crop
        current_probe  = ProbeState(
            frame_count = probe_result.source.frame_count if probe_result.source else 0,
            crop        = crop if crop else None,
        )
        self._current_probe = current_probe

        # Step 1 — force wipe (before any currency decision, so --force
        # always re-tests).
        persisted: OptimizationParams | None = OptimizationParams.load(opt_yaml)
        if force_wipe:
            self._wipe_artifacts(work_dir)
            persisted = None

        # Step 2 — probe mismatch invalidation.
        if persisted is not None and persisted.strategy_results:
            if persisted.probe != current_probe:
                raise RecoveryError(
                    "Probe params changed since last optimization run "
                    f"(persisted={persisted.probe}, current={current_probe}). "
                    "Re-run with --force to delete stale optimization artifacts and continue."
                )

        # Step 3 — quality-target / metrics_sampling change detection.
        current_targets  = _targets_as_strings(self._config.encoding.resolved_targets)
        current_sampling = self._config.measurement.sampling
        targets_changed = (
            persisted is not None
            and bool(persisted.quality_targets)
            and persisted.quality_targets != current_targets
        )
        sampling_changed = (
            persisted is not None
            and persisted.metrics_sampling is not None
            and persisted.metrics_sampling != current_sampling
        )
        if (targets_changed or sampling_changed) and persisted is not None and persisted.strategy_results:
            if sampling_changed:
                logger.debug(
                    "metrics_sampling changed (%d → %d) — wiping encoded/ dirs",
                    persisted.metrics_sampling, current_sampling,
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
                metrics_sampling = persisted.metrics_sampling,
            )
        self._persisted = persisted

        # Step 4 — all cached, only tolerance changed → cheap re-select work.
        cached_results: dict[str, StrategyTestResult] = {}
        if persisted is not None:
            for r in persisted.strategy_results:
                cached_results[r.strategy_name] = r
        self._cached_results = cached_results

        self._strategies_to_test = [s for s in strategies if s.name not in cached_results]
        if (
            not self._strategies_to_test
            and cached_results
            and persisted is not None
            and persisted.tolerance_pct != tolerance
        ):
            self._tolerance_reapply = True
            return Recovery(
                artifacts=_strategy_artifacts(list(cached_results.keys())),
                pending=True,
            )

        # All cached with matching tolerance → current; seed the reused payload.
        if not self._strategies_to_test and cached_results and persisted is not None:
            self._strategy_results = persisted.strategy_results
            self._selected_names   = persisted.selected or self._apply_tolerance(
                persisted.strategy_results, tolerance,
            )

        # Step 5 — one artifact per strategy result.
        artifacts = _strategy_artifacts(
            complete_names = list(cached_results.keys()),
            absent_names   = [s.name for s in self._strategies_to_test],
        )
        return Recovery.from_artifacts(artifacts)

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
        job_result = self._dep(JobPhase).result  # type: ignore[union-attr]
        work_dir   = job_result.work_dir
        opt_yaml   = work_dir / _OPTIMIZATION_YAML
        tolerance  = self._config.encoding.optimize_tolerance
        persisted  = self._persisted
        crop       = self._current_probe.crop if self._current_probe else None

        current_targets  = _targets_as_strings(self._config.encoding.resolved_targets)
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
                metrics_sampling = current_sampling,
            ).save(opt_yaml)
            self._selected_names   = selected
            self._strategy_results = persisted.strategy_results
            self._log_optimization_summary(persisted.strategy_results, selected)
            return self._make_result(
                PhaseOutcome.COMPLETED, [],
                "tolerance re-applied from cached results",
            )

        cached_results     = self._cached_results
        strategies_to_test = self._strategies_to_test

        # Test encodes need chunks from ChunkingPhase.
        chunking_result = self._dep(ChunkingPhase).result  # type: ignore[union-attr]
        chunks: list[ChunkMetadata] = getattr(chunking_result, "chunks", [])
        if strategies_to_test and not chunks:
            err = "No chunks available from ChunkingPhase"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)

        test_chunk_ids = persisted.test_chunks if persisted and persisted.test_chunks else []
        if test_chunk_ids:
            chunk_by_id = {c.chunk_id: c for c in chunks}
            test_chunks = [chunk_by_id[cid] for cid in test_chunk_ids if cid in chunk_by_id]
            if not test_chunks:
                logger.warning("Persisted test chunk IDs not found — re-selecting")
                test_chunks = _select_test_chunks(chunks)
        else:
            test_chunks = _select_test_chunks(chunks)

        # Persist test chunk selection early (before encoding starts).
        OptimizationParams(
            probe            = self._current_probe,
            test_chunks      = [c.chunk_id for c in test_chunks],
            strategy_results = list(cached_results.values()),
            tolerance_pct    = tolerance,
            selected         = [],
            quality_targets  = current_targets,
            metrics_sampling = current_sampling,
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
        reference_dir = work_dir / CHUNKS_DIR

        test_chunk_seconds = sum(c.end_timestamp - c.start_timestamp for c in test_chunks)
        total_seconds      = test_chunk_seconds * len(strategies_to_test)
        total_count        = len(test_chunks) * len(strategies_to_test)

        test_chunk_ids = [c.chunk_id for c in test_chunks]
        strategy_names = [s.name for s in strategies_to_test]
        from pyqenc.phases.encoding import (
            _encode_chunks_parallel,
            _recover_encoding_attempts,
        )

        phase_recovery = _recover_encoding_attempts(work_dir, test_chunk_ids, strategy_names)

        with ProgressBar(total_seconds, title="Optimization", total_count=total_count) as advance:
            # Pre-advance bar for already-complete pairs
            chunks_by_id = {c.chunk_id: c for c in test_chunks}
            for r in phase_recovery.pairs.values():
                if r.state == ArtifactState.COMPLETE:
                    advance((chunks_by_id[r.chunk_id].end_timestamp - chunks_by_id[r.chunk_id].start_timestamp), AdvanceState.SKIPPED)

            enc_result = asyncio.run(
                _encode_chunks_parallel(
                    encoder           = encoder,
                    chunks            = test_chunks,
                    reference_dir     = reference_dir,
                    strategies        = strategies_to_test,
                    quality_targets   = self._config.encoding.resolved_targets,
                    max_parallel      = self._config.encoding.concurrency,
                    force             = False,
                    collector         = self._collector,
                    phase_recovery    = phase_recovery,
                    advance           = advance,
                    metric_prefix     = MetricKey.OPTIMIZATION,
                )
            )
            advance(0, AdvanceState.COMPLETE)

        # Derive per-strategy results from encoded output
        new_results: list[StrategyTestResult] = []
        for strategy in strategies_to_test:
            file_sizes: list[float] = []
            for chunk in test_chunks:
                encoded_path = enc_result.encoded_chunks.get(chunk.chunk_id, {}).get(strategy.name)
                if encoded_path is not None and encoded_path.exists():
                    file_sizes.append(encoded_path.stat().st_size)
            new_results.append(StrategyTestResult(
                strategy_name = strategy.name,
                total_size    = int(sum(file_sizes)),
            ))

        all_results: list[StrategyTestResult] = list(cached_results.values()) + new_results

        # Sort final results by size and select strategies.
        final_results = sorted(all_results, key=lambda r: r.total_size)
        selected      = self._apply_tolerance(final_results, tolerance)

        # Persist final state with current quality targets and sampling.
        OptimizationParams(
            probe            = self._current_probe,
            test_chunks      = [c.chunk_id for c in test_chunks],
            strategy_results = final_results,
            tolerance_pct    = tolerance,
            selected         = selected,
            quality_targets  = current_targets,
            metrics_sampling = current_sampling,
        ).save(opt_yaml)

        self._selected_names   = selected
        self._strategy_results = final_results

        self._log_optimization_summary(final_results, selected)

        return self._make_result(
            PhaseOutcome.COMPLETED, [],
            f"{len(selected)} strategy(ies) selected",
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> OptimizationPhaseResult:
        """Build the reused result from the cached strategy results stash."""
        self._log_optimization_summary(self._strategy_results, self._selected_names)
        return self._make_result(
            PhaseOutcome.REUSED, [], "all strategy results reused",
        )

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact],
        message:   str,
        error:     str | None = None,
    ) -> OptimizationPhaseResult:
        """Assemble an ``OptimizationPhaseResult`` from the payload stashes.

        Args:
            outcome:   The phase outcome.
            artifacts: Artifact list (empty on non-execute paths).
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result (``selected_strategies`` resolved from the
            live config; ``None``-safe on dep-failure paths where no stash
            exists).
        """
        return OptimizationPhaseResult(
            outcome             = outcome,
            artifacts           = artifacts,
            message             = message,
            error               = error,
            selected_strategies = self._resolve_selected(self._selected_names),
            strategy_results    = list(self._strategy_results),
        )

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def _resolve_selected(self, selected_names: list[str]) -> list[Strategy]:
        """Resolve strategy name strings to ``Strategy`` objects from the live config.

        Args:
            selected_names: Strategy display names from ``_apply_tolerance``.

        Returns:
            Matching ``Strategy`` objects from the resolved strategies,
            preserving the order of *selected_names*.
        """
        by_name = {s.name: s for s in self._config.encoding.resolved_strategies}
        return [by_name[n] for n in selected_names if n in by_name]

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
        job_result       = self._dep(JobPhase).result  # type: ignore[union-attr]
        work_dir         = job_result.work_dir
        opt_yaml         = work_dir / _OPTIMIZATION_YAML
        current_targets  = _targets_as_strings(self._config.encoding.resolved_targets)
        current_sampling = self._config.measurement.sampling

        if not dry_run:
            persisted = OptimizationParams.load(opt_yaml)
            params_stale = (
                persisted is not None
                and (
                    (bool(persisted.quality_targets) and persisted.quality_targets != current_targets)
                    or (persisted.metrics_sampling is not None and persisted.metrics_sampling != current_sampling)
                )
            )
            if params_stale:
                logger.info(
                    "All-strategies mode: quality targets or metrics_sampling changed"
                    " — wiping encoded/ dirs"
                )
                _wipe_encoded_dir(work_dir, self._config.encoding.resolved_strategies)
            elif persisted is not None:
                logger.debug(
                    "All-strategies mode: params unchanged (sampling=%s, targets=%s) — encoded/ kept",
                    persisted.metrics_sampling, persisted.quality_targets,
                )

            # Always write optimization.yaml with current targets and sampling
            work_dir.mkdir(parents=True, exist_ok=True)
            OptimizationParams(
                probe            = None,
                test_chunks      = [],
                strategy_results = [],
                tolerance_pct    = 0.0,
                selected         = [s.name for s in self._config.encoding.resolved_strategies],
                quality_targets  = current_targets,
                metrics_sampling = current_sampling,
            ).save(opt_yaml)

        return OptimizationPhaseResult(
            outcome             = PhaseOutcome.REUSED,
            artifacts           = [],
            message             = "all-strategies mode — skipping optimization",
            selected_strategies = list(self._config.encoding.resolved_strategies),
            strategy_results    = [],
        )

    def _wipe_artifacts(self, work_dir: Path) -> None:
        """Delete optimization test artifacts and ``optimization.yaml``.

        Removes the ``encoded/`` directory (test encode workspace) and the
        ``optimization.yaml`` parameter file.

        Args:
            work_dir: Pipeline working directory.
        """
        opt_yaml = work_dir / _OPTIMIZATION_YAML
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

        return [r.strategy_name for r in successful if r.total_size <= threshold]

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
            size_mb  = res.total_size / (1024 * 1024) if res.total_size > 0 else 0.0
            size_str = f"{size_mb:,.1f}".replace(",", "\u202f")
            marker   = " ◀ selected" if res.strategy_name in selected_names else ""
            status   = "passed" if res.total_size > 0 else "failed"
            logger.info(
                "  %-30s  %12s  %8s%s",
                res.strategy_name[:30], size_str, status, marker,
            )

        logger.info("")
        if selected:
            logger.info("Selected strategies: %s", ", ".join(selected))
        else:
            logger.critical("NO strategies selected (all failed).")

        logger.info("")

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _strategy_artifacts(
    complete_names: list[str],
    absent_names:   list[str] | None = None,
) -> list[Artifact]:
    """Build a lightweight ``Artifact`` list representing strategy results.

    The optimization phase tracks strategy test results rather than a standard
    on-disk artifact list, so this adapts them to the artifact model consumed by
    ``log_recovery_line()`` (which reads only ``.wanted`` and ``.state``). Every
    strategy result is ``wanted=True``; cached results are ``COMPLETE`` and
    strategies still needing a test encode are ``ABSENT``. The synthetic path is
    derived from the strategy name purely for readability.

    Args:
        complete_names: Names of strategies with cached (finished) results.
        absent_names:   Names of strategies still requiring a test encode.

    Returns:
        Combined artifact list, complete entries first then absent entries.
    """
    artifacts:  list[Artifact] = [
        Artifact(path=Path(name), state=ArtifactState.COMPLETE, wanted=True)
        for name in complete_names
    ]
    artifacts += [
        Artifact(path=Path(name), state=ArtifactState.ABSENT, wanted=True)
        for name in (absent_names or [])
    ]
    return artifacts


def _targets_as_strings(quality_targets: list[QualityTarget]) -> list[str]:
    """Serialise *quality_targets* to ``"metric-statistic:value"`` strings.

    Args:
        quality_targets: Quality targets from :class:`~pyqenc.app_config.AppConfig`.

    Returns:
        Sorted list of strings like ``["vmaf-min:93.0"]``.
    """
    return sorted(f"{t.metric}-{t.statistic}:{t.value}" for t in quality_targets)


def _wipe_encoded_dir(work_dir: Path, strategies: list[Strategy]) -> None:
    """Delete the entire ``encoded/<strategy>/`` directory for each strategy.

    All contents are hard-linked winning attempts and result sidecars — no
    unique data lives here.  ``EncodingPhase._recover()`` will re-discover
    attempts from ``encoding/`` and re-evaluate them from scratch.

    Called when quality targets or ``metrics_sampling`` change, since both
    invalidate the previously selected winners and their recorded metrics.

    If the strategy list does not cover all subdirs present (e.g. strategies
    were renamed), wipes the entire ``encoded/`` base directory as a fallback
    to guarantee no stale data remains.

    Args:
        work_dir:   Pipeline working directory.
        strategies: All configured strategies (safe_name used for directory lookup).
    """
    encoded_base = work_dir / ENCODED_OUTPUT_DIR
    if not encoded_base.exists():
        return

    # Collect all existing strategy subdirs
    existing_dirs = [d for d in encoded_base.iterdir() if d.is_dir()]
    expected_names = {s.safe_name for s in strategies}
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
        strategy_dir = encoded_base / strategy.safe_name
        if not strategy_dir.exists():
            continue
        try:
            shutil.rmtree(strategy_dir)
            logger.debug("Wiped stale encoded dir: %s", strategy_dir)
        except OSError as exc:
            logger.warning("Could not wipe encoded dir %s: %s", strategy_dir, exc)


def _select_test_chunks(
    chunks:                list[ChunkMetadata],
    percentage:            float = 0.01,
    min_chunks:            int   = 3,
    exclude_start_percent: float = 0.10,
    exclude_end_percent:   float = 0.10,
) -> list[ChunkMetadata]:
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
    selected.sort(key=lambda c: c.chunk_id)
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
    converges.

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
    )



