"""Public API for the quality-based encoding pipeline.

This module provides the main entry points for programmatic access to the pipeline.
All functions are designed to be used both from CLI and from external code.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import TEMP_SUFFIX
from pyqenc.models import PipelineConfig
from pyqenc.orchestrator import PipelineOrchestrator, PipelineResult
from pyqenc.state import JobStateManager

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyqenc.models import ChunkingMode
    from pyqenc.phases.audio import AudioResult
    from pyqenc.phases.chunking import ChunkingResult
    from pyqenc.phases.encoding import EncodingResult
    from pyqenc.phases.extraction import ExtractionResult
    from pyqenc.phases.merge import MergeResult

def run_pipeline(
    config: PipelineConfig,
    dry_run: bool = True,
    max_phases: int | None = None
) -> PipelineResult:
    """Execute complete end-to-end pipeline.

    This is the main entry point for running the entire pipeline from extraction
    to final merge. The pipeline automatically handles resumption by checking for
    existing artifacts and reusing them when valid.

    Args:
        config: Pipeline configuration with all required parameters
        dry_run: If True, only report what would be done without executing (default: True)
        max_phases: Maximum number of phases to execute (None = all phases)

    Returns:
        PipelineResult with execution summary including:
        - success: Whether pipeline completed successfully
        - phases_executed: List of phases that performed work
        - phases_reused: List of phases that reused existing artifacts
        - phases_needing_work: List of phases that need work (dry-run mode)
        - output_files: List of final output file paths
        - error: Error message if pipeline failed

    Raises:
        ValueError: If configuration is invalid
        FileNotFoundError: If source video doesn't exist
        PermissionError: If working directory is not writable

    Example:
        >>> from pathlib import Path
        >>> from pyqenc.api import run_pipeline
        >>> from pyqenc.models import PipelineConfig
        >>>
        >>> config = PipelineConfig(
        ...     source_video=Path("movie.mkv"),
        ...     work_dir=Path("./work"),
        ...     quality_targets=["vmaf-min:95"],
        ...     strategies=["slow+h265-aq"],
        ... )
        >>> result = run_pipeline(config, dry_run=False)
        >>> if result.success:
        ...     print(f"Output: {result.output_files[0]}")
    """
    # Validate configuration
    if not config.source_video.exists():
        raise FileNotFoundError(f"Source video not found: {config.source_video}")

    config.work_dir.mkdir(parents=True, exist_ok=True)

    if not config.work_dir.is_dir():
        raise ValueError(f"Work directory is not a directory: {config.work_dir}")

    # Initialize state manager
    state_manager = JobStateManager(
        work_dir=config.work_dir,
        source_video=config.source_video,
        force=getattr(config, "force", False),
    )

    # Create orchestrator and run pipeline
    orchestrator = PipelineOrchestrator(config, state_manager)
    try:
        return orchestrator.run(dry_run=dry_run, max_phases=max_phases)
    except BaseException:
        logger.warning("Unhandled exception — re-raising.")
        raise


def extract_streams(
    source_video: Path,
    output_dir: Path,
    include: str | None = None,
    exclude: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> "ExtractionResult":
    """Extract video and audio streams from source MKV.

    Extracts all video and audio streams from the source MKV file, optionally
    filtering by regex patterns applied uniformly to all stream types.
    Crop detection is NOT performed here — use the orchestrator or call
    ``detect_crop_parameters`` separately after extraction.

    Args:
        source_video: Path to source MKV file
        output_dir: Directory for extracted streams
        include: Regex pattern applied to ALL stream types; only matching streams
                 are extracted (e.g. ``".*eng.*"``). ``None`` means include all.
        exclude: Regex pattern applied to ALL stream types; matching streams are
                 skipped (e.g. ``"attachment"``). ``None`` means exclude none.
        force: If True, re-extract even if files exist (default: False)
        dry_run: If True, only report status without extracting (default: False)

    Returns:
        ExtractionResult with video/audio metadata and phase outcome.

    Raises:
        FileNotFoundError: If source video doesn't exist
    """
    from pyqenc.phases.extraction import extract_streams as _extract_streams

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    output_dir.mkdir(parents=True, exist_ok=True)

    return _extract_streams(
        source_video=source_video,
        output_dir=output_dir,
        include=include,
        exclude=exclude,
        force=force,
        dry_run=dry_run,
    )


def chunk_video(
    video_file: Path,
    output_dir: Path,
    crop_params: str | None = None,
    scene_threshold: float = 0.3,
    min_scene_length: int = 24,
    chunking_mode: "ChunkingMode | None" = None,
    force: bool = False,
    dry_run: bool = False
) -> "ChunkingResult":
    """Split video into scene-based chunks.

    Detects scene boundaries and splits the video into separate chunk files.
    By default uses FFV1 lossless re-encoding for frame-perfect splits. Pass
    ``chunking_mode=ChunkingMode.REMUX`` to fall back to stream-copy (faster,
    smaller chunks, but boundaries may snap to the nearest I-frame).

    Runs inputs discovery to verify that the extraction phase has produced its
    outputs before proceeding (Req 11.1).

    Args:
        video_file: Path to video file to chunk
        output_dir: Directory for chunk output
        crop_params: Crop parameters to apply (format: "top bottom left right")
        scene_threshold: Scene detection sensitivity 0.0-1.0 (default: 0.3)
        min_scene_length: Minimum frames per chunk (default: 24)
        chunking_mode: ChunkingMode.LOSSLESS (default) or ChunkingMode.REMUX
        force: If True, re-chunk even if chunks exist (default: False)
        dry_run: If True, only report status without chunking (default: False)

    Returns:
        ChunkingResult with:
        - chunks: List of ChunkInfo objects with chunk details
        - total_frames: Total frame count across all chunks
        - reused: True if existing chunks were reused
        - needs_work: True if chunking would be performed (dry-run)
        - success: Whether chunking succeeded
        - error: Error message if chunking failed

    Raises:
        FileNotFoundError: If video file doesn't exist
        ValueError: If scene threshold or min length is invalid

    Example:
        >>> from pathlib import Path
        >>> from pyqenc.api import chunk_video
        >>>
        >>> result = chunk_video(
        ...     video_file=Path("./work/extracted/video_001.mkv"),
        ...     output_dir=Path("./work/chunks"),
        ...     crop_params="140 140 0 0",
        ... )
        >>> if result.success:
        ...     print(f"Created {len(result.chunks)} chunks")
        ...     print(f"Total frames: {result.total_frames}")
    """
    from pyqenc.models import ChunkingMode, CropParams
    from pyqenc.phases.chunking import chunk_video as _chunk_video
    from pyqenc.state import JobState, JobStateManager
    from pyqenc.models import VideoMetadata

    if not video_file.exists():
        raise FileNotFoundError(f"Video file not found: {video_file}")

    if not 0.0 <= scene_threshold <= 1.0:
        raise ValueError(f"Scene threshold must be between 0.0 and 1.0, got {scene_threshold}")

    if min_scene_length < 1:
        raise ValueError(f"Minimum scene length must be positive, got {min_scene_length}")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Parse crop params if provided
    parsed_crop_params = None
    if crop_params:
        try:
            parsed_crop_params = CropParams.parse(crop_params)
        except ValueError as e:
            raise ValueError(f"Invalid crop parameters: {e}") from e

    # Derive work_dir from output_dir (output_dir is work_dir/chunks)
    work_dir = output_dir.parent
    state_manager = JobStateManager(work_dir=work_dir, source_video=video_file)
    existing_job = state_manager.load_job()
    job = existing_job if existing_job is not None else JobState(source=VideoMetadata(path=video_file))

    return _chunk_video(
        video_file=video_file,
        output_dir=output_dir,
        state_manager=state_manager,
        job=job,
        chunking_mode=chunking_mode if chunking_mode is not None else ChunkingMode.LOSSLESS,
        dry_run=dry_run,
        standalone=True,
    )


def encode_chunks(
    chunks_dir: Path,
    strategies: list[str]|None,
    quality_targets: list[str]|None,
    work_dir: Path,
    max_parallel: int = 2,
    force: bool = False,
    dry_run: bool = False
) -> "EncodingResult":
    """Encode all chunks to meet quality targets.

    Encodes each chunk with specified strategies, iteratively adjusting CRF
    until all quality targets are met. Automatically reuses existing encodings
    that meet current targets. Supports parallel encoding for efficiency.

    Args:
        chunks_dir: Directory containing chunks to encode
        strategies: List of encoding strategies (e.g., ["slow+h265-aq"])
        quality_targets: List of quality targets (e.g., ["vmaf-min:95"])
        work_dir: Working directory for encoded chunks and metrics
        max_parallel: Maximum concurrent encoding processes (default: 2)
        force: If True, re-encode even if valid encodings exist (default: False)
        dry_run: If True, only report status without encoding (default: False)

    Returns:
        EncodingResult with:
        - encoded_chunks: Dict mapping chunk_id -> strategy -> encoded file path
        - reused_count: Number of chunks reused from previous runs
        - encoded_count: Number of chunks newly encoded
        - success: Whether encoding succeeded
        - failed_chunks: List of chunk IDs that failed encoding
        - error: Error message if encoding failed

    Raises:
        FileNotFoundError: If chunks directory doesn't exist
        ValueError: If strategies or quality targets are invalid

    Example:
        >>> from pathlib import Path
        >>> from pyqenc.api import encode_chunks
        >>>
        >>> result = encode_chunks(
        ...     chunks_dir=Path("./work/chunks"),
        ...     strategies=["slow+h265-aq"],
        ...     quality_targets=["vmaf-min:95", "ssim-med:0.98"],
        ...     work_dir=Path("./work"),
        ...     max_parallel=2,
        ... )
        >>> if result.success:
        ...     print(f"Encoded {result.encoded_count} chunks")
        ...     print(f"Reused {result.reused_count} chunks")
    """
    from pyqenc.config import ConfigManager
    from pyqenc.models import QualityTarget
    from pyqenc.phases.encoding import ChunkInfo, encode_all_chunks

    if not chunks_dir.exists():
        raise FileNotFoundError(f"Chunks directory not found: {chunks_dir}")

    if not strategies:
        raise ValueError("At least one strategy must be specified")

    if not quality_targets:
        raise ValueError("At least one quality target must be specified")

    if max_parallel < 1:
        raise ValueError(f"Max parallel must be positive, got {max_parallel}")

    # Load chunk files
    chunk_files = sorted(chunks_dir.glob("*.mkv"))
    if not chunk_files:
        raise FileNotFoundError(f"No chunks found in {chunks_dir}")

    # Create ChunkInfo objects
    chunks = []
    for chunk_file in chunk_files:
        chunk_id = chunk_file.stem
        chunks.append(ChunkInfo(
            chunk_id=chunk_id,
            file_path=chunk_file,
            start_frame=0,
            end_frame=0,
            frame_count=0,
            duration=0.0
        ))

    # Parse quality targets
    parsed_targets = []
    for target_str in quality_targets:
        try:
            parsed_targets.append(QualityTarget.parse(target_str))
        except ValueError as e:
            raise ValueError(f"Invalid quality target '{target_str}': {e}") from e

    # Initialize config manager and state manager
    config_manager = ConfigManager()
    state_manager = JobStateManager(work_dir=work_dir, source_video=chunks_dir)

    return encode_all_chunks(
        chunks=chunks,
        reference_dir=chunks_dir,
        strategies=strategies,
        quality_targets=parsed_targets,
        work_dir=work_dir,
        config_manager=config_manager,
        max_parallel=max_parallel,
        force=force,
        dry_run=dry_run,
        state_manager=state_manager,
        standalone=True,
    )


def process_audio(
    audio_dir: Path,
    output_dir: Path,
    audio_convert: str | None = None,
    audio_codec: str | None = None,
    audio_base_bitrate: str | None = None,
    dry_run: bool = False
) -> "AudioResult":
    """Process audio streams with normalization strategies.

    Processes extracted audio files to generate normalized stereo variants
    for day mode (standard normalization) and night mode (with dynamic range
    compression). Outputs AAC format for broad compatibility.

    Args:
        audio_dir: Directory containing audio files to process
        output_dir: Directory for processed audio output
        audio_convert: Regex pattern selecting processed audio files to convert to the
                       final delivery format. Overrides ``audio_output.convert_filter``
                       from config when provided.
        audio_codec: Override audio codec for all conversion profiles (e.g. ``'aac'``).
        audio_base_bitrate: Base bitrate for 2.0 stereo conversion (e.g. ``'192k'``).
                            Bitrates for other channel layouts are scaled proportionally.
        dry_run: If True, only report status without processing (default: False)

    Returns:
        AudioResult with:
        - day_mode_files: List of day mode audio file paths
        - night_mode_files: List of night mode audio file paths
        - reused: True if existing files were reused
        - needs_work: True if processing would be performed (dry-run)
        - success: Whether processing succeeded
        - error: Error message if processing failed

    Raises:
        FileNotFoundError: If audio directory doesn't exist

    Example:
        >>> from pathlib import Path
        >>> from pyqenc.api import process_audio
        >>>
        >>> result = process_audio(
        ...     audio_dir=Path("./work/extracted"),
        ...     output_dir=Path("./work/audio"),
        ... )
        >>> if result.success:
        ...     print(f"Processed {len(result.day_mode_files)} audio streams")
    """
    from pyqenc.phases.audio import process_audio_streams
    from pyqenc.phases.recovery import discover_inputs

    if not audio_dir.exists():
        raise FileNotFoundError(f"Audio directory not found: {audio_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Inputs discovery: verify extraction phase produced audio outputs (Req 11.1)
    work_dir = audio_dir.parent
    discovery = discover_inputs("audio", work_dir)
    if not discovery.ok:
        from pyqenc.phases.audio import AudioResult
        return AudioResult(
            day_mode_files=[], night_mode_files=[],
            reused=False, needs_work=False, success=False,
            error=discovery.error,
        )

    audio_files = list(audio_dir.glob("audio_*.mka"))
    if not audio_files:
        raise FileNotFoundError(f"No audio files found in {audio_dir}")

    return process_audio_streams(
        audio_files=audio_files,
        output_dir=output_dir,
        audio_convert=audio_convert,
        audio_codec=audio_codec,
        audio_base_bitrate=audio_base_bitrate,
        dry_run=dry_run,
    )


def merge_final(
    encoded_dir: Path,
    audio_dir: Path,
    output_dir: Path,
    source_frame_count: int | None = None,
    verify_frames: bool = True,
    dry_run: bool = False
) -> "MergeResult":
    """Merge encoded chunks and audio into final MKV files.

    Concatenates all encoded chunks for each strategy and merges with
    processed audio streams into final MKV files. Produces separate output
    files for each encoding strategy found in the encoded directory.

    Args:
        encoded_dir: Directory containing encoded chunks organized by strategy
        audio_dir: Directory containing processed audio files
        output_dir: Directory for final output MKV files
        source_frame_count: Expected frame count for verification (optional)
        verify_frames: If True, verify frame count matches source (default: True)
        dry_run: If True, only report status without merging (default: False)

    Returns:
        MergeResult with:
        - output_files: Dictionary mapping strategy names to output file paths
        - frame_counts: Dictionary mapping strategy names to frame counts
        - reused: True if existing files were reused
        - needs_work: True if merging would be performed (dry-run)
        - success: Whether merge succeeded
        - error: Error message if merge failed

    Raises:
        FileNotFoundError: If encoded or audio directory doesn't exist

    Example:
        >>> from pathlib import Path
        >>> from pyqenc.api import merge_final
        >>>
        >>> result = merge_final(
        ...     encoded_dir=Path("./work/encoded"),
        ...     audio_dir=Path("./work/audio"),
        ...     output_dir=Path("./work/final"),
        ... )
        >>> if result.success:
        ...     for strategy, output_file in result.output_files.items():
        ...         print(f"{strategy}: {output_file}")
        ...         print(f"  Frame count: {result.frame_counts[strategy]}")
    """
    from pyqenc.phases.merge import merge_final_video
    from pyqenc.phases.recovery import discover_inputs

    if not encoded_dir.exists():
        raise FileNotFoundError(f"Encoded directory not found: {encoded_dir}")

    if not audio_dir.exists():
        raise FileNotFoundError(f"Audio directory not found: {audio_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Inputs discovery: verify encoding phase produced outputs (Req 11.1)
    work_dir = encoded_dir.parent
    discovery = discover_inputs("merge", work_dir)
    if not discovery.ok:
        from pyqenc.phases.merge import MergeResult
        from pyqenc.models import PhaseOutcome
        return MergeResult(
            output_files={}, frame_counts={},
            final_metrics={}, targets_met={}, metrics_plots={},
            outcome=PhaseOutcome.FAILED,
            error=discovery.error,
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect encoded chunks organized by chunk_id and strategy
    # Structure: {chunk_id: {strategy: path}}
    encoded_chunks = {}

    # Scan encoded directory for all strategies
    for strategy_dir in encoded_dir.iterdir():
        if not strategy_dir.is_dir():
            continue

        strategy_name = strategy_dir.name

        # Find all successful encodings for this strategy
        # Look for files like chunk_0001_attempt_001.mkv
        # We want the final successful attempt for each chunk
        chunk_files = {}

        for encoded_file in strategy_dir.glob("*.mkv"):
            if TEMP_SUFFIX in encoded_file.name:
                continue  # Skip temporary files

            # Extract chunk_id from filename
            # Format: chunk_0001_attempt_001.mkv
            parts = encoded_file.stem.split("_")
            if len(parts) >= 2:
                chunk_id = f"{parts[0]}_{parts[1]}"  # e.g., "chunk_0001"

                # Keep track of highest attempt number for each chunk
                if chunk_id not in chunk_files:
                    chunk_files[chunk_id] = encoded_file
                else:
                    # Compare attempt numbers, keep higher
                    current_attempt = int(chunk_files[chunk_id].stem.split("_")[-1])
                    new_attempt = int(parts[-1])
                    if new_attempt > current_attempt:
                        chunk_files[chunk_id] = encoded_file

        # Add to encoded_chunks structure
        for chunk_id, chunk_path in chunk_files.items():
            if chunk_id not in encoded_chunks:
                encoded_chunks[chunk_id] = {}
            encoded_chunks[chunk_id][strategy_name] = chunk_path

    # Collect audio files — all AAC delivery files produced by the audio phase
    audio_files = sorted(audio_dir.glob("*.aac"))

    return merge_final_video(
        encoded_chunks=encoded_chunks,
        audio_files=audio_files,
        output_dir=output_dir,
        source_frame_count=source_frame_count,
        verify_frames=verify_frames,
        force=False,
        dry_run=dry_run,
    )


# Export all public functions
__all__ = [
    "run_pipeline",
    "extract_streams",
    "chunk_video",
    "encode_chunks",
    "process_audio",
    "merge_final",
]
