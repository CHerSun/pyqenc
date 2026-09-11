"""Public API for the quality-based encoding pipeline.

This module provides the main entry points for programmatic access to the pipeline.
All functions accept a fully assembled ``AppConfig`` together with the volatile
per-run parameters (``source``, ``work_dir``, ``force``, ``cleanup``,
``no_metrics``, ``dry_run``).  Config assembly — including CLI overrides — is
always the caller's responsibility; this module never loads or patches config
internally.

Each public function drives exactly one *target* phase via the :class:`Runner`
and returns a uniform :class:`RunResult`.  The runner runs only the target
phase; dependency execution happens inside the phases — each phase's
``_ensure_dependencies`` calls ``dep.run()`` on every upstream phase that has
not yet produced a result, so the full dependency chain is executed
automatically (not merely scanned).

Run-level concerns — the metrics collector lifecycle (incremental flush during
the run, final flush on success, flush on failure) and ``finalize`` broadcast —
are owned by the :class:`Runner`.  This module never flushes metrics or
registers collectors itself; it only builds the run-scoped collector and hands
it to the runner.
"""
# CHerSun 2026

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import DEFAULT_SCREENSHOT_COUNT
from pyqenc.metrics import NoOpMetricsCollector, YamlMetricsCollector
from pyqenc.models import CleanupLevel, CropParams
from pyqenc.phase import Phase, _build_registry
from pyqenc.phases.merge import MergePhase
from pyqenc.runner import Runner, RunResult
from pyqenc.utils.long_path import LongPath

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.phases.measure import MeasureResult


# ---------------------------------------------------------------------------
# Internal shared driver
# ---------------------------------------------------------------------------

def _drive(
    config:   "AppConfig",
    source:   Path,
    work_dir: Path,
    target:   type[Phase],
    *,
    force:          bool,
    cleanup:        CleanupLevel,
    no_metrics:     bool,
    dry_run:        bool,
    crop_params:    CropParams | None = None,
    video_required: bool              = True,
) -> RunResult:
    """Build the registry and drive a single target phase via the :class:`Runner`.

    Constructs the run-scoped metrics collector and phase registry, then hands
    both to a :class:`Runner` that runs only ``target``.  Dependency execution
    happens inside the phases; the metrics lifecycle and ``finalize`` broadcast
    are owned by the runner.

    Args:
        config:         Fully assembled application configuration.
        source:         Resolved path to the source video file.
        work_dir:       Working directory for all pipeline artifacts.
        target:         The terminal phase class to run.
        force:          Wipe existing artifacts on source mismatch when ``True``.
        cleanup:        Artifact retention policy for intermediate files.
        no_metrics:     When ``True``, use a no-op collector (no ``metrics.yaml``).
        dry_run:        Report only — no files written.
        crop_params:    Optional manual crop override; ``None`` falls back to
                        cached value in ``probe.yaml``, then auto-detection.
        video_required: When ``True`` (default), build the full video registry;
                        pass ``False`` for the audio-only registry.

    Returns:
        ``RunResult`` summarising the run.

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    if not source.exists():
        raise FileNotFoundError(f"Source video not found: {source}")

    work_dir = LongPath(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    collector = (
        NoOpMetricsCollector()
        if no_metrics
        else YamlMetricsCollector(work_dir=work_dir, force_wipe=force)
    )

    registry = _build_registry(
        config,
        source,
        work_dir,
        force,
        cleanup,
        no_metrics,
        collector,
        crop_params,
        video_required,
    )

    runner = Runner(
        registry         = registry,
        target           = target,
        collector        = collector,
        work_dir         = work_dir,
        cleanup          = cleanup,
        no_metrics       = no_metrics,
        is_terminal_most = target is MergePhase,
    )
    return runner.run(dry_run=dry_run)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(
    config:   "AppConfig",
    source:   Path,
    work_dir: Path,
    *,
    force:       bool             = False,
    cleanup:     CleanupLevel     = CleanupLevel.NONE,
    no_metrics:  bool             = False,
    dry_run:     bool             = True,
    crop_params: CropParams | None = None,
) -> RunResult:
    """Execute the complete end-to-end pipeline (all phases) up to ``MergePhase``.

    Drives ``MergePhase`` as the terminal-most target; every upstream phase is
    executed as a dependency inside the phases.

    Args:
        config:      Fully assembled application configuration.
        source:      Resolved path to the source video file.
        work_dir:    Working directory for all pipeline artifacts.
        force:       Wipe existing artifacts on source mismatch when ``True``.
        cleanup:     Artifact retention policy for intermediate files.
        no_metrics:  When ``True``, skip writing ``metrics.yaml``.
        dry_run:     If ``True``, only report what would be done (default: ``True``).
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``probe.yaml``, then auto-detection.

    Returns:
        ``RunResult`` with the run summary.

    Raises:
        FileNotFoundError: If source video does not exist.
        ValueError:        If configuration is invalid.
        PermissionError:   If working directory is not writable.
    """
    return _drive(
        config,
        source,
        work_dir,
        MergePhase,
        force          = force,
        cleanup        = cleanup,
        no_metrics     = no_metrics,
        dry_run        = dry_run,
        crop_params    = crop_params,
        video_required = True,
    )


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
) -> RunResult:
    """Run the pipeline up to and including the extraction phase.

    Drives ``ExtractionPhase``; all upstream phases are executed (not merely
    scanned) as dependencies inside the phases.

    Args:
        config:      Fully assembled application configuration.
        source:      Resolved path to the source video file.
        work_dir:    Working directory (same as used by ``run_pipeline``).
        force:       Wipe existing artifacts on source mismatch when ``True``.
        cleanup:     Artifact retention policy for intermediate files.
        no_metrics:  When ``True``, skip writing ``metrics.yaml``.
        dry_run:     Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``probe.yaml``, then auto-detection.

    Returns:
        ``RunResult`` with the run summary.

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    from pyqenc.phases.extraction import ExtractionPhase

    return _drive(
        config,
        source,
        work_dir,
        ExtractionPhase,
        force          = force,
        cleanup        = cleanup,
        no_metrics     = no_metrics,
        dry_run        = dry_run,
        crop_params    = crop_params,
        video_required = True,
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
) -> RunResult:
    """Run the pipeline up to and including the chunking phase.

    Drives ``ChunkingPhase``; all upstream phases are executed (not merely
    scanned) as dependencies inside the phases.

    Args:
        config:      Fully assembled application configuration.
        source:      Resolved path to the source video file.
        work_dir:    Working directory (same as used by ``run_pipeline``).
        force:       Wipe existing artifacts on source mismatch when ``True``.
        cleanup:     Artifact retention policy for intermediate files.
        no_metrics:  When ``True``, skip writing ``metrics.yaml``.
        dry_run:     Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``probe.yaml``, then auto-detection.

    Returns:
        ``RunResult`` with the run summary.

    Raises:
        FileNotFoundError: If source video does not exist.
        ValueError:        If scene threshold or min scene length is invalid.
    """
    from pyqenc.phases.chunking import ChunkingPhase

    return _drive(
        config,
        source,
        work_dir,
        ChunkingPhase,
        force          = force,
        cleanup        = cleanup,
        no_metrics     = no_metrics,
        dry_run        = dry_run,
        crop_params    = crop_params,
        video_required = True,
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
) -> RunResult:
    """Run the pipeline up to and including the audio processing phase.

    Drives ``AudioPhase`` against the audio-only registry (``video_required``
    is ``False``, so ``ProbePhase`` and downstream video phases are omitted).
    All upstream phases are executed (not merely scanned) as dependencies
    inside the phases.

    Args:
        config:     Fully assembled application configuration.
        source:     Resolved path to the source video file.
        work_dir:   Working directory (same as used by ``run_pipeline``).
        force:      Wipe existing artifacts on source mismatch when ``True``.
        cleanup:    Artifact retention policy for intermediate files.
        no_metrics: When ``True``, skip writing ``metrics.yaml``.
        dry_run:    Report only — no files written.

    Returns:
        ``RunResult`` with the run summary.

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    from pyqenc.phases.audio import AudioPhase

    return _drive(
        config,
        source,
        work_dir,
        AudioPhase,
        force          = force,
        cleanup        = cleanup,
        no_metrics     = no_metrics,
        dry_run        = dry_run,
        video_required = False,
    )


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
) -> RunResult:
    """Run the pipeline up to and including the encoding phase.

    Drives ``EncodingPhase``; all upstream phases are executed (not merely
    scanned) as dependencies inside the phases.

    Args:
        config:      Fully assembled application configuration.
        source:      Resolved path to the source video file.
        work_dir:    Working directory (same as used by ``run_pipeline``).
        force:       Wipe existing artifacts on source mismatch when ``True``.
        cleanup:     Artifact retention policy for intermediate files.
        no_metrics:  When ``True``, skip writing ``metrics.yaml``.
        dry_run:     Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``probe.yaml``, then auto-detection.

    Returns:
        ``RunResult`` with the run summary.

    Raises:
        FileNotFoundError: If source video does not exist.
        ValueError:        If strategies or quality targets are invalid.
    """
    from pyqenc.phases.encoding import EncodingPhase

    return _drive(
        config,
        source,
        work_dir,
        EncodingPhase,
        force          = force,
        cleanup        = cleanup,
        no_metrics     = no_metrics,
        dry_run        = dry_run,
        crop_params    = crop_params,
        video_required = True,
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
) -> RunResult:
    """Run the pipeline up to and including the merge phase.

    Drives ``MergePhase``; all upstream phases are executed (not merely
    scanned) as dependencies inside the phases.  Callers needing the final
    output paths can read them from ``RunResult.output_files``.

    Args:
        config:      Fully assembled application configuration.
        source:      Resolved path to the source video file.
        work_dir:    Working directory (same as used by ``run_pipeline``).
        force:       Wipe existing artifacts on source mismatch when ``True``.
        cleanup:     Artifact retention policy for intermediate files.
        no_metrics:  When ``True``, skip writing ``metrics.yaml``.
        dry_run:     Report only — no files written.
        crop_params: Optional manual crop override; ``None`` falls back to
                     cached value in ``probe.yaml``, then auto-detection.

    Returns:
        ``RunResult`` with the run summary (final paths in ``output_files``).

    Raises:
        FileNotFoundError: If source video does not exist.
    """
    return _drive(
        config,
        source,
        work_dir,
        MergePhase,
        force          = force,
        cleanup        = cleanup,
        no_metrics     = no_metrics,
        dry_run        = dry_run,
        crop_params    = crop_params,
        video_required = True,
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
