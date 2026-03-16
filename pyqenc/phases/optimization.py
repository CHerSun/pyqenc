"""
Optimization phase for the quality-based encoding pipeline.

This module handles optimal strategy selection by testing representative chunks
with all strategies and comparing file sizes.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from alive_progress import alive_bar, config_handler

from pyqenc.config import ConfigManager
from pyqenc.constants import PROGRESS_CHUNK_UNIT
from pyqenc.models import CropParams, QualityTarget
from pyqenc.phases.encoding import (
    ChunkEncoder,
    ChunkEncodingResult,
    ChunkInfo,
    _encode_chunk_async,
)
from pyqenc.progress import ProgressTracker
from pyqenc.utils.log_format import fmt_optimization_summary, fmt_strategy_result_block
from pyqenc.utils.visualization import QualityEvaluator

config_handler.set_global(enrich_print=False) # type: ignore
_logger = logging.getLogger(__name__)


@dataclass
class StrategyTestResult:
    """Result of testing a strategy on test chunks.

    Attributes:
        strategy: Strategy name
        avg_file_size: Average file size across test chunks (bytes)
        avg_crf: Average CRF value used
        test_chunks: List of chunk IDs used for testing
        all_passed: Whether all test chunks met quality targets
        error: Error message if testing failed
    """

    strategy: str
    avg_file_size: float = 0.0
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
    chunks: list[ChunkInfo],
    percentage: float = 0.01,
    min_chunks: int = 3,
    exclude_start_percent: float = 0.10,
    exclude_end_percent: float = 0.10
) -> list[ChunkInfo]:
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
    test_chunks:     list[ChunkInfo],
    reference_dir:   Path,
    strategy:        str,
    quality_targets: list[QualityTarget],
    max_parallel:    int,
    bar:             object | None = None,
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
        bar:             Optional ``alive_bar`` handle; incremented on each chunk
                         completion (success or failure) per Req 4.4, 4.7.

    Returns:
        ``StrategyTestResult`` populated with averages and pass/fail status.
    """
    semaphore = asyncio.Semaphore(max_parallel)
    result = StrategyTestResult(
        strategy=strategy,
        test_chunks=[c.chunk_id for c in test_chunks],
    )

    file_sizes: list[float] = []
    crfs:       list[float] = []
    errors:     list[str]   = []

    async def _encode_one(chunk: ChunkInfo) -> ChunkEncodingResult:
        reference = reference_dir / chunk.file_path.name
        if not reference.exists():
            msg = f"Reference chunk not found: {reference}"
            _logger.error(msg)
            errors.append(msg)
            if bar is not None:
                bar.text = f"⚠ {strategy}/{chunk.chunk_id}"  # type: ignore[union-attr]
                bar()  # type: ignore[operator]
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                error=msg,
            )

        avg_crf     = encoder.progress_tracker.get_successful_crf_average(strategy)
        initial_crf = avg_crf if avg_crf is not None else 20.0

        async with semaphore:
            chunk_result = await _encode_chunk_async(
                encoder,
                chunk,
                reference,
                strategy,
                quality_targets,
                initial_crf,
                force=False,
            )
            if bar is not None:
                if chunk_result.success:
                    bar.text = f"{strategy}/{chunk.chunk_id}"  # type: ignore[union-attr]
                else:
                    bar.text = f"⚠ {strategy}/{chunk.chunk_id}"  # type: ignore[union-attr]
                bar()  # type: ignore[operator]
            return chunk_result

    chunk_results = await asyncio.gather(*(_encode_one(c) for c in test_chunks))

    all_passed = True
    for cr in chunk_results:
        if cr.success and cr.encoded_file and cr.encoded_file.exists():
            file_sizes.append(cr.encoded_file.stat().st_size)
            if cr.final_crf is not None:
                crfs.append(cr.final_crf)
            _logger.info(
                "  Chunk %s: CRF %.2f, size %.2f MB",
                cr.chunk_id,
                cr.final_crf or 0.0,
                (cr.encoded_file.stat().st_size / 1024 / 1024),
            )
        else:
            all_passed = False
            err = cr.error or f"Unknown failure for chunk {cr.chunk_id}"
            errors.append(err)
            _logger.error("  Chunk %s failed: %s", cr.chunk_id, err)

    if file_sizes and crfs:
        result.avg_file_size = sum(file_sizes) / len(file_sizes)
        result.avg_crf       = sum(crfs) / len(crfs)

    result.all_passed = all_passed and not errors
    if errors:
        result.error = "; ".join(errors)

    return result


def find_optimal_strategy(
    chunks:                list[ChunkInfo],
    reference_dir:         Path,
    strategies:            list[str],
    quality_targets:       list[QualityTarget],
    work_dir:              Path,
    config_manager:        ConfigManager,
    progress_tracker:      ProgressTracker,
    dry_run:               bool = False,
    crop_params:           CropParams | None = None,
    max_parallel:          int = 2,
    persisted_chunk_ids:   list[str] | None = None,
) -> OptimizationResult:
    """Find optimal strategy by testing on representative chunks.

    Selects ~1% of chunks (min 3) from middle 80% of video, encodes them with
    all strategies, and selects the strategy producing the smallest average file size.
    Test chunks within each strategy are encoded in parallel; the strategy loop
    remains sequential so per-strategy result blocks can be logged cleanly.

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

    Returns:
        OptimizationResult with optimal strategy and test results

    Requirements:
        - 4.1: Select ~1% of chunks (min 3)
        - 4.2: Exclude first/last 10%
        - 4.3: Test all strategies
        - 4.4: Adjust CRF for each strategy
        - 4.5: Select strategy with smallest avg file size
        - 5.1, 5.2: Parallel workers controlled by max_parallel
        - 5.4: Strategy loop sequential; parallelism within each strategy
    """
    _logger.info(
        f"Optimization phase: testing {len(strategies)} strategies on "
        f"representative chunks"
    )

    if dry_run:
        _logger.info("[DRY-RUN] Would select test chunks and encode with all strategies")
        _logger.info(f"[DRY-RUN] Strategies to test: {', '.join(strategies)}")
        _logger.info("[DRY-RUN] Status: Would perform optimization")
        return OptimizationResult(success=True)

    # Select test chunks — reuse persisted IDs when available so restarts
    # always test the same representative sample.
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
    else:
        test_chunks = _select_test_chunks(chunks)

    _logger.info(
        f"Selected test chunks: {', '.join(c.chunk_id for c in test_chunks)}"
    )

    # Persist the selected chunk IDs immediately so a restart reuses the same sample.
    if not persisted_chunk_ids:
        from pyqenc.models import PhaseMetadata, PhaseStatus, PhaseUpdate
        progress_tracker.update_phase(PhaseUpdate(
            phase="optimization",
            status=PhaseStatus.IN_PROGRESS,
            metadata=PhaseMetadata(test_chunk_ids=[c.chunk_id for c in test_chunks]),
        ))

    # Create encoder — crop_params threaded in here (Req 1.2)
    encoder = ChunkEncoder(
        config_manager=config_manager,
        quality_evaluator=QualityEvaluator(work_dir),
        progress_tracker=progress_tracker,
        work_dir=work_dir,
        crop_params=crop_params,
    )

    # Test each strategy sequentially; chunks within each strategy run in parallel
    result = OptimizationResult()

    total_chunks = len(strategies) * len(test_chunks)
    with alive_bar(total_chunks, title="Optimization", unit=PROGRESS_CHUNK_UNIT) as bar:
        for strategy in strategies:
            _logger.info(f"Testing strategy: {strategy}")

            strategy_result = asyncio.run(
                _encode_strategy_chunks_parallel(
                    encoder=encoder,
                    test_chunks=test_chunks,
                    reference_dir=reference_dir,
                    strategy=strategy,
                    quality_targets=quality_targets,
                    max_parallel=max_parallel,
                    bar=bar,
                )
            )

            total_size_mb = strategy_result.avg_file_size * len(test_chunks) / (1024 * 1024)
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
        if res.all_passed and res.avg_file_size > 0
    }

    if not successful_strategies:
        error_msg = "No strategies successfully encoded all test chunks"
        _logger.error(error_msg)
        result.success = False
        result.error = error_msg
        return result

    # Find strategy with smallest average file size
    optimal_strategy = min(
        successful_strategies.items(),
        key=lambda item: item[1].avg_file_size
    )

    result.optimal_strategy = optimal_strategy[0]
    result.success = True

    for line in fmt_optimization_summary(result.optimal_strategy, result.test_results):
        _logger.info(line)

    return result
