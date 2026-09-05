"""Public API for the quality-based encoding pipeline.

This module provides the main entry points for programmatic access to the pipeline.
All functions accept a fully assembled ``AppConfig`` together with the volatile
per-run parameters (``source``, ``work_dir``, ``force``, ``cleanup``,
``no_metrics``, ``dry_run``).  Config assembly — including CLI overrides — is
always the caller's responsibility; this module never loads or patches config
internally.

Dependency execution: each phase function runs the target phase via
``phase.run()``.  The phase's ``_ensure_dependencies`` method calls
``dep.run()`` on every upstream phase that has not yet produced a result,
so the full dependency chain is executed automatically (not merely scanned).
"""
# CHerSun 2026

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import DEFAULT_SCREENSHOT_COUNT
from pyqenc.metrics import (
    NoOpMetricsCollector,
    YamlMetricsCollector,
    register_active_collector,
)
from pyqenc.models import CleanupLevel, CropParams
from pyqenc.orchestrator import PipelineOrchestrator, PipelineResult
from pyqenc.phase import Phase, PhaseResult, _build_registry
from pyqenc.utils.long_path import LongPath

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.phases.audio import AudioPhaseResult
    from pyqenc.phases.chunking import ChunkingPhaseResult
    from pyqenc.phases.encoding import EncodingPhaseResult
    from pyqenc.phases.extraction import ExtractionPhaseResult
    from pyqenc.phases.measure import MeasureResult
    from pyqenc.phases.merge import MergePhaseResult


# ---------------------------------------------------------------------------
# Internal shared runner
# ---------------------------------------------------------------------------

def _run_phase(
    config:      "AppConfig",
    source:      Path,
    work_dir:    Path,
    phase_class: type[Phase],
    *,
    force:       bool             = False,
    cleanup:     CleanupLevel     = CleanupLevel.NONE,
    no_metrics:  bool             = False,
    dry_run:     bool             = False,
    crop_params: CropParams | None = None,
) -> PhaseResult:
    """Build the registry, run the target phase, and return its result.

    All upstream dependencies are executed (not just scanned) automatically
    by the phase's own ``_ensure_dependencies`` logic.

    Args:
        config:      Fully assembled application configuration.
        source:      Resolved path to the source video file.
        work_dir:    Working directory for all pipeline artifacts.
        phase_class: The phase class to run as the terminal phase.
        force:       Wipe existing artifacts on source mismatch when ``True``.
        cleanup:     Artifact retention policy for intermediate files.
        no_metrics:  When ``True``, skip writing ``metrics.yaml``.
        dry_run:     Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``job.yaml``, then auto-detection.

    Returns:
        ``PhaseResult`` (or typed subclass) produced by the target phase.

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    if not source.exists():
        raise FileNotFoundError(f"Source video not found: {source}")

    work_dir = LongPath(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    collector = NoOpMetricsCollector()
    registry  = _build_registry(
        config      = config,
        source      = source,
        work_dir    = work_dir,
        force       = force,
        cleanup     = cleanup,
        no_metrics  = no_metrics,
        collector   = collector,
        crop_params = crop_params,
    )
    phase = registry[phase_class]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(
    config:     "AppConfig",
    source:     Path,
    work_dir:   Path,
    *,
    force:       bool             = False,
    cleanup:     CleanupLevel     = CleanupLevel.NONE,
    no_metrics:  bool             = False,
    dry_run:     bool             = True,
    crop_params: CropParams | None = None,
) -> PipelineResult:
    """Execute the complete end-to-end pipeline (all phases).

    Args:
        config:     Fully assembled application configuration.
        source:     Resolved path to the source video file.
        work_dir:   Working directory for all pipeline artifacts.
        force:      Wipe existing artifacts on source mismatch when ``True``.
        cleanup:    Artifact retention policy for intermediate files.
        no_metrics: When ``True``, skip writing ``metrics.yaml``.
        dry_run:    If ``True``, only report what would be done (default: ``True``).
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``job.yaml``, then auto-detection.

    Returns:
        ``PipelineResult`` with execution summary.

    Raises:
        FileNotFoundError: If source video does not exist.
        ValueError:        If configuration is invalid.
        PermissionError:   If working directory is not writable.
    """
    if not source.exists():
        raise FileNotFoundError(f"Source video not found: {source}")

    work_dir = LongPath(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if no_metrics:
        collector = NoOpMetricsCollector()
    else:
        collector = YamlMetricsCollector(work_dir=work_dir, force_wipe=force)
        register_active_collector(collector)

    registry = _build_registry(
        config      = config,
        source      = source,
        work_dir    = work_dir,
        force       = force,
        cleanup     = cleanup,
        no_metrics  = no_metrics,
        collector   = collector,
        crop_params = crop_params,
    )

    orchestrator = PipelineOrchestrator(
        registry   = registry,
        collector  = collector,
        no_metrics = no_metrics,
        work_dir   = work_dir,
        cleanup    = cleanup,
    )
    return orchestrator.run(dry_run=dry_run)


def extract_streams(
    config:   "AppConfig",
    source:   Path,
    work_dir: Path,
    *,
    force:       bool             = False,
    cleanup:     CleanupLevel     = CleanupLevel.NONE,
    no_metrics:  bool             = False,
    dry_run:     bool             = False,
    crop_params: CropParams | None = None,
) -> "ExtractionPhaseResult":
    """Run the pipeline up to and including the extraction phase.

    Runs ``JobPhase`` then ``ExtractionPhase``.  All upstream phases are
    executed (not merely scanned), so existing artifacts are reused or
    produced as needed.

    Args:
        config:     Fully assembled application configuration.
        source:     Resolved path to the source video file.
        work_dir:   Working directory (same as used by ``run_pipeline``).
        force:      Wipe existing artifacts on source mismatch when ``True``.
        cleanup:    Artifact retention policy for intermediate files.
        no_metrics: When ``True``, skip writing ``metrics.yaml``.
        dry_run:    Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``job.yaml``, then auto-detection.

    Returns:
        ``ExtractionPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    from pyqenc.phases.extraction import ExtractionPhase

    return _run_phase(  # type: ignore[return-value]
        config,
        source,
        work_dir,
        ExtractionPhase,
        force       = force,
        cleanup     = cleanup,
        no_metrics  = no_metrics,
        dry_run     = dry_run,
        crop_params = crop_params,
    )


def chunk_video(
    config:   "AppConfig",
    source:   Path,
    work_dir: Path,
    *,
    force:       bool             = False,
    cleanup:     CleanupLevel     = CleanupLevel.NONE,
    no_metrics:  bool             = False,
    dry_run:     bool             = False,
    crop_params: CropParams | None = None,
) -> "ChunkingPhaseResult":
    """Run the pipeline up to and including the chunking phase.

    Runs ``JobPhase``, runs ``ExtractionPhase``, then runs ``ChunkingPhase``.
    All upstream phases are executed (not merely scanned).

    Args:
        config:     Fully assembled application configuration.
        source:     Resolved path to the source video file.
        work_dir:   Working directory (same as used by ``run_pipeline``).
        force:      Wipe existing artifacts on source mismatch when ``True``.
        cleanup:    Artifact retention policy for intermediate files.
        no_metrics: When ``True``, skip writing ``metrics.yaml``.
        dry_run:    Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``job.yaml``, then auto-detection.

    Returns:
        ``ChunkingPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video does not exist.
        ValueError:        If scene threshold or min scene length is invalid.
    """
    from pyqenc.phases.chunking import ChunkingPhase

    return _run_phase(  # type: ignore[return-value]
        config,
        source,
        work_dir,
        ChunkingPhase,
        force       = force,
        cleanup     = cleanup,
        no_metrics  = no_metrics,
        dry_run     = dry_run,
        crop_params = crop_params,
    )


def process_audio(
    config:   "AppConfig",
    source:   Path,
    work_dir: Path,
    *,
    force:      bool         = False,
    cleanup:    CleanupLevel = CleanupLevel.NONE,
    no_metrics: bool         = False,
    dry_run:    bool         = False,
) -> "AudioPhaseResult":
    """Run the pipeline up to and including the audio processing phase.

    Runs ``JobPhase``, runs ``ExtractionPhase``, then runs ``AudioPhase``.
    All upstream phases are executed (not merely scanned).  Video extraction
    is skipped because ``ProbePhase`` is not included in the audio-only registry.

    Args:
        config:     Fully assembled application configuration.
        source:     Resolved path to the source video file.
        work_dir:   Working directory (same as used by ``run_pipeline``).
        force:      Wipe existing artifacts on source mismatch when ``True``.
        cleanup:    Artifact retention policy for intermediate files.
        no_metrics: When ``True``, skip writing ``metrics.yaml``.
        dry_run:    Report only — no files written.

    Returns:
        ``AudioPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    if not source.exists():
        raise FileNotFoundError(f"Source video not found: {source}")

    work_dir = LongPath(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    collector = NoOpMetricsCollector()
    registry  = _build_registry(
        config         = config,
        source         = source,
        work_dir       = work_dir,
        force          = force,
        cleanup        = cleanup,
        no_metrics     = no_metrics,
        collector      = collector,
        video_required = False,
    )

    from pyqenc.phases.audio import AudioPhase
    phase = registry[AudioPhase]
    return phase.run(dry_run=dry_run)  # type: ignore[return-value]


def encode_chunks(
    config:   "AppConfig",
    source:   Path,
    work_dir: Path,
    *,
    force:       bool             = False,
    cleanup:     CleanupLevel     = CleanupLevel.NONE,
    no_metrics:  bool             = False,
    dry_run:     bool             = False,
    crop_params: CropParams | None = None,
) -> "EncodingPhaseResult":
    """Run the pipeline up to and including the encoding phase.

    Runs ``JobPhase``, runs ``ExtractionPhase``, runs ``ChunkingPhase``,
    runs ``OptimizationPhase``, then runs ``EncodingPhase``.
    All upstream phases are executed (not merely scanned).

    Args:
        config:     Fully assembled application configuration.
        source:     Resolved path to the source video file.
        work_dir:   Working directory (same as used by ``run_pipeline``).
        force:      Wipe existing artifacts on source mismatch when ``True``.
        cleanup:    Artifact retention policy for intermediate files.
        no_metrics: When ``True``, skip writing ``metrics.yaml``.
        dry_run:    Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``job.yaml``, then auto-detection.

    Returns:
        ``EncodingPhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video does not exist.
        ValueError:        If strategies or quality targets are invalid.
    """
    from pyqenc.phases.encoding import EncodingPhase

    return _run_phase(  # type: ignore[return-value]
        config,
        source,
        work_dir,
        EncodingPhase,
        force       = force,
        cleanup     = cleanup,
        no_metrics  = no_metrics,
        dry_run     = dry_run,
        crop_params = crop_params,
    )


def merge_final(
    config:   "AppConfig",
    source:   Path,
    work_dir: Path,
    *,
    force:       bool             = False,
    cleanup:     CleanupLevel     = CleanupLevel.NONE,
    no_metrics:  bool             = False,
    dry_run:     bool             = False,
    crop_params: CropParams | None = None,
) -> "MergePhaseResult":
    """Run the pipeline up to and including the merge phase.

    Runs all prerequisite phases (Job, Extraction, Chunking, Optimization,
    Encoding, Audio), then runs ``MergePhase``.  All upstream phases are
    executed (not merely scanned).

    Args:
        config:     Fully assembled application configuration.
        source:     Resolved path to the source video file.
        work_dir:   Working directory (same as used by ``run_pipeline``).
        force:      Wipe existing artifacts on source mismatch when ``True``.
        cleanup:    Artifact retention policy for intermediate files.
        no_metrics: When ``True``, skip writing ``metrics.yaml``.
        dry_run:    Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``job.yaml``, then auto-detection.

    Returns:
        ``MergePhaseResult`` from the phase.

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    from pyqenc.phases.merge import MergePhase

    return _run_phase(  # type: ignore[return-value]
        config,
        source,
        work_dir,
        MergePhase,
        force       = force,
        cleanup     = cleanup,
        no_metrics  = no_metrics,
        dry_run     = dry_run,
        crop_params = crop_params,
    )


def measure_quality(
    source_video:             Path,
    work_dir:                 Path,
    target_videos:            list[Path]        | None = None,
    crop_params:              CropParams | None = None,
    metrics_sampling:         int               = 3,
    screenshot_count:         int | None        = DEFAULT_SCREENSHOT_COUNT,
    screenshot_interval:      str | None        = None,
    width:                    int | None        = None,
    screenshot_include_edges: bool              = False,
) -> "MeasureResult":
    """Measure quality metrics between a source and one or more encoded videos.

    Computes VMAF, SSIM, and PSNR metrics for each target, writes a metrics
    sidecar YAML per target, generates a quality graph per target, and captures
    screenshots from the source (once, shared positions) and each target.

    Screenshot positions are computed from the source video's frame count and
    exact rational FPS using integer frame arithmetic — no float drift.

    All outputs are written under ``work_dir/measure/``.

    Args:
        source_video:             Path to the reference (original) video file.
        work_dir:                 Working directory. Outputs go under ``work_dir/measure/``.
        target_videos:            Paths to encoded/distorted videos to evaluate. Pass an
                                  empty list to run in screenshots-only mode.
        crop_params:              Crop parameters applied to the source during metric
                                  computation. Pass ``None`` to auto-load from
                                  ``job.yaml`` in ``work_dir`` if present; pass an
                                  empty ``CropParams`` to explicitly disable cropping.
        metrics_sampling:         Frame subsampling factor (≥1, default 3).
        screenshot_count:         Screenshots to capture from each video (≥1, default 20).
                                  In interval mode, acts as a cap on the total count.
        screenshot_interval:      Interval string between screenshots in interval mode
                                  (e.g. ``"30s"``, ``"5m"``). ``None`` = count mode
                                  (evenly spaced across full duration).
        width:                    Scale both inputs to this width during metric computation
                                  (after cropping). ``None`` = no scaling.
        screenshot_include_edges: When True, include frame 0 and the last frame in
                                  screenshot positions (count mode only).

    Returns:
        ``MeasureResult`` containing source screenshots directory and per-target results.

    Raises:
        FileNotFoundError: If ``source_video`` or any path in ``target_videos`` does not exist.
        ValueError:        If ``metrics_sampling`` < 1 or ``screenshot_count`` < 1.
    """
    import asyncio

    from pyqenc.phases.measure import _parse_duration, run_measure

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    work_dir = LongPath(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    parsed_interval: float | None = None
    if screenshot_interval is not None:
        parsed_interval = _parse_duration(screenshot_interval)

    return asyncio.run(run_measure(
        source_video             = source_video,
        target_videos            = target_videos or [],
        work_dir                 = work_dir,
        crop_params              = crop_params,
        metrics_sampling         = metrics_sampling,
        width                    = width,
        screenshot_count         = screenshot_count,
        screenshot_interval      = parsed_interval,
        screenshot_include_edges = screenshot_include_edges,
    ))


__all__ = [
    "chunk_video",
    "encode_chunks",
    "extract_streams",
    "measure_quality",
    "merge_final",
    "process_audio",
    "run_pipeline",
]
