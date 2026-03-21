"""Public API for the quality-based encoding pipeline.

This module provides the main entry points for programmatic access to the pipeline.
All functions are designed to be used both from CLI and from external code.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    PipelineConfig,
    QualityTarget,
    Strategy,
)
from pyqenc.orchestrator import PipelineOrchestrator, PipelineResult
from pyqenc.phase import _build_registry

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyqenc.phases.audio import AudioPhaseResult
    from pyqenc.phases.chunking import ChunkingPhaseResult
    from pyqenc.phases.encoding import EncodingPhaseResult
    from pyqenc.phases.extraction import ExtractionPhaseResult
    from pyqenc.phases.merge import MergePhaseResult


def run_pipeline(
    config: PipelineConfig,
    dry_run: bool = True,
) -> PipelineResult:
    """Execute complete end-to-end pipeline.

    This is the main entry point for running the entire pipeline from extraction
    to final merge. The pipeline automatically handles resumption by checking for
    existing artifacts and reusing them when valid.

    Args:
        config:  Pipeline configuration with all required parameters.
        dry_run: If True, only report what would be done without executing (default: True).

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
    """
    if not config.source_video.exists():
        raise FileNotFoundError(f"Source video not found: {config.source_video}")

    config.work_dir.mkdir(parents=True, exist_ok=True)

    if not config.work_dir.is_dir():
        raise ValueError(f"Work directory is not a directory: {config.work_dir}")

    orchestrator = PipelineOrchestrator(config)
    try:
        return orchestrator.run(dry_run=dry_run)
    except BaseException:
        logger.warning("Unhandled exception — re-raising.")
        raise


def _minimal_config(
    source_video:    Path,
    work_dir:        Path,
    quality_targets: list[QualityTarget] | None = None,
    strategies:      list[Strategy] | None = None,
    include:         str | None = None,
    exclude:         str | None = None,
    chunking_mode:   ChunkingMode = ChunkingMode.LOSSLESS,
    max_parallel:    int = 2,
    force:           bool = False,
) -> PipelineConfig:
    """Build a minimal ``PipelineConfig`` for standalone phase invocations.

    Args:
        source_video:    Path to source video file.
        work_dir:        Working directory for intermediate files.
        quality_targets: Quality targets (defaults to empty list).
        strategies:      Encoding strategies (defaults to empty list).
        include:         Stream include regex.
        exclude:         Stream exclude regex.
        chunking_mode:   Chunking mode (default: LOSSLESS).
        max_parallel:    Max parallel encoding processes.
        force:           Force re-execution flag.

    Returns:
        Minimal ``PipelineConfig`` suitable for standalone phase construction.
    """
    return PipelineConfig(
        source_video    = source_video,
        work_dir        = work_dir,
        quality_targets = quality_targets or [],
        strategies      = strategies or [],
        optimize        = False,
        all_strategies  = True,
        max_parallel    = max_parallel,
        include         = include,
        exclude         = exclude,
        cleanup         = CleanupLevel.NONE,
        chunking_mode   = chunking_mode,
        force           = force,
    )


def extract_streams(
    source_video: Path,
    output_dir:   Path,
    include:      str | None = None,
    exclude:      str | None = None,
    force:        bool = False,
    dry_run:      bool = False,
) -> "ExtractionPhaseResult":
    """Extract video and audio streams from source MKV.

    Constructs an ``ExtractionPhase`` with its full dependency chain
    (including ``JobPhase``) and delegates to ``phase.run()``.

    Args:
        source_video: Path to source MKV file.
        output_dir:   Directory for extracted streams (parent is used as work_dir).
        include:      Regex pattern applied to ALL stream types; only matching streams
                      are extracted.  ``None`` means include all.
        exclude:      Regex pattern applied to ALL stream types; matching streams are
                      skipped.  ``None`` means exclude none.
        force:        If True, re-extract even if files exist (default: False).
        dry_run:      If True, only report status without extracting (default: False).

    Returns:
        ``ExtractionPhaseResult`` from the phase object.

    Raises:
        FileNotFoundError: If source video doesn't exist.
    """
    from pyqenc.phases.extraction import ExtractionPhase

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    work_dir = output_dir.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    config   = _minimal_config(source_video=source_video, work_dir=work_dir, include=include, exclude=exclude, force=force)
    registry = _build_registry(config)
    phase    = registry[ExtractionPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def chunk_video(
    video_file:       Path,
    output_dir:       Path,
    scene_threshold:  float = 0.3,
    min_scene_length: int = 24,
    chunking_mode:    ChunkingMode | None = None,
    force:            bool = False,
    dry_run:          bool = False,
) -> "ChunkingPhaseResult":
    """Split video into scene-based chunks.

    Constructs a ``ChunkingPhase`` with its full dependency chain and delegates
    to ``phase.run()``.

    Args:
        video_file:       Path to video file to chunk (used as source_video).
        output_dir:       Directory for chunk output (parent is used as work_dir).
        scene_threshold:  Scene detection sensitivity 0.0-1.0 (default: 0.3).
        min_scene_length: Minimum frames per chunk (default: 24).
        chunking_mode:    ChunkingMode.LOSSLESS (default) or ChunkingMode.REMUX.
        force:            If True, re-chunk even if chunks exist (default: False).
        dry_run:          If True, only report status without chunking (default: False).

    Returns:
        ``ChunkingPhaseResult`` from the phase object.

    Raises:
        FileNotFoundError: If video file doesn't exist.
        ValueError: If scene threshold or min length is invalid.
    """
    from pyqenc.phases.chunking import ChunkingPhase

    if not video_file.exists():
        raise FileNotFoundError(f"Video file not found: {video_file}")

    if not 0.0 <= scene_threshold <= 1.0:
        raise ValueError(f"Scene threshold must be between 0.0 and 1.0, got {scene_threshold}")

    if min_scene_length < 1:
        raise ValueError(f"Minimum scene length must be positive, got {min_scene_length}")

    work_dir = output_dir.parent
    work_dir.mkdir(parents=True, exist_ok=True)

    config   = _minimal_config(
        source_video  = video_file,
        work_dir      = work_dir,
        chunking_mode = chunking_mode if chunking_mode is not None else ChunkingMode.LOSSLESS,
        force         = force,
    )
    registry = _build_registry(config)
    phase    = registry[ChunkingPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def encode_chunks(
    chunks_dir:      Path,
    strategies:      list[str] | None,
    quality_targets: list[str] | None,
    work_dir:        Path,
    max_parallel:    int = 2,
    force:           bool = False,
    dry_run:         bool = False,
) -> "EncodingPhaseResult":
    """Encode all chunks to meet quality targets.

    Constructs an ``EncodingPhase`` with its full dependency chain and delegates
    to ``phase.run()``.

    Args:
        chunks_dir:      Directory containing chunks to encode (used as source_video parent).
        strategies:      List of encoding strategy name strings.
        quality_targets: List of quality target strings (e.g. ``["vmaf-min:95"]``).
        work_dir:        Working directory for encoded chunks and metrics.
        max_parallel:    Maximum concurrent encoding processes (default: 2).
        force:           If True, re-encode even if valid encodings exist (default: False).
        dry_run:         If True, only report status without encoding (default: False).

    Returns:
        ``EncodingPhaseResult`` from the phase object.

    Raises:
        FileNotFoundError: If chunks directory doesn't exist.
        ValueError: If strategies or quality targets are invalid.
    """
    from pyqenc.phases.encoding import EncodingPhase

    if not chunks_dir.exists():
        raise FileNotFoundError(f"Chunks directory not found: {chunks_dir}")

    if not strategies:
        raise ValueError("At least one strategy must be specified")

    if not quality_targets:
        raise ValueError("At least one quality target must be specified")

    if max_parallel < 1:
        raise ValueError(f"Max parallel must be positive, got {max_parallel}")

    parsed_targets = []
    for t in quality_targets:
        try:
            parsed_targets.append(QualityTarget.parse(t))
        except ValueError as e:
            raise ValueError(f"Invalid quality target '{t}': {e}") from e

    parsed_strategies = [Strategy.from_name(s) for s in strategies]

    # Use the first chunk file as a stand-in source_video for config construction
    chunk_files = sorted(chunks_dir.glob("*.mkv"))
    if not chunk_files:
        raise FileNotFoundError(f"No chunks found in {chunks_dir}")

    config   = _minimal_config(
        source_video    = chunk_files[0],
        work_dir        = work_dir,
        quality_targets = parsed_targets,
        strategies      = parsed_strategies,
        max_parallel    = max_parallel,
        force           = force,
    )
    registry = _build_registry(config)
    phase    = registry[EncodingPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def process_audio(
    audio_dir:          Path,
    output_dir:         Path,
    audio_convert:      str | None = None,
    audio_codec:        str | None = None,
    audio_base_bitrate: str | None = None,
    dry_run:            bool = False,
) -> "AudioPhaseResult":
    """Process audio streams with normalization strategies.

    Constructs an ``AudioPhase`` with its full dependency chain and delegates
    to ``phase.run()``.

    Args:
        audio_dir:          Directory containing audio files to process.
        output_dir:         Directory for processed audio output (parent is work_dir).
        audio_convert:      Regex pattern selecting processed audio files to convert to the
                            final delivery format.
        audio_codec:        Override audio codec for all conversion profiles.
        audio_base_bitrate: Base bitrate for 2.0 stereo conversion (e.g. ``'192k'``).
        dry_run:            If True, only report status without processing (default: False).

    Returns:
        ``AudioPhaseResult`` from the phase object.

    Raises:
        FileNotFoundError: If audio directory doesn't exist.
    """
    from pyqenc.phases.audio import AudioPhase

    if not audio_dir.exists():
        raise FileNotFoundError(f"Audio directory not found: {audio_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    audio_files = list(audio_dir.glob("audio_*.mka"))
    if not audio_files:
        raise FileNotFoundError(f"No audio files found in {audio_dir}")

    # Use the first audio file's parent as a stand-in source_video for config
    work_dir = output_dir.parent
    config   = PipelineConfig(
        source_video       = audio_files[0],
        work_dir           = work_dir,
        quality_targets    = [],
        strategies         = [],
        optimize           = False,
        all_strategies     = True,
        max_parallel       = 2,
        cleanup            = CleanupLevel.NONE,
        chunking_mode      = ChunkingMode.LOSSLESS,
        audio_convert      = audio_convert,
        audio_codec        = audio_codec,
        audio_base_bitrate = audio_base_bitrate,
    )

    registry = _build_registry(config)
    phase    = registry[AudioPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def merge_final(
    encoded_dir:        Path,
    audio_dir:          Path,
    output_dir:         Path,
    source_stem:        str,
    source_frame_count: int | None = None,
    verify_frames:      bool = True,
    dry_run:            bool = False,
) -> "MergePhaseResult":
    """Merge encoded chunks and audio into final MKV files.

    Constructs a ``MergePhase`` with its full dependency chain and delegates
    to ``phase.run()``.

    Args:
        encoded_dir:        Directory containing encoded chunks organized by strategy.
        audio_dir:          Directory containing processed audio files.
        output_dir:         Directory for final output MKV files (parent is work_dir).
        source_stem:        Stem of the source video filename (used in output filename).
        source_frame_count: Expected frame count for verification (optional).
        verify_frames:      If True, verify frame count matches source (default: True).
        dry_run:            If True, only report status without merging (default: False).

    Returns:
        ``MergePhaseResult`` from the phase object.

    Raises:
        FileNotFoundError: If encoded or audio directory doesn't exist.
    """
    from pyqenc.phases.merge import MergePhase

    if not encoded_dir.exists():
        raise FileNotFoundError(f"Encoded directory not found: {encoded_dir}")

    if not audio_dir.exists():
        raise FileNotFoundError(f"Audio directory not found: {audio_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Derive work_dir from output_dir (output_dir is work_dir/final)
    work_dir = output_dir.parent

    # Use a placeholder source_video — MergePhase in standalone mode scans encoded/
    # We need any existing file as source_video for config construction
    placeholder = encoded_dir
    for strategy_dir in encoded_dir.iterdir():
        if strategy_dir.is_dir():
            for f in strategy_dir.glob("*.mkv"):
                placeholder = f
                break
        if placeholder != encoded_dir:
            break

    config   = _minimal_config(source_video=placeholder, work_dir=work_dir)
    registry = _build_registry(config)
    phase    = registry[MergePhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


# Export all public functions
__all__ = [
    "run_pipeline",
    "extract_streams",
    "chunk_video",
    "encode_chunks",
    "process_audio",
    "merge_final",
]
