"""
Optimization phase for the quality-based encoding pipeline.

This module handles optimal strategy selection by testing representative chunks
with all strategies and comparing file sizes.
"""

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from alive_progress import config_handler

from pyqenc.config import ConfigManager
from pyqenc.models import ChunkMetadata, CropParams, QualityTarget, VideoMetadata
from pyqenc.phases.encoding import (
    ChunkEncoder,
    ChunkEncodingResult,
    _encode_chunk_async,
)
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.log_format import fmt_optimization_summary, fmt_strategy_result_block
from pyqenc.utils.visualization import QualityEvaluator

if TYPE_CHECKING:
    from pyqenc.state import JobStateManager

config_handler.set_global(enrich_print=False) # type: ignore
_logger = logging.getLogger(__name__)


@dataclass
class StrategyTestResult:
    """Result of testing a strategy on test chunks.

    Attributes:
        strategy:        Strategy name
        total_file_size: Total file size across all successful test chunks (bytes)
        avg_crf:         Average CRF value used
        test_chunks:     List of chunk IDs used for testing
        all_passed:      Whether all test chunks met quality targets
        error:           Error message if testing failed
    """

    strategy: str
    total_file_size: float = 0.0
    avg_crf: float = 0.0
    test_chunks: list[str] = field(default_factory=list)
    all_passed: bool = False
    error: str | None = None


@dataclass
class OptimizationResult:
    """Result of optimization phase.

    Attributes:
        optimal_strategy: Name of optimal strategy (smallest avg file size)
        test_results: Results for each tested strategy
        success: Whether optimization completed successfully
        error: Error message if optimization failed
    """

    optimal_strategy: str | None = None
    test_results: dict[str, StrategyTestResult] = field(default_factory=dict)
    success: bool = True
    error: str | None = None


def _select_test_chunks(
    chunks: list[ChunkMetadata],
    percentage: float = 0.01,
    min_chunks: int = 3,
    exclude_start_percent: float = 0.10,
    exclude_end_percent: float = 0.10
) -> list[ChunkMetadata]:
    """Select representative test chunks for optimization.

    Selects approximately 1% of chunks (minimum 3) from the middle 80% of the video,
    excluding the first 10% and last 10% which may not be representative.

    Args:
        chunks: List of all chunks
        percentage: Percentage of chunks to select (default 1%)
        min_chunks: Minimum number of chunks to select
        exclude_start_percent: Percentage to exclude from start (default 10%)
        exclude_end_percent: Percentage to exclude from end (default 10%)

    Returns:
        List of selected test chunks
    """
    total_chunks = len(chunks)

    # Calculate exclusion boundaries
    start_idx = int(total_chunks * exclude_start_percent)
    end_idx = int(total_chunks * (1.0 - exclude_end_percent))

    # Get eligible chunks (middle 80%)
    eligible_chunks = chunks[start_idx:end_idx]

    if not eligible_chunks:
        _logger.warning("No eligible chunks after exclusion, using all chunks")
        eligible_chunks = chunks

    # Calculate number of test chunks
    num_test_chunks = max(min_chunks, int(total_chunks * percentage))
    num_test_chunks = min(num_test_chunks, len(eligible_chunks))

    _logger.info(
        f"Selecting {num_test_chunks} test chunks from {len(eligible_chunks)} "
        f"eligible chunks (excluded first {start_idx} and last {total_chunks - end_idx})"
    )

    # Randomly select test chunks
    test_chunks = random.sample(eligible_chunks, num_test_chunks)

    # Sort by chunk ID for consistent ordering
    test_chunks.sort(key=lambda c: c.chunk_id)

    return test_chunks


async def _encode_strategy_chunks_parallel(
    encoder:         ChunkEncoder,
    test_chunks:     list[ChunkMetadata],
    reference_dir:   Path,
    strategy:        str,
    quality_targets: list[QualityTarget],
    max_parallel:    int,
    phase_recovery:  "PhaseRecovery | None" = None,
    bar:             Callable[[int | float, AdvanceState], None] | None = None,
) -> StrategyTestResult:
    """Encode all test chunks for a single strategy in parallel.

    Mirrors ``_encode_chunks_parallel`` from ``encoding.py`` but is scoped to
    one strategy so the caller can log a per-strategy result block after all
    chunks finish.

    Args:
        encoder:         Configured ``ChunkEncoder`` (already carries crop params).
        test_chunks:     Representative chunks to encode.
        reference_dir:   Directory containing reference chunks.
        strategy:        Strategy name being tested.
        quality_targets: Quality targets to meet.
        max_parallel:    Maximum concurrent encoding workers.
        phase_recovery:  Optional recovery state; ``COMPLETE`` pairs are skipped
                         and ``ARTIFACT_ONLY`` pairs resume from recovered history.
        bar:             Optional advance callable yielded by ``ProgressBar``;
                         advanced by chunk duration on each completion.

    Returns:
        ``StrategyTestResult`` populated with averages and pass/fail status.
    """
    from pyqenc.phases.recovery import ArtifactState as _AS, PhaseRecovery as _PR  # local import

    semaphore = asyncio.Semaphore(max_parallel)
    result = StrategyTestResult(
        strategy=strategy,
        test_chunks=[c.chunk_id for c in test_chunks],
    )

    file_sizes: list[float] = []
    crfs:       list[float] = []
    errors:     list[str]   = []

    async def _encode_one(chunk: ChunkMetadata) -> ChunkEncodingResult:
        # Skip COMPLETE pairs from recovery
        if phase_recovery is not None:
            pair_rec = phase_recovery.pairs.get((chunk.chunk_id, strategy))
            if pair_rec is not None and pair_rec.state == _AS.COMPLETE:
                _logger.info(
                    "Skipping COMPLETE pair %s/%s (encoding result sidecar valid)",
                    chunk.chunk_id, strategy,
                )
                # Find the winning attempt to report file size
                for ar in reversed(pair_rec.attempts):
                    if ar.state == _AS.COMPLETE and ar.path.exists():
                        file_sizes.append(ar.path.stat().st_size)
                        crfs.append(ar.crf)
                        break
                if bar is not None:
                    bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.SKIPPED)
                return ChunkEncodingResult(
                    chunk_id=chunk.chunk_id,
                    strategy=strategy,
                    success=True,
                    reused=True,
                )

        reference_path = reference_dir / chunk.path.name
        if not reference_path.exists():
            msg = f"Reference chunk not found: {reference_path}"
            _logger.error(msg)
            errors.append(msg)
            if bar is not None:
                bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                error=msg,
            )

        reference = VideoMetadata(path=reference_path)

        # Inject recovered CRFHistory for ARTIFACT_ONLY pairs
        recovered_history = None
        if phase_recovery is not None:
            pair_rec = phase_recovery.pairs.get((chunk.chunk_id, strategy))
            if pair_rec is not None and pair_rec.state == _AS.ARTIFACT_ONLY:
                recovered_history = pair_rec.history
                _logger.info(
                    "Resuming ARTIFACT_ONLY pair %s/%s from %d recovered attempt(s)",
                    chunk.chunk_id, strategy, len(pair_rec.history.attempts),
                )

        async with semaphore:
            chunk_result = await _encode_chunk_async(
                encoder,
                chunk,
                reference,
                strategy,
                quality_targets,
                20.0,
                force=False,
                initial_history=recovered_history,
            )
            if bar is not None:
                if chunk_result.success:
                    bar(chunk.end_timestamp - chunk.start_timestamp)
                else:
                    errors.append(chunk_result.error or f"Unknown failure for chunk {chunk_result.chunk_id}")
                    bar(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)
            return chunk_result

    chunk_results = await asyncio.gather(*(_encode_one(c) for c in test_chunks))

    all_passed = True
    for cr in chunk_results:
        if cr.reused:
            # Already counted file_sizes/crfs above in the COMPLETE branch
            continue
        if cr.success and cr.encoded_file and cr.encoded_file.path.exists():
            file_sizes.append(cr.encoded_file.path.stat().st_size)
            if cr.final_crf is not None:
                crfs.append(cr.final_crf)
            _logger.info(
                "  Chunk %s: CRF %.2f, size %.2f MB",
                cr.chunk_id,
                cr.final_crf or 0.0,
                (cr.encoded_file.path.stat().st_size / 1024 / 1024),
            )
        else:
            all_passed = False
            err = cr.error or f"Unknown failure for chunk {cr.chunk_id}"
            errors.append(err)
            _logger.error("  Chunk %s failed: %s", cr.chunk_id, err)

    if file_sizes and crfs:
        result.total_file_size = sum(file_sizes)
        result.avg_crf         = sum(crfs) / len(crfs)

    result.all_passed = all_passed and not errors
    if errors:
        result.error = "; ".join(errors)

    return result


def find_optimal_strategy(
    chunks:                list[ChunkMetadata],
    reference_dir:         Path,
    strategies:            list[str],
    quality_targets:       list[QualityTarget],
    work_dir:              Path,
    config_manager:        ConfigManager,
    dry_run:               bool = False,
    crop_params:           CropParams | None = None,
    max_parallel:          int = 2,
    persisted_chunk_ids:   list[str] | None = None,
    state_manager:         "JobStateManager | None" = None,
    force:                 bool = False,
    standalone:            bool = False,
) -> OptimizationResult:
    """Find optimal strategy by testing on representative chunks.

    Selects ~1% of chunks (min 3) from middle 80% of video, encodes them with
    all strategies, and selects the strategy producing the smallest average file size.
    Test chunks within each strategy are encoded in parallel; the strategy loop
    remains sequential so per-strategy result blocks can be logged cleanly.

    Recovery integration (Req 3.4, 3.6, 2.3):
    - Pre-validates crop params against ``optimization.yaml`` (Req 3.4).
    - Writes ``optimization.yaml`` with crop + test_chunks after selection (Req 2.3).
    - Calls ``recover_attempts`` to classify all ``(test_chunk, strategy)`` pairs.
    - Skips ``COMPLETE`` pairs; resumes ``ARTIFACT_ONLY`` pairs from recovered history.
    - Updates ``optimization.yaml`` with ``optimal_strategy`` on convergence (Req 2.3).

    When *standalone* is ``True`` (direct CLI invocation, not via the
    auto-pipeline), ``discover_inputs`` is called first to verify that the
    chunking phase has produced its outputs (Req 11.1, 11.2).

    Args:
        chunks:                List of all chunks
        reference_dir:         Directory containing reference chunks
        strategies:            List of strategies to test
        quality_targets:       Quality targets to meet
        work_dir:              Working directory for artifacts
        config_manager:        Configuration manager
        progress_tracker:      Progress tracker
        dry_run:               If True, only report what would be done
        crop_params:           Crop parameters to apply uniformly to every chunk attempt.
                               When ``None``, no cropping is applied.
        max_parallel:          Maximum concurrent encoding workers per strategy (default 2).
        persisted_chunk_ids:   Chunk IDs from a previous run to reuse; when provided the
                               random selection step is skipped entirely.
        state_manager:         Optional ``JobStateManager`` for reading/writing phase
                               parameter YAML files.  When provided, crop pre-validation
                               and ``optimization.yaml`` persistence are enabled.
        force:                 When ``True``, a crop mismatch triggers artifact deletion
                               rather than a hard stop.
        standalone:            If True, run inputs discovery before proceeding (Req 11.1).

    Returns:
        OptimizationResult with optimal strategy and test results
    """
    from pyqenc.phases.recovery import discover_inputs, recover_attempts
    from pyqenc.state import OptimizationParams

    _logger.info(
        "Optimization phase: testing %d strategies on representative chunks",
        len(strategies),
    )

    # Inputs discovery — only when invoked standalone (not via auto-pipeline)
    if standalone:
        discovery = discover_inputs("optimization", work_dir)
        if not discovery.ok:
            return OptimizationResult(success=False, error=discovery.error)

    # --- Step 1: Crop pre-validation against optimization.yaml (Req 3.4) ---
    if state_manager is not None:
        persisted_opt = state_manager.load_optimization()
        if persisted_opt is not None:
            if persisted_opt.crop != crop_params:
                if force:
                    _logger.warning(
                        "Crop params changed since last optimization run "
                        "(persisted=%s, current=%s) — --force: deleting all optimization attempt artifacts",
                        persisted_opt.crop, crop_params,
                    )
                    encoded_base = work_dir / "encoded"
                    if encoded_base.exists():
                        import shutil as _shutil
                        _shutil.rmtree(encoded_base)
                        _logger.debug("Deleted encoded directory: %s", encoded_base)
                else:
                    _logger.critical(
                        "Crop params changed since last optimization run "
                        "(persisted=%s, current=%s). "
                        "Re-run with --force to delete stale optimization artifacts and continue.",
                        persisted_opt.crop, crop_params,
                    )
                    return OptimizationResult(
                        success=False,
                        error="Crop params mismatch — aborting. Use --force to override.",
                    )
            # Restore persisted test chunk IDs when not already provided
            if not persisted_chunk_ids and persisted_opt.test_chunks:
                persisted_chunk_ids = persisted_opt.test_chunks
                _logger.debug(
                    "Restored %d test chunk IDs from optimization.yaml",
                    len(persisted_chunk_ids),
                )

    if dry_run:
        _logger.info("[DRY-RUN] Would select test chunks and encode with all strategies")
        _logger.info("[DRY-RUN] Strategies to test: %s", ", ".join(strategies))
        _logger.info("[DRY-RUN] Status: Would perform optimization")
        return OptimizationResult(success=True)

    # --- Step 2: Select test chunks ---
    if persisted_chunk_ids:
        chunk_by_id = {c.chunk_id: c for c in chunks}
        test_chunks = [chunk_by_id[cid] for cid in persisted_chunk_ids if cid in chunk_by_id]
        if test_chunks:
            _logger.info(
                "Reusing %d persisted test chunks: %s",
                len(test_chunks),
                ", ".join(c.chunk_id for c in test_chunks),
            )
        else:
            _logger.warning("Persisted test chunk IDs not found in current chunk list — re-selecting")
            test_chunks = _select_test_chunks(chunks)
            _logger.info("Selected test chunks: %s", ", ".join(c.chunk_id for c in test_chunks))
    else:
        test_chunks = _select_test_chunks(chunks)
        _logger.info("Selected test chunks: %s", ", ".join(c.chunk_id for c in test_chunks))

    # --- Step 3: Write optimization.yaml with crop + test_chunks (Req 2.3) ---
    if state_manager is not None:
        state_manager.save_optimization(OptimizationParams(
            crop=crop_params,
            test_chunks=[c.chunk_id for c in test_chunks],
        ))
        _logger.debug("Wrote optimization.yaml (crop=%s, test_chunks=%d)", crop_params, len(test_chunks))

    # --- Step 4: Artifact recovery via recover_attempts (Req 3.6) ---
    test_chunk_ids = [c.chunk_id for c in test_chunks]
    phase_recovery = recover_attempts(work_dir, test_chunk_ids, strategies, quality_targets)

    # Create encoder — crop_params threaded in here
    encoder = ChunkEncoder(
        config_manager=config_manager,
        quality_evaluator=QualityEvaluator(work_dir),
        work_dir=work_dir,
        crop_params=crop_params,
    )

    # --- Step 5: Test each strategy sequentially; chunks within each strategy run in parallel ---
    result = OptimizationResult()

    total_seconds = sum(c.end_timestamp - c.start_timestamp for c in test_chunks) * len(strategies)
    with ProgressBar(total_seconds, title="Optimization") as advance:
        for strategy in strategies:
            _logger.info("Testing strategy: %s", strategy)

            strategy_result = asyncio.run(
                _encode_strategy_chunks_parallel(
                    encoder=encoder,
                    test_chunks=test_chunks,
                    reference_dir=reference_dir,
                    strategy=strategy,
                    quality_targets=quality_targets,
                    max_parallel=max_parallel,
                    phase_recovery=phase_recovery,
                    bar=advance,
                )
            )

            total_size_mb = strategy_result.total_file_size / (1024 * 1024)
            for line in fmt_strategy_result_block(
                strategy=strategy,
                avg_crf=strategy_result.avg_crf,
                total_size_mb=total_size_mb,
                num_chunks=len(test_chunks),
                passed=strategy_result.all_passed,
                error=strategy_result.error,
            ):
                _logger.info(line)

            result.test_results[strategy] = strategy_result

    # Select optimal strategy (smallest average file size among successful strategies)
    successful_strategies = {
        name: res for name, res in result.test_results.items()
        if res.all_passed and res.total_file_size > 0
    }

    if not successful_strategies:
        error_msg = "No strategies successfully encoded all test chunks"
        _logger.error(error_msg)
        result.success = False
        result.error = error_msg
        return result

    # Find strategy with smallest total file size
    optimal_strategy = min(
        successful_strategies.items(),
        key=lambda item: item[1].total_file_size
    )

    result.optimal_strategy = optimal_strategy[0]
    result.success = True

    # --- Step 6: Update optimization.yaml with optimal_strategy (Req 2.3) ---
    if state_manager is not None:
        state_manager.save_optimization(OptimizationParams(
            crop=crop_params,
            test_chunks=[c.chunk_id for c in test_chunks],
            optimal_strategy=result.optimal_strategy,
        ))
        _logger.debug("Updated optimization.yaml with optimal_strategy=%s", result.optimal_strategy)

    for line in fmt_optimization_summary(result.optimal_strategy, result.test_results):
        _logger.info(line)

    return result
