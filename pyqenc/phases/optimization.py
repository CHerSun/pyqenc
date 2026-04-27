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
from typing import TYPE_CHECKING, cast

from alive_progress import config_handler

from pyqenc.constants import (
    CHUNKS_DIR,
    DEFAULT_METRICS_SAMPLING,
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import (
    ChunkMetadata,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    Strategy,
)
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.state import ArtifactState, OptimizationParams, StrategyTestResult
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.log_format import (
    emit_phase_banner,
    log_recovery_line,
)
from pyqenc.utils.visualization import QualityEvaluator

if TYPE_CHECKING:
    from pyqenc.metrics import MetricsCollector
    from pyqenc.models import PipelineConfig

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

class OptimizationPhase:
    """Phase object for strategy optimization.

    In **all-strategies mode** (``config.optimize=False``), returns all
    configured strategies immediately without running any test encodes and
    without emitting any log messages.

    In **optimization mode** (``config.optimize=True``), runs test encodes on
    representative chunks, persists per-strategy results to
    ``optimization.yaml``, and selects strategies within the configured
    tolerance of the best result.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "optimization"

    def __init__(
        self,
        config:    "PipelineConfig",
        phases:    "dict[type[Phase], Phase] | None" = None,
        *,
        collector: "MetricsCollector",
    ) -> None:
        from pyqenc.phases.chunking import ChunkingPhase as _ChunkingPhase
        from pyqenc.phases.job import JobPhase as _JobPhase

        self._config    = config
        self._collector: "MetricsCollector" = collector
        self._job:      "_JobPhase | None"      = cast("_JobPhase",      phases[_JobPhase])      if phases else None
        self._chunking: "_ChunkingPhase | None" = cast("_ChunkingPhase", phases[_ChunkingPhase]) if phases else None
        self.result:    "OptimizationPhaseResult | None" = None
        self.dependencies: "list[Phase]" = [d for d in [self._job, self._chunking] if d is not None]

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> "OptimizationPhaseResult":
        """Classify existing optimization artifacts without executing any work.

        In all-strategies mode, returns all configured strategies immediately.
        In optimization mode, loads ``optimization.yaml`` and classifies the
        cached results.  If quality targets changed since the last run, returns
        ``DRY_RUN`` to signal that ``run()`` must be called.

        Returns:
            ``OptimizationPhaseResult`` with current artifact state.
        """
        if self.result is not None:
            return self.result

        if not self._config.strategies:
            raise RuntimeError("no strategies configured")

        # All-strategies mode: no artifacts, just return all strategies. Triggered by either flag or single strategy given (noting to optimize)
        if not self._config.optimize or len(self._config.strategies) == 1:
            self.result = self._all_strategies_result()
            return self.result

        dep_result = self._ensure_dependencies(execute=False)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        opt_yaml  = self._config.work_dir / _OPTIMIZATION_YAML
        persisted = OptimizationParams.load(opt_yaml)

        if persisted is None or not persisted.strategy_results:
            self.result = OptimizationPhaseResult(
                outcome             = PhaseOutcome.DRY_RUN,
                artifacts           = [],
                message             = "optimization.yaml not found or empty",
                selected_strategies = [],
                strategy_results    = [],
            )
        else:
            current_targets  = _targets_as_strings(self._config.quality_targets)
            current_sampling = self._config.metrics_sampling
            params_stale = (
                (persisted.quality_targets and persisted.quality_targets != current_targets)
                or (persisted.metrics_sampling is not None and persisted.metrics_sampling != current_sampling)
            )
            if params_stale:
                # Quality targets or metrics_sampling changed — work needed; run() will invalidate encoded/ sidecars
                self.result = OptimizationPhaseResult(
                    outcome             = PhaseOutcome.DRY_RUN,
                    artifacts           = [],
                    message             = "quality targets or metrics_sampling changed — re-run needed",
                    selected_strategies = [],
                    strategy_results    = [],
                )
            else:
                selected = self._apply_tolerance(persisted.strategy_results, self._config.strategy_selection_tolerance)
                self.result = OptimizationPhaseResult(
                    outcome             = PhaseOutcome.REUSED,
                    artifacts           = [Artifact(
                        path  = self._config.work_dir / _OPTIMIZATION_YAML,
                        state = ArtifactState.COMPLETE,
                    )],
                    message             = f"optimization.yaml loaded — {len(selected)} strategy(ies) selected",
                    selected_strategies = self._resolve_selected(selected),
                    strategy_results    = persisted.strategy_results,
                )

        return self.result

    def run(self, dry_run: bool = False) -> "OptimizationPhaseResult":
        """Recover, run test encodes if needed, cache and return result.

        In all-strategies mode, returns all configured strategies immediately
        without any logging or test encodes.  Always writes ``optimization.yaml``
        with current quality targets so target-change detection works on the next
        run regardless of mode.

        In optimization mode:
        1. Emit phase banner.
        2. Ensure dependencies have results.
        3. Handle ``force_wipe`` from ``JobPhase``.
        4. Check quality-target change — delete ``encoded/`` result sidecars if changed.
        5. Check crop mismatch against ``optimization.yaml``.
        6. Check if all results are cached and only tolerance changed → re-select.
        7. Check if all results are cached with matching tolerance → reuse.
        8. Run test encodes for pending strategies.
        9. Persist results (including current quality targets) and select strategies.
        10. Log completion summary.

        Args:
            dry_run: When ``True``, report what would be done without writing files.

        Returns:
            ``OptimizationPhaseResult`` with ``selected_strategies`` set.
        """
        if not self._config.strategies:
            raise RuntimeError("no strategies configured")

        # All-strategies mode: no artifacts, just return all strategies. Triggered by either flag or single strategy given (noting to optimize)
        if not self._config.optimize or len(self._config.strategies) == 1:
            self.result = self._run_all_strategies(dry_run=dry_run)
            return self.result

        work_dir  = self._config.work_dir
        opt_yaml  = work_dir / _OPTIMIZATION_YAML
        tolerance = self._config.strategy_selection_tolerance

        # Step 1: load persisted optimization params (before dependency check so
        # tolerance re-application can short-circuit without needing live phases)
        persisted = OptimizationParams.load(opt_yaml)

        current_targets  = _targets_as_strings(self._config.quality_targets)
        current_sampling = self._config.metrics_sampling

        # Step 2: quality-target / metrics_sampling change detection — must happen before
        # tolerance re-application so we don't skip re-encoding when params changed.
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
        params_changed = targets_changed or sampling_changed
        if params_changed and persisted is not None and persisted.strategy_results:
            if sampling_changed:
                logger.debug(
                    "metrics_sampling changed (%d → %d) — wiping encoded/ dirs",
                    persisted.metrics_sampling, current_sampling,
                )
            # Wipe encoded/ for every strategy — contents are hard-linked attempts
            # and result sidecars; EncodingPhase will re-discover from encoding/.
            _wipe_encoded_dir(work_dir, self._config.strategies)
            # Treat all cached strategy results as stale — force re-encoding
            persisted = OptimizationParams(
                crop             = persisted.crop,
                test_chunks      = persisted.test_chunks,
                strategy_results = [],
                tolerance_pct    = persisted.tolerance_pct,
                selected         = [],
                quality_targets  = persisted.quality_targets,
                metrics_sampling = persisted.metrics_sampling,
            )

        # Step 3: tolerance re-application — all results cached, only tolerance changed
        # This is a pure read-from-cache operation; no dependencies needed.
        if (
            persisted is not None
            and persisted.strategy_results
            and len(persisted.strategy_results) == len(self._config.strategies)
            and persisted.tolerance_pct != tolerance
            and not params_changed
        ):
            emit_phase_banner("OPTIMIZATION", logger)
            logger.info("Strategies:  %s", ", ".join(s.name for s in self._config.strategies))
            logger.info("Tolerance:   %.1f%%", tolerance)
            logger.info(
                "All strategy results cached; tolerance changed (%.1f%% → %.1f%%) — re-selecting without re-encoding",
                persisted.tolerance_pct, tolerance,
            )
            with self._collector.time(MetricKey.RECOVERY):
                selected = self._apply_tolerance(persisted.strategy_results, tolerance)
                OptimizationParams(
                    crop             = persisted.crop,
                    test_chunks      = persisted.test_chunks,
                    strategy_results = persisted.strategy_results,
                    tolerance_pct    = tolerance,
                    selected         = selected,
                    quality_targets  = current_targets,
                    metrics_sampling = current_sampling,
                ).save(opt_yaml)
            log_recovery_line(logger, len(persisted.strategy_results), 0, unit="strategy result")
            self._log_optimization_summary(persisted.strategy_results, selected)
            self.result = OptimizationPhaseResult(
                outcome             = PhaseOutcome.REUSED,
                artifacts           = [Artifact(
                    path  = work_dir / _OPTIMIZATION_YAML,
                    state = ArtifactState.COMPLETE,
                )],
                message             = "tolerance re-applied from cached results",
                selected_strategies = self._resolve_selected(selected),
                strategy_results    = persisted.strategy_results,
            )
            return self.result

        # Step 4: check if all results already cached with matching tolerance
        if (
            persisted is not None
            and persisted.strategy_results
            and len(persisted.strategy_results) == len(self._config.strategies)
            and persisted.tolerance_pct == tolerance
            and not params_changed
        ):
            emit_phase_banner("OPTIMIZATION", logger)
            logger.info("Strategies:  %s", ", ".join(s.name for s in self._config.strategies))
            logger.info("Tolerance:   %.1f%%", tolerance)
            with self._collector.time(MetricKey.RECOVERY):
                selected = persisted.selected or self._apply_tolerance(persisted.strategy_results, tolerance)
            log_recovery_line(logger, len(persisted.strategy_results), 0, unit="strategy result")
            self._log_optimization_summary(persisted.strategy_results, selected)
            self.result = OptimizationPhaseResult(
                outcome             = PhaseOutcome.REUSED,
                artifacts           = [Artifact(
                    path  = work_dir / _OPTIMIZATION_YAML,
                    state = ArtifactState.COMPLETE,
                )],
                message             = "all strategy results reused",
                selected_strategies = self._resolve_selected(selected),
                strategy_results    = persisted.strategy_results,
            )
            return self.result

        # From here on we need live dependencies (for crop params and chunks)
        emit_phase_banner("OPTIMIZATION", logger)
        logger.info("Strategies:  %s", ", ".join(s.name for s in self._config.strategies))
        logger.info("Tolerance:   %.1f%%", self._config.strategy_selection_tolerance)

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)
        crop       = getattr(job_result, "crop", None)

        # Step 5: force-wipe
        if force_wipe:
            self._wipe_artifacts(work_dir)
            persisted = None

        # Step 6: crop mismatch check (reload persisted after potential wipe)
        if persisted is None:
            persisted = OptimizationParams.load(opt_yaml)

        if persisted is not None and persisted.strategy_results:
            if persisted.crop != crop:
                if self._config.force:
                    logger.warning(
                        "Crop params changed since last optimization run "
                        "(persisted=%s, current=%s) — --force: deleting optimization artifacts",
                        persisted.crop, crop,
                    )
                    self._wipe_artifacts(work_dir)
                    persisted = None
                else:
                    err = (
                        "Crop params changed since last optimization run "
                        f"(persisted={persisted.crop}, current={crop}). "
                        "Re-run with --force to delete stale optimization artifacts and continue."
                    )
                    logger.critical(err)
                    self.result = _failed(err)
                    return self.result

        # Step 7: determine which strategies still need test encodes
        cached_results: dict[str, StrategyTestResult] = {}
        if persisted is not None:
            for r in persisted.strategy_results:
                cached_results[r.strategy_name] = r

        strategies_to_test = [
            s for s in self._config.strategies
            if s.name not in cached_results
        ]
        complete_count = len(cached_results)
        pending_count  = len(strategies_to_test)
        log_recovery_line(logger, complete_count, pending_count, unit="strategy result")

        if dry_run:
            self.result = OptimizationPhaseResult(
                outcome             = PhaseOutcome.DRY_RUN if pending_count > 0 else PhaseOutcome.REUSED,
                artifacts           = [],
                message             = "dry-run",
                selected_strategies = [],
                strategy_results    = list(cached_results.values()),
            )
            return self.result

        # Step 8: get chunks from ChunkingPhase
        chunking_result = self._chunking.result  # type: ignore[union-attr]
        chunks: list[ChunkMetadata] = getattr(chunking_result, "chunks", [])
        if not chunks:
            err = "No chunks available from ChunkingPhase"
            logger.critical(err)
            self.result = _failed(err)
            return self.result

        # Step 9: select or restore test chunks
        test_chunk_ids = persisted.test_chunks if persisted and persisted.test_chunks else []
        if test_chunk_ids:
            chunk_by_id = {c.chunk_id: c for c in chunks}
            test_chunks = [chunk_by_id[cid] for cid in test_chunk_ids if cid in chunk_by_id]
            if not test_chunks:
                logger.warning("Persisted test chunk IDs not found — re-selecting")
                test_chunks = _select_test_chunks(chunks)
        else:
            test_chunks = _select_test_chunks(chunks)

        # Persist test chunk selection early (before encoding starts)
        OptimizationParams(
            crop             = crop,
            test_chunks      = [c.chunk_id for c in test_chunks],
            strategy_results = list(cached_results.values()),
            tolerance_pct    = tolerance,
            selected         = [],
            quality_targets  = current_targets,
            metrics_sampling = current_sampling,
        ).save(opt_yaml)

        # Step 10: run test encodes for all pending strategies in parallel (unified pool)
        encoder       = _make_encoder(work_dir, crop, self._config.visual_hash, self._config.metrics_sampling)
        reference_dir = work_dir / CHUNKS_DIR

        from pyqenc.phases.encoding import (
            _encode_chunks_parallel,
            _recover_encoding_attempts,
        )

        test_chunk_seconds = sum(c.end_timestamp - c.start_timestamp for c in test_chunks)
        total_seconds      = test_chunk_seconds * len(strategies_to_test)
        total_count        = len(test_chunks) * len(strategies_to_test)

        test_chunk_ids = [c.chunk_id for c in test_chunks]
        strategy_names = [s.name for s in strategies_to_test]
        phase_recovery = _recover_encoding_attempts(work_dir, test_chunk_ids, strategy_names)

        with self._collector.time(MetricKey.OPTIMIZATION), ProgressBar(total_seconds, title="Optimization", total_count=total_count) as advance:
            # Pre-advance bar for already-complete pairs
            chunks_by_id = {c.chunk_id: c for c in test_chunks}
            for r in phase_recovery.pairs.values():
                if r.state == ArtifactState.COMPLETE:
                    advance((chunks_by_id[r.chunk_id].end_timestamp - chunks_by_id[r.chunk_id].start_timestamp), AdvanceState.SKIPPED)

            enc_result = asyncio.run(
                _encode_chunks_parallel(
                    encoder         = encoder,
                    chunks          = test_chunks,
                    reference_dir   = reference_dir,
                    strategies      = strategies_to_test,
                    quality_targets = self._config.quality_targets,
                    max_parallel    = self._config.max_parallel,
                    force           = False,
                    collector       = self._collector,
                    phase_recovery  = phase_recovery,
                    advance         = advance,
                )
            )
            advance(0, AdvanceState.COMPLETE)

        # Derive per-strategy results from encoded output
        import yaml as _yaml
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

        # Step 11: sort final results by size and select strategies
        final_results = sorted(all_results, key=lambda r: r.total_size)
        selected      = self._apply_tolerance(final_results, tolerance)

        # Persist final state with current quality targets and sampling
        OptimizationParams(
            crop             = crop,
            test_chunks      = [c.chunk_id for c in test_chunks],
            strategy_results = final_results,
            tolerance_pct    = tolerance,
            selected         = selected,
            quality_targets  = current_targets,
            metrics_sampling = current_sampling,
        ).save(opt_yaml)

        self._log_optimization_summary(final_results, selected)

        self.result = OptimizationPhaseResult(
            outcome             = PhaseOutcome.COMPLETED,
            artifacts           = [Artifact(
                path  = work_dir / _OPTIMIZATION_YAML,
                state = ArtifactState.COMPLETE,
            )],
            message             = f"{len(selected)} strategy(ies) selected",
            selected_strategies = self._resolve_selected(selected),
            strategy_results    = final_results,
        )
        return self.result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_selected(self, selected_names: list[str]) -> list[Strategy]:
        """Resolve strategy name strings to ``Strategy`` objects from the live config.

        Args:
            selected_names: Strategy display names from ``_apply_tolerance``.

        Returns:
            Matching ``Strategy`` objects from ``self._config.strategies``,
            preserving the order of *selected_names*.
        """
        by_name = {s.name: s for s in self._config.strategies}
        return [by_name[n] for n in selected_names if n in by_name]

    def _all_strategies_result(self) -> "OptimizationPhaseResult":
        """Return all configured strategies silently (all-strategies mode, scan path)."""
        return OptimizationPhaseResult(
            outcome             = PhaseOutcome.REUSED,
            artifacts           = [],
            message             = "all-strategies mode — skipping optimization",
            selected_strategies = list(self._config.strategies),
            strategy_results    = [],
        )

    def _run_all_strategies(self, dry_run: bool) -> "OptimizationPhaseResult":
        """Handle all-strategies mode in ``run()``.

        Always writes ``optimization.yaml`` with ``strategy_results=[]`` and the
        current quality targets so that target-change detection works on the next
        run.  If quality targets changed since the last run, deletes all result
        sidecars from ``encoded/`` before returning so ``EncodingPhase`` sees
        ``ARTIFACT_ONLY`` pairs.

        Args:
            dry_run: When ``True``, skip writing ``optimization.yaml``.

        Returns:
            ``OptimizationPhaseResult`` with all configured strategies selected.
        """
        work_dir         = self._config.work_dir
        opt_yaml         = work_dir / _OPTIMIZATION_YAML
        current_targets  = _targets_as_strings(self._config.quality_targets)
        current_sampling = self._config.metrics_sampling

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
                _wipe_encoded_dir(work_dir, self._config.strategies)
            elif persisted is not None:
                logger.debug(
                    "All-strategies mode: params unchanged (sampling=%s, targets=%s) — encoded/ kept",
                    persisted.metrics_sampling, persisted.quality_targets,
                )

            # Always write optimization.yaml with current targets and sampling
            work_dir.mkdir(parents=True, exist_ok=True)
            OptimizationParams(
                crop             = None,
                test_chunks      = [],
                strategy_results = [],
                tolerance_pct    = 0.0,
                selected         = [s.name for s in self._config.strategies],
                quality_targets  = current_targets,
                metrics_sampling = current_sampling,
            ).save(opt_yaml)

        return OptimizationPhaseResult(
            outcome             = PhaseOutcome.REUSED,
            artifacts           = [],
            message             = "all-strategies mode — skipping optimization",
            selected_strategies = list(self._config.strategies),
            strategy_results    = [],
        )

    def _ensure_dependencies(self, execute: bool) -> "OptimizationPhaseResult | None":
        """Scan/run dependencies if they have no cached result; fail fast if incomplete.

        Args:
            execute: When ``True``, call ``dep.run()`` for deps without a cached result.

        Returns:
            A ``FAILED`` result if any dependency is not complete; ``None`` otherwise.
        """
        if self._job is None:
            return _failed("OptimizationPhase requires JobPhase")

        if self._job.result is None:
            if execute:
                self._job.run()
            else:
                self._job.scan()

        if not self._job.result.is_complete:  # type: ignore[union-attr]
            err = "JobPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        if self._chunking is None:
            return _failed("OptimizationPhase requires ChunkingPhase")

        if self._chunking.result is None:
            if execute:
                self._chunking.run()
            else:
                self._chunking.scan()

        if not self._chunking.result.is_complete:  # type: ignore[union-attr]
            err = "ChunkingPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        return None

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

def _targets_as_strings(quality_targets: "list[QualityTarget]") -> list[str]:
    """Serialise *quality_targets* to ``"metric-statistic:value"`` strings.

    Args:
        quality_targets: Quality targets from ``PipelineConfig``.

    Returns:
        Sorted list of strings like ``["vmaf-min:93.0"]``.
    """
    return sorted(f"{t.metric}-{t.statistic}:{t.value}" for t in quality_targets)


def _wipe_encoded_dir(work_dir: Path, strategies: "list[Strategy]") -> None:
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


def _failed(error: str) -> "OptimizationPhaseResult":
    """Return a ``FAILED`` ``OptimizationPhaseResult`` with the given error."""
    return OptimizationPhaseResult(
        outcome             = PhaseOutcome.FAILED,
        artifacts           = [],
        message             = error,
        error               = error,
        selected_strategies = [],
        strategy_results    = [],
    )


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
    crop_params:      CropParams | None,
    visual_hash:      bool = True,
    metrics_sampling: int  = DEFAULT_METRICS_SAMPLING,
) -> "ChunkEncoder":
    """Construct a ``ChunkEncoder`` for test encodes.

    Args:
        work_dir:         Pipeline working directory.
        crop_params:      Crop parameters to apply.
        visual_hash:      Whether to prepend emoji hash to chunk log lines.
        metrics_sampling: Frame subsampling factor for quality metric generation.

    Returns:
        Configured ``ChunkEncoder`` instance.
    """
    from pyqenc.phases.encoding import ChunkEncoder
    return ChunkEncoder(
        quality_evaluator = QualityEvaluator(work_dir),
        work_dir          = work_dir,
        crop_params       = crop_params,
        visual_hash       = visual_hash,
        metrics_sampling  = metrics_sampling,
    )



