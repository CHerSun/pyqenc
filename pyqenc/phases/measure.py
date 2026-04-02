"""Standalone measure phase — data models and helpers.

Provides data models for measure results and helper functions for
crop resolution, resolution validation, duration parsing, screenshot
timestamp generation, and screenshot filename formatting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pyqenc.constants import (
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import CropParams, VideoMetadata
from pyqenc.quality import ChunkQualityStats, MetricType
from pyqenc.state import JobState
from pyqenc.utils.yaml_utils import write_yaml_atomic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TargetMeasureResult:
    """Artifacts produced for a single target video."""

    target_video:    Path
    graph:           Path | None       # <target_stem>.png in measure_dir
    sidecar:         Path | None       # <target_stem>.yaml in measure_dir
    screenshots_dir: Path              # <target_stem>.screenshots/ in measure_dir
    metrics:         ChunkQualityStats # parsed metric statistics


@dataclass
class MeasureResult:
    """All artifacts produced by a measure run."""

    source_screenshots_dir: Path                      # <source_stem>.screenshots/ in measure_dir
    targets:                list[TargetMeasureResult] # one entry per target video


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_HMS_RE = re.compile(
    r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+(?:\.\d+)?)s?)?$"
)
"""Regex for human-friendly duration strings like ``1h30m45s``, ``5m``, ``30s``, ``90.5s``."""


def _parse_duration(value: str) -> float:
    """Parse a duration string to seconds.

    Accepts:
    - Plain int/float: ``"30"``, ``"90.5"``
    - Human-friendly: ``"30s"``, ``"5m"``, ``"1h"``, ``"1h30m"``, ``"1h30m45s"``

    Returns seconds as float. Raises ``ValueError`` on invalid input.
    """
    value = value.strip()
    if not value:
        raise ValueError("Duration string is empty")

    # Try plain numeric first
    try:
        result = float(value)
        if result < 0:
            raise ValueError(f"Duration must be non-negative, got: {value!r}")
        return result
    except ValueError:
        pass  # not a plain number — try HMS pattern

    match = _DURATION_HMS_RE.fullmatch(value)
    if not match or not any(match.group(g) for g in ("h", "m", "s")):
        raise ValueError(f"Invalid duration string: {value!r}")

    h = int(match.group("h") or 0)
    m = int(match.group("m") or 0)
    s = float(match.group("s") or 0.0)
    return h * 3600.0 + m * 60.0 + s


# ---------------------------------------------------------------------------
# Screenshot timestamp helpers
# ---------------------------------------------------------------------------


def _screenshot_timestamps_count(duration: float, count: int) -> list[float]:
    """Return up to ``count`` evenly-spaced timestamps in the interior of ``[0, duration]``.

    ``step = duration / (count + 1)``; timestamps are ``[step, 2*step, ..., count*step]``.
    Filters out any timestamp ``>= duration``. Returns fewer than ``count`` if duration is short.
    """
    step = duration / (count + 1)
    return [t for i in range(1, count + 1) if (t := i * step) < duration]


def _screenshot_timestamps_interval(duration: float, interval_s: float) -> list[float]:
    """Return timestamps at ``[interval_s, 2*interval_s, ...]`` up to ``duration`` (exclusive).

    First screenshot is at ``1×interval`` (skipping frame 0). Returns empty list if
    ``interval_s >= duration``.
    """
    if interval_s >= duration:
        return []
    result: list[float] = []
    t = interval_s
    while t < duration:
        result.append(t)
        t += interval_s
    return result


# ---------------------------------------------------------------------------
# Screenshot filename formatting
# ---------------------------------------------------------------------------


def _screenshot_filename(timestamp_s: float, video_stem: str) -> str:
    """Format a screenshot filename from a timestamp and video stem.

    Format: ``HH꞉MM꞉SS․mmm_stem.png`` using ``TIME_SEPARATOR_SAFE`` (꞉) and
    ``TIME_SEPARATOR_MS`` (․) from ``pyqenc/constants.py``. All components are
    zero-padded.

    Example: ``3723.456`` → ``01꞉02꞉03․456_my_video.png``
    """
    total_ms = int(timestamp_s * 1000)
    ms       = total_ms % 1000
    total_s  = total_ms // 1000
    h, rem   = divmod(total_s, 3600)
    m, s     = divmod(rem, 60)
    sep      = TIME_SEPARATOR_SAFE
    ms_sep   = TIME_SEPARATOR_MS
    prefix   = f"{h:02d}{sep}{m:02d}{sep}{s:02d}{ms_sep}{ms:03d}"
    return f"{prefix}_{video_stem}.png"


# ---------------------------------------------------------------------------
# Resolution validation
# ---------------------------------------------------------------------------


def _resolve_crop(
    crop_params:  CropParams | None,
    work_dir:     Path,
    source_video: Path,
) -> CropParams:
    """Resolve final crop parameters for a measure run.

    Resolution order:

    1. If *crop_params* is a ``CropParams`` instance (including empty/no-op):
       return it directly — the caller made an explicit choice.
    2. If *crop_params* is ``None``: attempt to load ``job.yaml`` from
       *work_dir*.

       - If the file exists, its ``source.path`` matches *source_video*, and
         it contains crop data: use that crop and log at debug level.
       - Otherwise: return an empty ``CropParams`` and log at info level.

    ``job.yaml`` is **never** written or modified by this function.

    Args:
        crop_params:  Explicit crop override, or ``None`` to auto-load.
        work_dir:     Directory that may contain ``job.yaml``.
        source_video: Path to the source video (used for source-match check).

    Returns:
        Resolved ``CropParams`` (never ``None``).
    """
    if crop_params is not None:
        logger.debug("Using explicit crop params: %s", crop_params)
        return crop_params

    # Auto-load from job.yaml
    job_yaml = work_dir / "job.yaml"
    job = JobState.load(job_yaml)

    if job is None:
        logger.info("No job.yaml found in %s — proceeding without crop", work_dir)
        return CropParams()

    if job.source.path.resolve() != source_video.resolve():
        logger.info(
            "job.yaml source (%s) does not match source video (%s) — proceeding without crop",
            job.source.path,
            source_video,
        )
        return CropParams()

    if job.crop is None:
        logger.info(
            "job.yaml found but contains no crop data — proceeding without crop"
        )
        return CropParams()

    logger.debug("Loaded crop from job.yaml: %s", job.crop)
    return job.crop


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def _run_metrics(
    source_video:     Path,
    target_video:     Path,
    crop_params:      CropParams,
    width:            int | None,
    metrics_dir:      Path,
    graph_path:       Path,
    subsample_factor: int,
) -> ChunkQualityStats:
    """Run quality metric computation for one source/target pair.

    Delegates to ``QualityEvaluator.evaluate_chunk`` with ``targets=[]`` so
    no pass/fail evaluation is performed — only metric statistics are returned.

    Args:
        source_video:     Reference (original) video path.
        target_video:     Encoded/distorted video path.
        crop_params:      Crop applied to the source during metric computation.
        width:            Scale both inputs to this width after cropping
                          (``None`` or ``0`` = no scaling).
        metrics_dir:      Directory for raw metric log files (PSNR/SSIM/VMAF).
        graph_path:       Destination path for the quality metrics PNG plot.
        subsample_factor: Frame subsampling factor (≥1).

    Returns:
        ``ChunkQualityStats`` mapping each ``MetricType`` to its key statistics.
    """
    from pyqenc.utils.visualization import QualityEvaluator

    evaluator  = QualityEvaluator(metrics_dir)
    evaluation = evaluator.evaluate_chunk(
        encoded          = target_video,
        reference        = source_video,
        ref_crop         = crop_params,
        targets          = [],
        output_dir       = metrics_dir,
        subsample_factor = subsample_factor,
        show_progress    = True,
        plot_path        = graph_path,
        width            = width or 0,
    )
    return evaluation.metrics



def _effective_resolution(
    meta:        VideoMetadata,
    crop_params: CropParams,
    width:       int | None,
) -> tuple[int, int]:
    """Compute effective (width, height) after crop and optional scale.

    Args:
        meta:        Video metadata with a populated ``resolution`` string.
        crop_params: Crop to apply (may be empty/no-op).
        width:       Target scale width, or ``None`` for no scaling.

    Returns:
        ``(effective_width, effective_height)`` as integers.

    Raises:
        ValueError: If ``meta.resolution`` is unavailable or unparseable.
    """
    if not meta.resolution:
        raise ValueError(f"Resolution unavailable for {meta.path}")

    parts = meta.resolution.split("x")
    if len(parts) != 2:
        raise ValueError(f"Unexpected resolution format: {meta.resolution!r}")

    raw_w = int(parts[0])
    raw_h = int(parts[1])

    cropped_w = raw_w - crop_params.left - crop_params.right
    cropped_h = raw_h - crop_params.top  - crop_params.bottom

    if width is None:
        return cropped_w, cropped_h

    # Scale preserving aspect ratio: scale=width:-1 (height rounded to even)
    scale_h = round(cropped_h * width / cropped_w)
    # ffmpeg scale=-1 rounds to nearest even
    if scale_h % 2 != 0:
        scale_h += 1
    return width, scale_h


def _check_resolution_match(
    source_meta: VideoMetadata,
    target_meta: VideoMetadata,
    crop_params: CropParams,
    width:       int | None,
) -> None:
    """Verify source and target have matching effective resolution after crop and scale.

    Called once per target, upfront before any processing begins.  Effective
    resolution = raw resolution minus crop pixels (source only), then scaled
    to *width* if provided.

    Args:
        source_meta: Metadata for the source (reference) video.
        target_meta: Metadata for the target (encoded) video.
        crop_params: Crop applied to the source.
        width:       Optional scale width applied to both after cropping.

    Raises:
        ValueError: If effective resolutions differ, with an actionable
                    suggestion for ``--crop`` / ``--width`` arguments.
    """
    src_w, src_h = _effective_resolution(source_meta, crop_params, width)
    # Target is never cropped — pass empty CropParams
    tgt_w, tgt_h = _effective_resolution(target_meta, CropParams(), width)

    if src_w == tgt_w and src_h == tgt_h:
        return  # match — nothing to do

    # Build an actionable suggestion.
    # Raw dimensions (before any crop/scale applied here)
    src_parts = source_meta.resolution.split("x") if source_meta.resolution else ["?", "?"]
    tgt_parts = target_meta.resolution.split("x") if target_meta.resolution else ["?", "?"]
    src_raw_w = int(src_parts[0]) if src_parts[0].isdigit() else 0
    src_raw_h = int(src_parts[1]) if src_parts[1].isdigit() else 0
    tgt_raw_w = int(tgt_parts[0]) if tgt_parts[0].isdigit() else 0
    tgt_raw_h = int(tgt_parts[1]) if tgt_parts[1].isdigit() else 0

    suggestion = _build_resolution_suggestion(
        src_raw_w, src_raw_h,
        tgt_raw_w, tgt_raw_h,
        width,
    )

    raise ValueError(
        f"Resolution mismatch: source effective {src_w}x{src_h} "
        f"vs target effective {tgt_w}x{tgt_h}. {suggestion}"
    )


def _build_resolution_suggestion(
    src_raw_w: int,
    src_raw_h: int,
    tgt_raw_w: int,
    tgt_raw_h: int,
    width:     int | None,
) -> str:
    """Build a human-readable ``--crop`` / ``--width`` suggestion string.

    Computes the vertical crop needed to align source height to target height,
    then determines whether ``--width`` is also needed.

    Args:
        src_raw_w: Raw source width (before any crop/scale).
        src_raw_h: Raw source height (before any crop/scale).
        tgt_raw_w: Raw target width.
        tgt_raw_h: Raw target height.
        width:     Currently requested scale width (may be ``None``).

    Returns:
        A suggestion string suitable for inclusion in an error message.
    """
    if src_raw_h < tgt_raw_h:
        return (
            "Target is taller than source — vertical crop cannot fix this. "
            "Re-encode the target at the correct resolution."
        )

    height_diff = src_raw_h - tgt_raw_h
    top    = height_diff // 2
    bottom = height_diff - top

    # After vertical crop: source is src_raw_w × tgt_raw_h
    if src_raw_w == tgt_raw_w:
        # Crop alone resolves the mismatch
        final_w = tgt_raw_w
        final_h = tgt_raw_h
        return (
            f"Did you mean: --crop {top} {bottom}? "
            f"This would bring both videos to {final_w}x{final_h} for metric computation."
        )

    # Need --width to align horizontal dimension too
    suggested_width = tgt_raw_w
    # After crop+scale: source becomes suggested_width × (tgt_raw_h * suggested_width / src_raw_w)
    scaled_h = round(tgt_raw_h * suggested_width / src_raw_w)
    if scaled_h % 2 != 0:
        scaled_h += 1
    return (
        f"Did you mean: --crop {top} {bottom} --width {suggested_width}? "
        f"This would bring both videos to {suggested_width}x{scaled_h} for metric computation."
    )


# ---------------------------------------------------------------------------
# Sidecar YAML
# ---------------------------------------------------------------------------


def _write_sidecar(
    path:                       Path,
    source_video:               Path,
    target_video:               Path,
    subsample_factor:           int,
    crop_params:                CropParams,
    metrics:                    ChunkQualityStats,
    source_duration_seconds:    float | None,
    target_duration_seconds:    float | None,
    effective_duration_seconds: float | None,
) -> None:
    """Write a metrics sidecar YAML file using the ``.tmp``-then-rename protocol.

    Records source/target paths, durations, subsample factor, crop parameters,
    and all computed metric statistics.  On write failure logs a warning and
    returns without raising so that the graph and screenshots are not lost.

    Args:
        path:                       Destination path for the sidecar YAML.
        source_video:               Reference video path.
        target_video:               Encoded/distorted video path.
        subsample_factor:           Frame subsampling factor used during computation.
        crop_params:                Resolved crop parameters applied to the source.
        metrics:                    Computed quality statistics per metric type.
        source_duration_seconds:    Source video duration in seconds, or ``None``.
        target_duration_seconds:    Target video duration in seconds, or ``None``.
        effective_duration_seconds: ``min(source, target)`` duration, or ``None``.
    """
    crop_dict: dict[str, int] | None = {
        "top":    crop_params.top,
        "bottom": crop_params.bottom,
        "left":   crop_params.left,
        "right":  crop_params.right,
    }

    # Serialise metrics: {metric_name: {stat_name: value}}
    metrics_dict: dict[str, dict[str, float]] = {
        metric_type.value: dict(stats)
        for metric_type, stats in metrics.items()
    }

    data: dict = {
        "source_video":               str(source_video),
        "target_video":               str(target_video),
        "source_duration_seconds":    source_duration_seconds,
        "target_duration_seconds":    target_duration_seconds,
        "effective_duration_seconds": effective_duration_seconds,
        "subsample_factor":           subsample_factor,
        "crop_params":                crop_dict,
        "metrics":                    metrics_dict,
    }

    try:
        write_yaml_atomic(path, data)
        logger.debug("Wrote metrics sidecar: %s", path)
    except Exception as exc:
        logger.warning("Failed to write metrics sidecar %s: %s", path, exc)
