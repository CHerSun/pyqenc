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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from alive_progress import config_handler

from pyqenc.config import ConfigManager
from pyqenc.constants import (
    CHUNKS_DIR,
    CRF_INITIAL_DEFAULT,
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
    TEMP_SUFFIX,
    THICK_LINE,
    THIN_LINE,
)
from pyqenc.models import (
    ChunkMetadata,
    CropParams,
    PhaseOutcome,
    QualityTarget,
    Strategy,
    VideoMetadata,
)
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.state import ArtifactState, OptimizationParams, StrategyTestResult
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.log_format import (
    emit_phase_banner,
    fmt_strategy_result_block,
    log_recovery_line,
)
from pyqenc.utils.visualization import QualityEvaluator

if TYPE_CHECKING:
    from pyqenc.models import PipelineConfig
    from pyqenc.phases.chunking import ChunkingPhase, ChunkingPhaseResult
    from pyqenc.phases.job import JobPhase, JobPhaseResult

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
        config: "PipelineConfig",
        phases: "dict[type[Phase], Phase] | None" = None,
    ) -> None:
        from pyqenc.phases.chunking import ChunkingPhase as _ChunkingPhase
        from pyqenc.phases.job import JobPhase as _JobPhase

        self._config   = config
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

        # All-strategies mode: no artifacts, just return all strategies
        if not self._config.optimize:
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
            current_targets = _targets_as_strings(self._config.quality_targets)
            if persisted.quality_targets and persisted.quality_targets != current_targets:
                # Quality targets changed — work is needed; run() will invalidate encoded/ sidecars
                self.result = OptimizationPhaseResult(
                    outcome             = PhaseOutcome.DRY_RUN,
                    artifacts           = [],
                    message             = "quality targets changed — re-run needed",
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
                    selected_strategies = selected,
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
        # All-strategies mode: silent, no logging, no test encodes
        if not self._config.optimize:
            self.result = self._run_all_strategies(dry_run=dry_run)
            return self.result

        work_dir  = self._config.work_dir
        opt_yaml  = work_dir / _OPTIMIZATION_YAML
        tolerance = self._config.strategy_selection_tolerance

        # Step 1: load persisted optimization params (before dependency check so
        # tolerance re-application can short-circuit without needing live phases)
        persisted = OptimizationParams.load(opt_yaml)

        current_targets = _targets_as_strings(self._config.quality_targets)

        # Step 2: quality-target change detection — must happen before tolerance
        # re-application so we don't skip re-encoding when targets changed.
        targets_changed = (
            persisted is not None
            and bool(persisted.quality_targets)
            and persisted.quality_targets != current_targets
        )
        if targets_changed and persisted is not None and persisted.strategy_results:
            # Delete all result sidecars from encoded/ for every strategy so
            # EncodingPhase sees ARTIFACT_ONLY pairs and re-evaluates them.
            _delete_encoded_result_sidecars(work_dir, self._config.strategies)
            # Treat all cached strategy results as stale — force re-encoding
            persisted = OptimizationParams(
                crop             = persisted.crop,
                test_chunks      = persisted.test_chunks,
                strategy_results = [],
                tolerance_pct    = persisted.tolerance_pct,
                selected         = [],
                quality_targets  = persisted.quality_targets,
            )

        # Step 3: tolerance re-application — all results cached, only tolerance changed
        # This is a pure read-from-cache operation; no dependencies needed.
        if (
            persisted is not None
            and persisted.strategy_results
            and len(persisted.strategy_results) == len(self._config.strategies)
            and persisted.tolerance_pct != tolerance
            and not targets_changed
        ):
            emit_phase_banner("OPTIMIZATION", logger)
            logger.info("Strategies:  %s", ", ".join(s.name for s in self._config.strategies))
            logger.info("Tolerance:   %.1f%%", tolerance)
            logger.info(
                "All strategy results cached; tolerance changed (%.1f%% → %.1f%%) — re-selecting without re-encoding",
                persisted.tolerance_pct, tolerance,
            )
            selected = self._apply_tolerance(persisted.strategy_results, tolerance)
            OptimizationParams(
                crop             = persisted.crop,
                test_chunks      = persisted.test_chunks,
                strategy_results = persisted.strategy_results,
                tolerance_pct    = tolerance,
                selected         = selected,
                quality_targets  = current_targets,
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
                selected_strategies = selected,
                strategy_results    = persisted.strategy_results,
            )
            return self.result

        # Step 4: check if all results already cached with matching tolerance
        if (
            persisted is not None
            and persisted.strategy_results
            and len(persisted.strategy_results) == len(self._config.strategies)
            and persisted.tolerance_pct == tolerance
            and not targets_changed
        ):
            emit_phase_banner("OPTIMIZATION", logger)
            logger.info("Strategies:  %s", ", ".join(s.name for s in self._config.strategies))
            logger.info("Tolerance:   %.1f%%", tolerance)
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
                selected_strategies = selected,
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
                cached_results[r.strategy.name] = r

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
        ).save(opt_yaml)

        # Step 10: run test encodes for pending strategies
        config_manager = ConfigManager()
        encoder = _make_encoder(config_manager, work_dir, crop)
        reference_dir  = work_dir / CHUNKS_DIR

        all_results: list[StrategyTestResult] = list(cached_results.values())

        for strategy in strategies_to_test:
            logger.info("Testing strategy: %s", strategy.name)

            # Seed initial CRF from the last persisted result for this strategy (moving average),
            # falling back to the codec default_crf when no prior result exists.
            persisted_for_strategy = cached_results.get(strategy.name)
            if persisted_for_strategy is not None and persisted_for_strategy.avg_crf > 0.0:
                strategy_initial_crf = persisted_for_strategy.avg_crf
            else:
                try:
                    strategy_configs     = config_manager.parse_strategy(strategy.name)
                    strategy_initial_crf = strategy_configs[0].codec.default_crf if strategy_configs else CRF_INITIAL_DEFAULT
                except (ValueError, IndexError):
                    strategy_initial_crf = CRF_INITIAL_DEFAULT

            strategy_result = asyncio.run(
                _encode_strategy_test_chunks(
                    encoder        = encoder,
                    test_chunks    = test_chunks,
                    reference_dir  = reference_dir,
                    strategy       = strategy,
                    quality_targets= self._config.quality_targets,
                    max_parallel   = self._config.max_parallel,
                    work_dir       = work_dir,
                    initial_crf    = strategy_initial_crf,
                )
            )

            all_results.append(strategy_result)

            # Persist after each strategy completes (ordered by size ascending)
            sorted_results = sorted(all_results, key=lambda r: r.total_size)
            OptimizationParams(
                crop             = crop,
                test_chunks      = [c.chunk_id for c in test_chunks],
                strategy_results = sorted_results,
                tolerance_pct    = tolerance,
                selected         = [],
                quality_targets  = current_targets,
            ).save(opt_yaml)

            # Log per-strategy result block
            total_size_mb = strategy_result.total_size / (1024 * 1024)
            passed = strategy_result.total_size > 0
            for line in fmt_strategy_result_block(
                strategy      = strategy.name,
                avg_crf       = strategy_result.avg_crf,
                total_size_mb = total_size_mb,
                num_chunks    = len(test_chunks),
                passed        = passed,
            ):
                logger.info(line)

        # Step 11: sort final results by size and select strategies
        final_results = sorted(all_results, key=lambda r: r.total_size)
        selected      = self._apply_tolerance(final_results, tolerance)

        # Persist final state with current quality targets
        OptimizationParams(
            crop             = crop,
            test_chunks      = [c.chunk_id for c in test_chunks],
            strategy_results = final_results,
            tolerance_pct    = tolerance,
            selected         = selected,
            quality_targets  = current_targets,
        ).save(opt_yaml)

        self._log_optimization_summary(final_results, selected)

        self.result = OptimizationPhaseResult(
            outcome             = PhaseOutcome.COMPLETED,
            artifacts           = [Artifact(
                path  = work_dir / _OPTIMIZATION_YAML,
                state = ArtifactState.COMPLETE,
            )],
            message             = f"{len(selected)} strategy(ies) selected",
            selected_strategies = selected,
            strategy_results    = final_results,
        )
        return self.result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
        work_dir        = self._config.work_dir
        opt_yaml        = work_dir / _OPTIMIZATION_YAML
        current_targets = _targets_as_strings(self._config.quality_targets)

        if not dry_run:
            persisted = OptimizationParams.load(opt_yaml)
            if (
                persisted is not None
                and bool(persisted.quality_targets)
                and persisted.quality_targets != current_targets
            ):
                logger.debug(
                    "All-strategies mode: quality targets changed — deleting encoded/ result sidecars"
                )
                _delete_encoded_result_sidecars(work_dir, self._config.strategies)

            # Always write optimization.yaml with current targets
            work_dir.mkdir(parents=True, exist_ok=True)
            OptimizationParams(
                crop             = None,
                test_chunks      = [],
                strategy_results = [],
                tolerance_pct    = 0.0,
                selected         = list(self._config.strategies),
                quality_targets  = current_targets,
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
    ) -> list[Strategy]:
        """Select strategies within *tolerance_pct* of the best (smallest) result.

        Args:
            results:       Per-strategy test results ordered by increasing total size.
            tolerance_pct: Percentage threshold; strategies within this percentage
                           of the best strategy's size are also selected.
                           ``0.0`` means exactly one strategy is selected.

        Returns:
            List of selected ``Strategy`` objects.
        """
        successful = [r for r in results if r.total_size > 0]
        if not successful:
            return []

        best_size = successful[0].total_size
        threshold = best_size * (1.0 + tolerance_pct / 100.0)

        return [r.strategy for r in successful if r.total_size <= threshold]

    def _log_optimization_summary(
        self,
        results:  list[StrategyTestResult],
        selected: list[Strategy],
    ) -> None:
        """Emit the optimization summary table to the log.

        Args:
            results:  All strategy test results ordered by size.
            selected: Selected strategies.
        """
        selected_names = {s.name for s in selected}

        logger.info("")
        logger.info(
            "  %-30s  %8s  %12s  %8s",
            "Strategy", "Avg CRF", "Size (MB)", "Status",
        )
        logger.info(
            "  %-30s  %8s  %12s  %8s",
            "-" * 30, "-" * 8, "-" * 12, "-" * 8,
        )

        for res in results:
            size_mb  = res.total_size / (1024 * 1024) if res.total_size > 0 else 0.0
            size_str = f"{size_mb:,.1f}".replace(",", "\u202f")
            marker   = " ◀ selected" if res.strategy.name in selected_names else ""
            status   = "passed" if res.total_size > 0 else "failed"
            logger.info(
                "  %-30s  %8.2f  %12s  %8s%s",
                res.strategy.name[:30], res.avg_crf, size_str, status, marker,
            )

        logger.info("")
        if selected:
            logger.info("Selected strategies: %s", ", ".join(s.name for s in selected))
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


def _delete_encoded_result_sidecars(work_dir: Path, strategies: "list[Strategy]") -> None:
    """Delete all ``<chunk_id>.<res>.yaml`` result sidecars from ``encoded/<strategy>/``.

    Leaves attempt files in ``encoding/`` intact so CRF history can be replayed.
    After this call ``EncodingPhase._recover()`` will see ``ARTIFACT_ONLY`` pairs
    (attempt files present, result sidecar absent) and resume from CRF history.

    Args:
        work_dir:   Pipeline working directory.
        strategies: All configured strategies (safe_name used for directory lookup).
    """
    encoded_base = work_dir / ENCODED_OUTPUT_DIR
    if not encoded_base.exists():
        return

    for strategy in strategies:
        strategy_dir = encoded_base / strategy.safe_name
        if not strategy_dir.exists():
            continue
        for sidecar in list(strategy_dir.glob("*.yaml")):
            # Result sidecars match <chunk_id>.<resolution>.yaml — they do NOT
            # match the attempt pattern (which includes .crf<N>. in the stem).
            # We delete all .yaml files here; attempt .yaml sidecars live in
            # encoding/<strategy>/, not encoded/<strategy>/.
            try:
                sidecar.unlink()
                logger.debug("Deleted stale result sidecar: %s", sidecar)
            except OSError as exc:
                logger.warning("Could not delete result sidecar %s: %s", sidecar, exc)


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
    config_manager: ConfigManager,
    work_dir:       Path,
    crop_params:    CropParams | None,
) -> "ChunkEncoder":
    """Construct a ``ChunkEncoder`` for test encodes.

    Args:
        config_manager: Configuration manager.
        work_dir:       Pipeline working directory.
        crop_params:    Crop parameters to apply.

    Returns:
        Configured ``ChunkEncoder`` instance.
    """
    from pyqenc.phases.encoding import ChunkEncoder
    return ChunkEncoder(
        config_manager    = config_manager,
        quality_evaluator = QualityEvaluator(work_dir),
        work_dir          = work_dir,
        crop_params       = crop_params,
    )


async def _encode_strategy_test_chunks(
    encoder:         "ChunkEncoder",
    test_chunks:     list[ChunkMetadata],
    reference_dir:   Path,
    strategy:        Strategy,
    quality_targets: list[QualityTarget],
    max_parallel:    int,
    work_dir:        Path,
    initial_crf:     float = CRF_INITIAL_DEFAULT,
) -> StrategyTestResult:
    """Encode all test chunks for a single strategy in parallel.

    Args:
        encoder:         Configured ``ChunkEncoder``.
        test_chunks:     Representative chunks to encode.
        reference_dir:   Directory containing reference chunks.
        strategy:        Strategy being tested.
        quality_targets: Quality targets to meet.
        max_parallel:    Maximum concurrent encoding workers.
        work_dir:        Pipeline working directory (for recovery).
        initial_crf:     Starting CRF for the first test chunk (moving average
                         from the last persisted result for this strategy).

    Returns:
        ``StrategyTestResult`` populated with totals and averages.
    """
    from pyqenc.phases.encoding import (
        ArtifactState,
        _encode_chunk_async,
        _recover_encoding_attempts,
    )

    semaphore = asyncio.Semaphore(max_parallel)

    # Recover existing attempt state for this strategy
    test_chunk_ids = [c.chunk_id for c in test_chunks]
    phase_recovery = _recover_encoding_attempts(work_dir, test_chunk_ids, [strategy.name], quality_targets)

    file_sizes: list[float] = []
    crfs:       list[float] = []

    # Moving-average CRF seed: start from the provided initial_crf (rounded to granularity),
    # then update after each winning test chunk so later chunks start closer to the optimum.
    moving_crf: float = round(initial_crf / CRF_GRANULARITY) * CRF_GRANULARITY
    total_seconds = sum(c.end_timestamp - c.start_timestamp for c in test_chunks)
    with ProgressBar(total_seconds, title=f"Optimization [{strategy.name}]") as advance:
        async def _encode_one(chunk: ChunkMetadata) -> None:
            nonlocal moving_crf
            pair_rec = phase_recovery.pairs.get((chunk.chunk_id, strategy.name))

            # Skip COMPLETE pairs
            if pair_rec is not None and pair_rec.state == ArtifactState.COMPLETE:
                for ar in reversed(pair_rec.attempts):
                    if ar.state == ArtifactState.COMPLETE and ar.path.exists():
                        file_sizes.append(ar.path.stat().st_size)
                        crfs.append(ar.crf)
                        break
                advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.SKIPPED)
                return

            reference_path = reference_dir / chunk.path.name
            if not reference_path.exists():
                logger.error("Reference chunk not found: %s", reference_path)
                advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)
                return

            reference = VideoMetadata(path=reference_path)

            # Inject recovered CRFHistory for ARTIFACT_ONLY pairs
            recovered_history = None
            if pair_rec is not None and pair_rec.state == ArtifactState.ARTIFACT_ONLY:
                recovered_history = pair_rec.history

            async with semaphore:
                chunk_result = await _encode_chunk_async(
                    encoder,
                    chunk,
                    reference,
                    strategy.name,
                    quality_targets,
                    moving_crf,
                    force=False,
                    initial_history=recovered_history,
                )

            if chunk_result.success and chunk_result.encoded_file and chunk_result.encoded_file.path.exists():
                file_sizes.append(chunk_result.encoded_file.path.stat().st_size)
                if chunk_result.final_crf is not None:
                    crfs.append(chunk_result.final_crf)
                    # Update moving average: blend last stored seed with this winning CRF
                    moving_crf = (moving_crf + chunk_result.final_crf) / 2.0
                advance(chunk.end_timestamp - chunk.start_timestamp)
            else:
                logger.error("Test encode failed for chunk %s / %s", chunk.chunk_id, strategy.name)
                advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)

        await asyncio.gather(*(_encode_one(c) for c in test_chunks))

    total_size = int(sum(file_sizes))
    avg_crf    = sum(crfs) / len(crfs) if crfs else 0.0

    return StrategyTestResult(
        strategy   = strategy,
        total_size = total_size,
        avg_crf    = avg_crf,
    )
