"""
Encoding phase for the quality-based encoding pipeline.

This module handles chunk encoding with iterative CRF adjustment to meet
quality targets, including parallel execution and artifact-based resumption.
"""

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from alive_progress import alive_bar, config_handler

from pyqenc.config import ConfigManager
from pyqenc.constants import (
    FAILURE_SYMBOL_MINOR,
    PADDING_CRF,
    PROGRESS_CHUNK_UNIT,
    SUCCESS_SYMBOL_MINOR,
    THRESHOLD_ATTEMPTS_WARNING,
)
from pyqenc.models import (
    AttemptInfo,
    ChunkUpdate,
    CropParams,
    PhaseStatus,
    QualityTarget,
    StrategyConfig,
    VideoMetadata,
)
from pyqenc.progress import ProgressTracker
from pyqenc.quality import CRFHistory, adjust_crf
from pyqenc.utils.alive import update_bar
from pyqenc.utils.log_format import (
    fmt_chunk,
    fmt_chunk_attempt_result,
    fmt_chunk_attempt_start,
    fmt_chunk_final,
    fmt_chunk_start,
)
from pyqenc.utils.visualization import QualityEvaluator

config_handler.set_global(enrich_print=False) # type: ignore
_logger = logging.getLogger(__name__)


def _probe_resolution(path: Path) -> str | None:
    """Return the video resolution of *path* as ``'WxH'``, or ``None`` on failure."""
    import json as _json
    import subprocess
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        data = _json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            w, h = streams[0].get("width"), streams[0].get("height")
            if w and h:
                return f"{w}x{h}"
    except Exception:
        pass
    return None


@dataclass
class ChunkInfo:
    """Information about a video chunk.

    Attributes:
        chunk_id: Unique chunk identifier
        file_path: Path to chunk file
        start_frame: Starting frame number
        end_frame: Ending frame number
        frame_count: Number of frames in chunk
        duration: Duration in seconds
    """

    chunk_id: str
    file_path: Path
    start_frame: int
    end_frame: int
    frame_count: int
    duration: float


@dataclass
class ChunkEncodingResult:
    """Result of encoding a single chunk.

    Attributes:
        chunk_id: Chunk identifier
        strategy: Strategy used
        success: Whether encoding succeeded
        final_crf: Final CRF value used
        attempts: Number of encoding attempts
        encoded_file: Path to final encoded file
        reused: Whether existing encoding was reused
        error: Error message if failed
    """

    chunk_id: str
    strategy: str
    success: bool
    final_crf: float | None = None
    attempts: int = 0
    encoded_file: Path | None = None
    reused: bool = False
    error: str | None = None


@dataclass
class EncodingResult:
    """Result of encoding all chunks.

    Attributes:
        encoded_chunks: Mapping of chunk_id -> strategy -> encoded file path
        reused_count: Number of chunks reused from previous runs
        encoded_count: Number of chunks newly encoded
        success: Whether all chunks encoded successfully
        failed_chunks: List of chunk IDs that failed
        error: Error message if pipeline failed
    """

    encoded_chunks: dict[str, dict[str, Path]] = field(default_factory=dict)
    reused_count: int = 0
    encoded_count: int = 0
    success: bool = True
    failed_chunks: list[str] = field(default_factory=list)
    error: str | None = None


class ChunkEncoder:
    """Handles encoding of individual chunks with CRF adjustment.

    This class manages the iterative encoding process for a single chunk,
    adjusting CRF values until quality targets are met.
    """

    def __init__(
        self,
        config_manager:    ConfigManager,
        quality_evaluator: QualityEvaluator,
        progress_tracker:  ProgressTracker,
        work_dir:          Path,
        crop_params:       CropParams | None = None,
    ):
        """Initialize chunk encoder.

        Args:
            config_manager:    Configuration manager for strategy parsing
            quality_evaluator: Quality evaluator for metric calculation
            progress_tracker:  Progress tracker for state management
            work_dir:          Working directory for artifacts
            crop_params:       Optional crop parameters to apply to every chunk attempt.
        """
        self.config_manager    = config_manager
        self.quality_evaluator = quality_evaluator
        self.progress_tracker  = progress_tracker
        self.work_dir          = work_dir
        self._crop_params      = crop_params

    def _get_output_dir(self, strategy: str) -> Path:
        """Get output directory for strategy.

        Args:
            strategy: Strategy name

        Returns:
            Path to strategy output directory
        """
        # Make strategy name filesystem-safe
        safe_strategy = strategy.replace("+", "_").replace(":", "_")
        return self.work_dir / "encoded" / safe_strategy

    def _get_attempt_path(
        self,
        chunk_id: str,
        strategy: str,
        resolution: str | None = None,
        crf: float | None = None,
    ) -> Path:
        """Get path for encoding attempt.

        Uses the naming pattern:
        ``<start>-<end>.<width>x<height>.crf<CRF>.mkv``

        Falls back to a simpler name when resolution or CRF are not yet known.

        Args:
            chunk_id:       Chunk identifier (e.g. ``'000000-000319'``).
            strategy:       Strategy name.
            attempt_number: Attempt number.
            resolution:     Output resolution string (e.g. ``'1920x800'``).
            crf:            CRF value used for this attempt.

        Returns:
            Path to encoded file for this attempt.
        """
        output_dir = self._get_output_dir(strategy)
        if resolution and crf is not None:
            filename = f"{chunk_id}.{resolution}.crf{crf:{PADDING_CRF}}.mkv"
        else:
            filename = f"{chunk_id}.mkv"
        return output_dir / filename

    def _check_existing_encoding(
        self,
        chunk_id: str,
        strategy: str,
        targets: list[QualityTarget]
    ) -> tuple[bool, Path | None, float | None]:
        """Check if chunk already encoded and meets current targets.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
            targets: Quality targets to check against

        Returns:
            Tuple of (meets_targets, encoded_file_path, crf_used)
        """
        # Check progress tracker for this chunk+strategy
        chunk_state = self.progress_tracker.get_chunk_state(chunk_id, strategy)
        if chunk_state is None:
            return (False, None, None)

        strategy_state = chunk_state.strategies.get(strategy)
        if strategy_state is None or strategy_state.status != PhaseStatus.COMPLETED:
            return (False, None, None)

        # Find the successful attempt
        successful_attempt = None
        for attempt in strategy_state.attempts:
            if attempt.success:
                successful_attempt = attempt
                break

        if successful_attempt is None:
            return (False, None, None)

        # Check if metrics meet current targets
        all_met = True
        for target in targets:
            metric_key = f"{target.metric}_{target.statistic}"
            actual = successful_attempt.metrics.get(metric_key)
            if actual is None or actual < target.value:
                all_met = False
                break

        if all_met and successful_attempt.file_path and successful_attempt.file_path.exists():
            return (True, successful_attempt.file_path, successful_attempt.crf)

        return (False, None, None)

    def _encode_with_ffmpeg(
        self,
        chunk: ChunkInfo,
        strategy_config: StrategyConfig,
        crf: float,
        output_file: Path
    ) -> bool:
        """Encode chunk with FFmpeg.

        Args:
            chunk: Chunk information
            strategy_config: Strategy configuration
            crf: CRF value to use
            output_file: Output file path

        Returns:
            True if encoding succeeded, False otherwise
        """
        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Build FFmpeg command
        ffmpeg_args = strategy_config.to_ffmpeg_args(crf)

        # Inject crop filter when crop params are set and non-empty
        if self._crop_params and not self._crop_params.is_empty():
            ffmpeg_args = ["-vf", self._crop_params.to_ffmpeg_filter(), *ffmpeg_args]

        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-i", str(chunk.file_path),
            *ffmpeg_args,
            str(output_file)
        ]

        _logger.debug(f"Encoding command: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False
            )

            if result.returncode != 0:
                _logger.error(
                    f"FFmpeg encoding failed with code {result.returncode}: "
                    f"{result.stderr[:500]}"
                )
                return False

            if not output_file.exists():
                _logger.error(f"Encoded file not created: {output_file}")
                return False

            return True

        except Exception as e:
            _logger.error(f"Exception during encoding: {e}")
            return False

    def encode_chunk(
        self,
        chunk: ChunkInfo,
        reference: VideoMetadata,
        strategy: str,
        quality_targets: list[QualityTarget],
        initial_crf: float = 20.0,
        force: bool = False,
        max_attempts: int = 10    ) -> ChunkEncodingResult:
        """Encode single chunk, adjusting CRF until quality targets met.

        Args:
            chunk: Chunk information
            reference: Path to reference chunk (for quality comparison)
            strategy: Strategy string (e.g., 'slow+h265-aq')
            quality_targets: Quality targets to meet
            initial_crf: Initial CRF value (if no history available)
            force: If False, reuse existing encoding that meets targets
            max_attempts: Maximum encoding attempts before giving up

        Returns:
            ChunkEncodingResult with encoding outcome

        Note:
            Chunks are already cropped, no additional cropping needed.
        """
        _logger.info(fmt_chunk_start(strategy, chunk.chunk_id))

        # Check for existing encoding if not forcing
        if not force:
            meets_targets, encoded_file, crf_used = self._check_existing_encoding(
                chunk.chunk_id, strategy, quality_targets
            )
            if meets_targets:
                _logger.info(
                    f"Chunk {chunk.chunk_id} already encoded with {strategy} "
                    f"(CRF {crf_used:{PADDING_CRF}}), reusing"
                )
                return ChunkEncodingResult(
                    chunk_id=chunk.chunk_id,
                    strategy=strategy,
                    success=True,
                    final_crf=crf_used,
                    attempts=0,
                    encoded_file=encoded_file,
                    reused=True
                )

        # Parse strategy
        try:
            strategy_configs = self.config_manager.parse_strategy(strategy)
            if not strategy_configs:
                raise ValueError(f"Strategy '{strategy}' resolved to no configurations")
            strategy_config = strategy_configs[0]
        except ValueError as e:
            _logger.error(f"Invalid strategy '{strategy}': {e}")
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                error=str(e)
            )

        # Determine initial CRF
        # Use average of successful CRFs from other chunks if available
        avg_crf = self.progress_tracker.get_successful_crf_average(strategy)
        if avg_crf is not None:
            current_crf = avg_crf
            _logger.debug(
                f"Using average successful CRF {current_crf:.2f} as starting point"
            )
        else:
            current_crf = initial_crf
            _logger.debug(f"Using default initial CRF {current_crf:.2f}")

        # Initialize CRF history for smart adjustment
        history = CRFHistory()

        # Iterative encoding loop — no hard attempt limit; warn at _ATTEMPT_WARN_THRESHOLD
        attempt_number = 0
        final_encoded_file = None
        best_crf: float | None = None

        # Get CRF range for this codec so adjust_crf can interpolate properly
        strategy_config_obj = strategy_configs[0]
        crf_min, crf_max = strategy_config_obj.codec.crf_range

        while True:
            attempt_number += 1

            if attempt_number == THRESHOLD_ATTEMPTS_WARNING:
                _logger.warning(
                    fmt_chunk(strategy, chunk.chunk_id,
                              f"reached {THRESHOLD_ATTEMPTS_WARNING} attempts without meeting targets — "
                              "continuing search")
                )

            _logger.info(
                    fmt_chunk_attempt_start(strategy, chunk.chunk_id, attempt_number, current_crf)
                )

            # Encode chunk — use a temporary name first, then rename with resolution
            temp_output = self._get_output_dir(strategy) / f"{chunk.chunk_id}.attempt_{attempt_number:03d}.tmp.mkv"
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            encode_success = self._encode_with_ffmpeg(
                chunk, strategy_config, current_crf, temp_output
            )

            if not encode_success:
                error_msg = f"Encoding failed for chunk {chunk.chunk_id}"
                _logger.error(error_msg)
                return ChunkEncodingResult(
                    chunk_id=chunk.chunk_id,
                    strategy=strategy,
                    success=False,
                    attempts=attempt_number,
                    error=error_msg
                )

            # Determine output resolution from encoded file and rename to final path
            resolution = _probe_resolution(temp_output)
            output_file = self._get_attempt_path(chunk.chunk_id, strategy, resolution=resolution, crf=current_crf)
            try:
                temp_output.replace(output_file)
            except OSError:
                temp_output.rename(output_file)

            # Evaluate quality
            metrics_dir = output_file.parent / f"{output_file.stem}_metrics"
            evaluation = self.quality_evaluator.evaluate_chunk(
                encoded=output_file,
                reference=reference,
                targets=quality_targets,
                output_dir=metrics_dir
            )

            # Extract metrics for storage. Select only relevant ones - metrics with targets.
            targets_set = {f"{t.metric}_{t.statistic}" for t in quality_targets}
            metrics_dict: dict[str, float] = {target: float(value)
                                              for metric in evaluation.metrics
                                              for stat, value in evaluation.metrics[metric].items()
                                              if (target:=f"{metric.value}_{stat}") in targets_set}

            # Record attempt in progress tracker
            attempt_info = AttemptInfo(
                attempt_number=attempt_number,
                crf=current_crf,
                metrics=metrics_dict,
                success=evaluation.targets_met,
                file_path=output_file if evaluation.targets_met else None,
                file_size=output_file.stat().st_size if evaluation.targets_met else None
            )
            self.progress_tracker.update_chunk(ChunkUpdate(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                attempt=attempt_info,
            ))

            # Add to history
            history.add_attempt(current_crf, metrics_dict)

            # Test if we found better CRF:
            best_string: str = ""
            if evaluation.targets_met:
                if final_encoded_file is None or current_crf > (best_crf or 0):
                    best_crf = current_crf
                    final_encoded_file = output_file
                    best_string = " NEW BEST"

            # Log metrics summary — condensed, info level with pass/fail emoji
            metric_summary = ", ".join(f"{k}={v:.1f}" for k, v in metrics_dict.items())
            pass_fail:str = f"{SUCCESS_SYMBOL_MINOR} pass" if evaluation.targets_met else f"{FAILURE_SYMBOL_MINOR} miss"
            _logger.info(
                fmt_chunk_attempt_result(strategy, chunk.chunk_id, attempt_number,
                            f"{pass_fail} with CRF {current_crf:.2f} ({metric_summary}){best_string}")
            )

            # Adjust CRF — continues searching even after a pass to find optimal
            next_crf = adjust_crf(
                current_crf, metrics_dict, quality_targets, history,
                crf_min=crf_min, crf_max=crf_max,
            )

            if next_crf is None:
                if final_encoded_file is not None:
                    _logger.info(fmt_chunk_final(strategy, chunk.chunk_id, best_crf, attempt_number))
                else:
                    _logger.warning(
                        "CRF search space exhausted for chunk %s strategy %s after %d attempts",
                        chunk.chunk_id, strategy, attempt_number,
                    )
                break

            _logger.debug("Adjusting CRF from %.2f to %.2f", current_crf, next_crf)
            current_crf = next_crf

        # Check final result
        if final_encoded_file is not None:
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=True,
                final_crf=best_crf,
                attempts=attempt_number,
                encoded_file=final_encoded_file,
                reused=False
            )
        else:
            error_msg = f"Failed to meet quality targets after {attempt_number} attempts"
            _logger.error(f"Chunk {chunk.chunk_id}: {error_msg}")
            return ChunkEncodingResult(
                chunk_id=chunk.chunk_id,
                strategy=strategy,
                success=False,
                attempts=attempt_number,
                error=error_msg
            )



class ChunkQueue:
    """Manages queue of chunks for parallel encoding.

    Prioritizes completing started chunks before starting new ones.
    """

    def __init__(self, chunks: list[ChunkInfo], strategies: list[str]):
        """Initialize chunk queue.

        Args:
            chunks: List of chunks to encode
            strategies: List of strategies to apply
        """
        self.chunks = chunks
        self.strategies = strategies
        self._pending: list[tuple[ChunkInfo, str]] = []
        self._in_progress: set[tuple[str, str]] = set()
        self._completed: set[tuple[str, str]] = set()

        # Build initial queue (all chunk+strategy combinations)
        for chunk in chunks:
            for strategy in strategies:
                self._pending.append((chunk, strategy))

    def get_next(self) -> tuple[ChunkInfo, str] | None:
        """Get next chunk+strategy to encode.

        Prioritizes completing started chunks before starting new ones.

        Returns:
            Tuple of (chunk, strategy) or None if queue empty
        """
        if not self._pending:
            return None

        # Check if any in-progress chunks have other strategies pending
        for chunk, strategy in self._pending:
            if any((chunk.chunk_id, s) in self._in_progress for s in self.strategies):
                # This chunk has work in progress, prioritize it
                self._pending.remove((chunk, strategy))
                self._in_progress.add((chunk.chunk_id, strategy))
                return (chunk, strategy)

        # No in-progress chunks, take first pending
        chunk, strategy = self._pending.pop(0)
        self._in_progress.add((chunk.chunk_id, strategy))
        return (chunk, strategy)

    def mark_complete(self, chunk_id: str, strategy: str) -> None:
        """Mark chunk+strategy as complete.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
        """
        self._in_progress.discard((chunk_id, strategy))
        self._completed.add((chunk_id, strategy))

    def mark_failed(self, chunk_id: str, strategy: str) -> None:
        """Mark chunk+strategy as failed.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
        """
        self._in_progress.discard((chunk_id, strategy))

    def is_empty(self) -> bool:
        """Check if queue is empty.

        Returns:
            True if no more work to do
        """
        return len(self._pending) == 0 and len(self._in_progress) == 0

    def get_progress(self) -> tuple[int, int]:
        """Get current progress.

        Returns:
            Tuple of (completed, total)
        """
        total = len(self.chunks) * len(self.strategies)
        completed = len(self._completed)
        return (completed, total)


async def _encode_chunk_async(
    encoder: ChunkEncoder,
    chunk: ChunkInfo,
    reference: VideoMetadata,
    strategy: str,
    quality_targets: list[QualityTarget],
    initial_crf: float,
    force: bool
) -> ChunkEncodingResult:
    """Async wrapper for chunk encoding.

    Args:
        encoder: ChunkEncoder instance
        chunk: Chunk to encode
        reference: Reference chunk path
        strategy: Strategy to use
        quality_targets: Quality targets
        initial_crf: Initial CRF value
        force: Whether to force re-encoding

    Returns:
        ChunkEncodingResult
    """
    # Run encoding in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        encoder.encode_chunk,
        chunk,
        reference,
        strategy,
        quality_targets,
        initial_crf,
        force
    )


async def _encode_chunks_parallel(
    encoder: ChunkEncoder,
    chunks: list[ChunkInfo],
    reference_dir: Path,
    strategies: list[str],
    quality_targets: list[QualityTarget],
    max_parallel: int,
    force: bool,
    bar: object | None = None,
) -> EncodingResult:
    """Encode chunks in parallel with semaphore control.

    Args:
        encoder: ChunkEncoder instance
        chunks: List of chunks to encode
        reference_dir: Directory containing reference chunks
        strategies: List of strategies to use
        quality_targets: Quality targets to meet
        max_parallel: Maximum concurrent encodings
        force: Whether to force re-encoding
        bar: Optional ``alive_bar`` handle; incremented on each chunk completion
             (success or failure) per Req 4.5, 4.7.

    Returns:
        EncodingResult with all encoding outcomes
    """
    result = EncodingResult()
    queue = ChunkQueue(chunks, strategies)
    semaphore = asyncio.Semaphore(max_parallel)
    counter_failed = 0


    async def encode_worker():
        """Worker coroutine for encoding chunks."""
        nonlocal counter_failed
        while not queue.is_empty():
            # Get next chunk+strategy
            next_item = queue.get_next()
            if next_item is None:
                break

            chunk, strategy = next_item

            async with semaphore:
                # Find reference chunk
                reference = reference_dir / chunk.file_path.name
                if not reference.exists():
                    _logger.error(f"Reference chunk not found: {reference}")
                    queue.mark_failed(chunk.chunk_id, strategy)
                    result.failed_chunks.append(chunk.chunk_id)
                    update_bar(bar, increment=0)
                    continue

                # Get initial CRF from progress tracker
                avg_crf = encoder.progress_tracker.get_successful_crf_average(strategy)
                initial_crf = avg_crf if avg_crf is not None else 20.0

                # Encode chunk
                chunk_result = await _encode_chunk_async(
                    encoder,
                    chunk,
                    reference,
                    strategy,
                    quality_targets,
                    initial_crf,
                    force
                )

                # Update result
                if chunk_result.success:
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    result.encoded_chunks[chunk.chunk_id][strategy] = chunk_result.encoded_file

                    if chunk_result.reused:
                        result.reused_count += 1
                    else:
                        result.encoded_count += 1

                    queue.mark_complete(chunk.chunk_id, strategy)
                else:
                    queue.mark_failed(chunk.chunk_id, strategy)
                    result.failed_chunks.append(chunk.chunk_id)
                    result.success = False
                    counter_failed += 1

                update_bar(bar, increment=int(chunk_result.success))


    # Start worker tasks
    workers = [asyncio.create_task(encode_worker()) for _ in range(max_parallel)]

    # Wait for all workers to complete
    await asyncio.gather(*workers)

    return result


def encode_all_chunks(
    chunks:           list[ChunkInfo],
    reference_dir:    Path,
    strategies:       list[str],
    quality_targets:  list[QualityTarget],
    work_dir:         Path,
    config_manager:   ConfigManager,
    progress_tracker: ProgressTracker,
    max_parallel:     int = 2,
    force:            bool = False,
    dry_run:          bool = False,
    crop_params:      CropParams | None = None,
) -> EncodingResult:
    """Encode all chunks with quality-targeted CRF adjustment.

    This is the main entry point for the encoding phase. It handles:
    - Scanning for existing encodings
    - Evaluating existing encodings against current targets
    - Parallel encoding of chunks that need work
    - Dry-run mode for reporting what would be done

    Args:
        chunks:           List of chunks to encode
        reference_dir:    Directory containing reference chunks (already cropped)
        strategies:       List of encoding strategies to use
        quality_targets:  Quality targets to meet
        work_dir:         Working directory for artifacts
        config_manager:   Configuration manager
        progress_tracker: Progress tracker
        max_parallel:     Maximum concurrent encoding processes
        force:            If False, reuse existing encodings that meet current targets
        dry_run:          If True, only report what would be done without encoding
        crop_params:      Crop parameters to apply uniformly to every chunk attempt.
                          When ``None``, no cropping is applied.

    Returns:
        EncodingResult with paths to encoded chunks and statistics
    """
    _logger.info(
        f"Encoding phase: {len(chunks)} chunks, {len(strategies)} strategies, "
        f"{len(quality_targets)} quality targets"
    )

    if dry_run:
        _logger.info("[DRY-RUN] Scanning for existing encodings...")
        result = EncodingResult()

        # Check what work needs to be done
        encoder = ChunkEncoder(
            config_manager=config_manager,
            quality_evaluator=QualityEvaluator(work_dir),
            progress_tracker=progress_tracker,
            work_dir=work_dir,
            crop_params=crop_params,
        )

        needs_work = []
        for chunk in chunks:
            for strategy in strategies:
                meets_targets, encoded_file, crf = encoder._check_existing_encoding(
                    chunk.chunk_id, strategy, quality_targets
                )
                if meets_targets:
                    if chunk.chunk_id not in result.encoded_chunks:
                        result.encoded_chunks[chunk.chunk_id] = {}
                    result.encoded_chunks[chunk.chunk_id][strategy] = encoded_file
                    result.reused_count += 1
                else:
                    needs_work.append((chunk.chunk_id, strategy))

        _logger.info(f"[DRY-RUN] Existing encodings: {result.reused_count}")
        _logger.info(f"[DRY-RUN] Need encoding: {len(needs_work)}")

        if needs_work:
            _logger.info("[DRY-RUN] Would encode:")
            for chunk_id, strategy in needs_work[:10]:  # Show first 10
                _logger.info(f"[DRY-RUN]   - {chunk_id} with {strategy}")
            if len(needs_work) > 10:
                _logger.info(f"[DRY-RUN]   ... and {len(needs_work) - 10} more")
            _logger.info("[DRY-RUN] Status: Needs work")
        else:
            _logger.info("[DRY-RUN] Status: Complete (all chunks already encoded)")

        return result

    # Create encoder
    encoder = ChunkEncoder(
        config_manager=config_manager,
        quality_evaluator=QualityEvaluator(work_dir),
        progress_tracker=progress_tracker,
        work_dir=work_dir,
        crop_params=crop_params,
    )

    # Run parallel encoding
    _logger.info(f"Starting parallel encoding with {max_parallel} workers")
    total_items = len(chunks) * len(strategies)
    with alive_bar(total_items, title="Encoding", unit=PROGRESS_CHUNK_UNIT) as bar:
        result = asyncio.run(
            _encode_chunks_parallel(
                encoder=encoder,
                chunks=chunks,
                reference_dir=reference_dir,
                strategies=strategies,
                quality_targets=quality_targets,
                max_parallel=max_parallel,
                force=force,
                bar=bar,
            )
        )

    # Log summary
    _logger.info(
        f"Encoding complete: {result.encoded_count} newly encoded, "
        f"{result.reused_count} reused, {len(result.failed_chunks)} failed"
    )

    if result.failed_chunks:
        _logger.error(f"Failed chunks: {', '.join(result.failed_chunks)}")

    return result
