"""Public API for the quality-based encoding pipeline.

This module provides the main entry points for programmatic access to the pipeline.
All functions accept the original source video and work directory — the same pair
used by the full ``auto`` pipeline — and run the target phase through its complete
dependency chain (including ``JobPhase``).
"""
# CHerSun 2026

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import DEFAULT_METRICS_SAMPLING, DEFAULT_SCREENSHOT_COUNT
from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
    PipelineConfig,
    QualityTarget,
    Strategy,
)
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.orchestrator import PipelineOrchestrator, PipelineResult
from pyqenc.phase import _build_registry

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyqenc.phases.audio import AudioPhaseResult
    from pyqenc.phases.chunking import ChunkingPhaseResult
    from pyqenc.phases.encoding import EncodingPhaseResult
    from pyqenc.phases.extraction import ExtractionPhaseResult
    from pyqenc.phases.measure import MeasureResult
    from pyqenc.phases.merge import MergePhaseResult


def run_pipeline(
    config:  PipelineConfig,
    dry_run: bool = True,
) -> PipelineResult:
    """Execute complete end-to-end pipeline.

    Args:
        config:  Pipeline configuration with all required parameters.
        dry_run: If True, only report what would be done without executing (default: True).

    Returns:
        PipelineResult with execution summary.

    Raises:
        FileNotFoundError: If source video doesn't exist.
        ValueError:        If configuration is invalid.
        PermissionError:   If working directory is not writable.
    """
    if not config.source_video.exists():
        raise FileNotFoundError(f"Source video not found: {config.source_video}")

    config.work_dir.mkdir(parents=True, exist_ok=True)

    orchestrator = PipelineOrchestrator(config)
    return orchestrator.run(dry_run=dry_run)


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
    audio_convert:      str | None = None,
    audio_codec:        str | None = None,
    audio_base_bitrate: str | None = None,
) -> PipelineConfig:
    """Build a ``PipelineConfig`` for standalone phase invocations.

    When ``strategies`` is ``None``, resolves the default strategy list from
    ``ConfigManager`` so that disk space estimation reflects the real pipeline
    scope even for phases that don't explicitly specify strategies (e.g. ``chunk``).
    Standalone phases assume optimization mode (1..N strategies) since the final
    strategy count is not yet known.
    """
    from pyqenc.config import ConfigManager
    resolved_strategies = strategies if strategies is not None else ConfigManager().resolve_strategies(None)
    return PipelineConfig(
        source_video       = source_video,
        work_dir           = work_dir,
        quality_targets    = quality_targets or [],
        strategies         = resolved_strategies,
        optimize           = strategies is None,  # optimization mode when strategies not explicitly given
        max_parallel       = max_parallel,
        include            = include,
        exclude            = exclude,
        cleanup            = CleanupLevel.NONE,
        chunking_mode      = chunking_mode,
        force              = force,
        audio_convert      = audio_convert,
        audio_codec        = audio_codec,
        audio_base_bitrate = audio_base_bitrate,
    )



def extract_streams(
    source_video: Path,
    work_dir:     Path,
    include:      str | None = None,
    exclude:      str | None = None,
    force:        bool = False,
    dry_run:      bool = False,
) -> "ExtractionPhaseResult":
    """Extract video and audio streams from source MKV.

    Runs ``JobPhase`` then ``ExtractionPhase``.  Prerequisite phases are
    scanned (not re-executed) so existing artifacts are reused.

    Args:
        source_video: Path to source MKV file.
        work_dir:     Working directory (same as used by ``auto``).
        include:      Regex to include streams; ``None`` means include all.
        exclude:      Regex to exclude streams; ``None`` means exclude none.
        force:        Wipe existing artifacts on source mismatch when ``True``.
        dry_run:      Report only, no files written.

    Returns:
        ``ExtractionPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video doesn't exist.
    """
    from pyqenc.phases.extraction import ExtractionPhase

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    work_dir.mkdir(parents=True, exist_ok=True)
    config   = _minimal_config(source_video=source_video, work_dir=work_dir, include=include, exclude=exclude, force=force)
    registry = _build_registry(config, NoOpMetricsCollector())
    phase    = registry[ExtractionPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def chunk_video(
    source_video:     Path,
    work_dir:         Path,
    scene_threshold:  float = 0.3,
    min_scene_length: int = 24,
    chunking_mode:    ChunkingMode = ChunkingMode.LOSSLESS,
    force:            bool = False,
    dry_run:          bool = False,
) -> "ChunkingPhaseResult":
    """Split extracted video into scene-based FFV1 chunks.

    Runs ``JobPhase``, scans ``ExtractionPhase``, then runs ``ChunkingPhase``.

    Args:
        source_video:     Path to original source MKV file.
        work_dir:         Working directory (same as used by ``auto``).
        scene_threshold:  Scene detection sensitivity 0.0–1.0 (default: 0.3).
        min_scene_length: Minimum frames per chunk (default: 24).
        chunking_mode:    ``LOSSLESS`` (default) or ``REMUX``.
        force:            Wipe existing artifacts on source mismatch when ``True``.
        dry_run:          Report only, no files written.

    Returns:
        ``ChunkingPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video doesn't exist.
        ValueError:        If scene threshold or min length is invalid.
    """
    from pyqenc.phases.chunking import ChunkingPhase

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    if not 0.0 <= scene_threshold <= 1.0:
        raise ValueError(f"Scene threshold must be between 0.0 and 1.0, got {scene_threshold}")

    if min_scene_length < 1:
        raise ValueError(f"Minimum scene length must be positive, got {min_scene_length}")

    work_dir.mkdir(parents=True, exist_ok=True)
    config   = _minimal_config(source_video=source_video, work_dir=work_dir, chunking_mode=chunking_mode, force=force)
    registry = _build_registry(config, NoOpMetricsCollector())
    phase    = registry[ChunkingPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def encode_chunks(
    source_video:    Path,
    work_dir:        Path,
    strategies:      list[str],
    quality_targets: list[str],
    max_parallel:    int = 2,
    force:           bool = False,
    dry_run:         bool = False,
) -> "EncodingPhaseResult":
    """Encode all chunks to meet quality targets.

    Runs ``JobPhase``, scans ``ExtractionPhase`` and ``ChunkingPhase``,
    then runs ``EncodingPhase``.

    Args:
        source_video:    Path to original source MKV file.
        work_dir:        Working directory (same as used by ``auto``).
        strategies:      List of encoding strategy name strings.
        quality_targets: List of quality target strings (e.g. ``["vmaf-min:95"]``).
        max_parallel:    Maximum concurrent encoding processes (default: 2).
        force:           Wipe existing artifacts on source mismatch when ``True``.
        dry_run:         Report only, no files written.

    Returns:
        ``EncodingPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video doesn't exist.
        ValueError:        If strategies or quality targets are invalid.
    """
    from pyqenc.phases.encoding import EncodingPhase

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    if not strategies:
        raise ValueError("At least one strategy must be specified")

    if not quality_targets:
        raise ValueError("At least one quality target must be specified")

    parsed_targets    = [QualityTarget.parse(t) for t in quality_targets]
    parsed_strategies = [Strategy.from_name(s) for s in strategies]

    work_dir.mkdir(parents=True, exist_ok=True)
    config   = _minimal_config(
        source_video    = source_video,
        work_dir        = work_dir,
        quality_targets = parsed_targets,
        strategies      = parsed_strategies,
        max_parallel    = max_parallel,
        force           = force,
    )
    registry = _build_registry(config, NoOpMetricsCollector())
    phase    = registry[EncodingPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def process_audio(
    source_video:       Path,
    work_dir:           Path,
    audio_convert:      str | None = None,
    audio_codec:        str | None = None,
    audio_base_bitrate: str | None = None,
    dry_run:            bool = False,
) -> "AudioPhaseResult":
    """Process audio streams with normalization strategies.

    Runs ``JobPhase``, scans ``ExtractionPhase``, then runs ``AudioPhase``.

    Args:
        source_video:       Path to original source MKV file.
        work_dir:           Working directory (same as used by ``auto``).
        audio_convert:      Regex selecting processed audio files to convert.
        audio_codec:        Override audio codec for all conversion profiles.
        audio_base_bitrate: Base bitrate for 2.0 stereo conversion (e.g. ``'192k'``).
        dry_run:            Report only, no files written.

    Returns:
        ``AudioPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video doesn't exist.
    """
    from pyqenc.phases.audio import AudioPhase

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    work_dir.mkdir(parents=True, exist_ok=True)
    config   = _minimal_config(
        source_video       = source_video,
        work_dir           = work_dir,
        audio_convert      = audio_convert,
        audio_codec        = audio_codec,
        audio_base_bitrate = audio_base_bitrate,
    )
    registry = _build_registry(config, NoOpMetricsCollector())
    phase    = registry[AudioPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def merge_final(
    source_video: Path,
    work_dir:     Path,
    dry_run:      bool = False,
) -> "MergePhaseResult":
    """Merge encoded chunks and audio into final MKV files.

    Runs ``JobPhase``, scans all prerequisite phases, then runs ``MergePhase``.

    Args:
        source_video: Path to original source MKV file.
        work_dir:     Working directory (same as used by ``auto``).
        dry_run:      Report only, no files written.

    Returns:
        ``MergePhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video doesn't exist.
    """
    from pyqenc.phases.merge import MergePhase

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    work_dir.mkdir(parents=True, exist_ok=True)
    config   = _minimal_config(source_video=source_video, work_dir=work_dir)
    registry = _build_registry(config, NoOpMetricsCollector())
    phase    = registry[MergePhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def measure_quality(
    source_video:        Path,
    target_videos:       list[Path]   = [],
    work_dir:            Path         = Path("."),
    crop_params:         CropParams | None = None,
    metrics_sampling:    int          = DEFAULT_METRICS_SAMPLING,
    screenshot_count:    int | None   = DEFAULT_SCREENSHOT_COUNT,
    screenshot_interval: str | None   = None,
    width:               int | None   = None,
) -> "MeasureResult":
    """Measure quality metrics between a source and one or more encoded videos.

    Computes VMAF, SSIM, and PSNR metrics for each target, writes a metrics
    sidecar YAML per target, generates a quality graph per target, and captures
    screenshots from the source (once, shared timestamps) and each target.

    All outputs are written under ``work_dir/measure/``.

    Args:
        source_video:        Path to the reference (original) video file.
        target_videos:       Paths to encoded/distorted videos to evaluate. Pass an
                             empty list to run in screenshots-only mode.
        work_dir:            Working directory. Outputs go under ``work_dir/measure/``.
        crop_params:         Crop parameters applied to the source during metric
                             computation. Pass ``None`` to auto-load from
                             ``job.yaml`` in ``work_dir`` if present; pass an
                             empty ``CropParams`` to explicitly disable cropping.
        metrics_sampling:    Frame subsampling factor (≥1, default 10).
        screenshot_count:    Screenshots to capture from each video (≥1, default 20).
        screenshot_interval: Interval string between screenshots in interval mode
                             (e.g. ``"30s"``, ``"5m"``). ``None`` = count mode.
        width:               Scale both inputs to this width during metric computation
                             (after cropping). ``None`` = no scaling.

    Returns:
        ``MeasureResult`` containing source screenshots directory and per-target results.

    Raises:
        FileNotFoundError: If ``source_video`` or any path in ``target_videos`` does not exist.
        ValueError:        If ``metrics_sampling`` < 1 or ``screenshot_count`` < 1.
    """
    import asyncio

    from pyqenc.phases.measure import MeasureResult, _parse_duration, run_measure

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    work_dir.mkdir(parents=True, exist_ok=True)

    parsed_interval: float | None = None
    if screenshot_interval is not None:
        parsed_interval = _parse_duration(screenshot_interval)

    return asyncio.run(run_measure(
        source_video        = source_video,
        target_videos       = target_videos,
        work_dir            = work_dir,
        crop_params         = crop_params,
        metrics_sampling    = metrics_sampling,
        width               = width,
        screenshot_count    = screenshot_count,
        screenshot_interval = parsed_interval,
    ))


__all__ = [
    "run_pipeline",
    "extract_streams",
    "chunk_video",
    "encode_chunks",
    "process_audio",
    "merge_final",
    "measure_quality",
]
