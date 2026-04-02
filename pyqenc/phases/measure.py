"""Standalone measure phase — data models and helpers.

Provides data models for measure results and helper functions for
crop resolution, resolution validation, duration parsing, screenshot
timestamp generation, and screenshot filename formatting.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pyqenc.constants import (
    TEMP_SUFFIX,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import CropParams, VideoMetadata
from pyqenc.quality import ChunkQualityStats, MetricType
from pyqenc.state import JobState
from pyqenc.utils.ffmpeg_runner import run_ffmpeg_async
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


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------


async def _capture_screenshots(
    video_path:      Path,
    timestamps_s:    list[float],
    screenshots_dir: Path,
    crop_params:     CropParams | None,
    fps:             float | None,
    has_timestamps:  bool,
) -> list[Path]:
    """Capture all screenshots for one video in a single ffmpeg pass.

    Uses the ``select`` filter for frame-perfect extraction (no fast-seek).
    All screenshots are captured in one ffmpeg invocation and written as
    ``%04d.png`` into a temporary subdirectory, then renamed to the final
    ``<HH꞉MM꞉SS․mmm>_<stem>.png`` format using the known timestamp list.

    Selection mode:

    - **Primary** (default): timestamp-based ``select='eq(t,T1)+eq(t,T2)+...'``.
      Works for normal and VFR content. Used when ``has_timestamps=True`` or
      when ``fps`` is unknown.
    - **Fallback**: frame-number-based ``select='eq(n,F1)+eq(n,F2)+...'``.
      Used when ``has_timestamps=False`` AND ``fps`` is known.
      Frame numbers derived from ``round(timestamp_s * fps)``.

    Crop is applied in the filter chain. No scaling. Uses ``-vsync 0``.
    The ``.tmp``-then-rename protocol applies to the final named files.

    Args:
        video_path:      Path to the video file to capture from.
        timestamps_s:    List of timestamps in seconds to capture.
        screenshots_dir: Directory where final named screenshots are written.
        crop_params:     Crop to apply in the filter chain, or ``None`` for no crop.
        fps:             Frames per second (used for fallback frame-number mode).
        has_timestamps:  Whether the container has embedded timestamps.

    Returns:
        List of successfully written screenshot paths (may be shorter than
        ``timestamps_s`` if individual frames failed).
    """
    if not timestamps_s:
        return []

    video_stem = video_path.stem

    # Build the select expression
    use_frame_numbers = (not has_timestamps) and (fps is not None)

    if use_frame_numbers:
        assert fps is not None  # narrowing for type checker
        frame_nums = [round(t * fps) for t in timestamps_s]
        select_expr = "+".join(f"eq(n,{n})" for n in frame_nums)
        logger.debug(
            "Screenshot mode: frame-number-based (has_timestamps=False, fps=%.3f)", fps
        )
    else:
        select_expr = "+".join(f"eq(t,{t})" for t in timestamps_s)
        logger.debug("Screenshot mode: timestamp-based")

    # Build filter chain: select [+ crop] + setpts
    filter_parts: list[str] = [f"select='{select_expr}'"]

    if crop_params is not None and not crop_params.is_empty():
        filter_parts.append(crop_params.to_ffmpeg_filter())

    filter_parts.append("setpts=N/FRAME_RATE/TB")
    vf = ",".join(filter_parts)

    # Use a temp subdir for raw %04d.png output
    tmp_dir = screenshots_dir / f".tmp_capture_{video_stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    output_pattern = tmp_dir / "%04d.png"

    cmd: list[str | Path] = [
        "ffmpeg",
        "-i",     video_path,
        "-vf",    vf,
        "-vsync", "0",
        str(output_pattern),
    ]

    logger.debug(
        "Capturing %d screenshots from %s into %s",
        len(timestamps_s), video_path.name, tmp_dir,
    )

    try:
        result = await run_ffmpeg_async(cmd, output_file=None)
    except Exception as exc:
        logger.warning("ffmpeg failed for screenshot capture of %s: %s", video_path.name, exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return []

    if not result.success:
        logger.warning(
            "ffmpeg exited with code %d during screenshot capture of %s",
            result.returncode, video_path.name,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return []

    # Collect raw output files in order: 0001.png, 0002.png, ...
    raw_files = sorted(tmp_dir.glob("*.png"))

    if not raw_files:
        logger.warning("No screenshot files produced for %s", video_path.name)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return []

    if len(raw_files) != len(timestamps_s):
        logger.warning(
            "Expected %d screenshots for %s but got %d; some frames may have been missed",
            len(timestamps_s), video_path.name, len(raw_files),
        )

    # Rename each raw file to its final timestamped name using .tmp-then-rename
    written: list[Path] = []
    for raw_file, timestamp in zip(raw_files, timestamps_s):
        final_name = _screenshot_filename(timestamp, video_stem)
        final_path = screenshots_dir / final_name
        tmp_path   = screenshots_dir / f"{final_name}{TEMP_SUFFIX}"

        try:
            shutil.copy2(raw_file, tmp_path)
            tmp_path.replace(final_path)
            written.append(final_path)
            logger.debug("Screenshot written: %s", final_path.name)
        except Exception as exc:
            logger.warning(
                "Failed to write screenshot %s for %s: %s",
                final_name, video_path.name, exc,
            )
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    # Clean up temp capture dir
    shutil.rmtree(tmp_dir, ignore_errors=True)

    logger.debug(
        "Captured %d/%d screenshots for %s",
        len(written), len(timestamps_s), video_path.name,
    )
    return written


# ---------------------------------------------------------------------------
# Top-level async entry point
# ---------------------------------------------------------------------------


async def run_measure(
    source_video:        Path,
    target_videos:       list[Path],
    work_dir:            Path,
    crop_params:         CropParams | None,
    metrics_sampling:    int,
    width:               int | None,
    screenshot_count:    int | None,
    screenshot_interval: float | None,
) -> MeasureResult:
    """Execute a standalone quality measurement run.

    Args:
        source_video:        Reference video path.
        target_videos:       Encoded videos to evaluate.  Pass an empty list to
                             run in screenshots-only mode (no metrics, graph, or
                             sidecar).
        work_dir:            Working directory; outputs go under
                             ``work_dir/measure/``.
        crop_params:         Explicit crop (or empty ``CropParams`` for no-crop).
                             Pass ``None`` to auto-load from ``job.yaml`` if
                             present.
        metrics_sampling:    Frame subsampling factor (≥1).  Ignored in
                             screenshots-only mode.
        width:               Scale both inputs to this width during metric
                             computation (after cropping).  ``None`` = no
                             scaling.  Does not affect screenshots.  Ignored in
                             screenshots-only mode.
        screenshot_count:    Screenshots per video in count mode (≥1), or cap
                             in interval mode.  ``None`` uses the default.
        screenshot_interval: Interval in seconds between screenshots in interval
                             mode (>0).  ``None`` = count mode.

    Returns:
        ``MeasureResult`` with source screenshots directory and per-target
        results.

    Raises:
        FileNotFoundError: If ``source_video`` or any path in ``target_videos``
                           does not exist.
        ValueError:        If ``metrics_sampling < 1``, ``screenshot_count < 1``,
                           or any resolution mismatch is detected.
    """
    from pyqenc.constants import (
        MEASURE_DIR,
        METRICS_SUBDIR_SUFFIX,
        SCREENSHOTS_SUBDIR_SUFFIX,
    )
    from pyqenc.utils.log_format import fmt_key_value_table

    # ------------------------------------------------------------------
    # 10.1 — Input validation and crop resolution
    # ------------------------------------------------------------------

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    for target in target_videos:
        if not target.exists():
            raise FileNotFoundError(f"Target video not found: {target}")

    if metrics_sampling < 1:
        raise ValueError(f"metrics_sampling must be ≥ 1, got {metrics_sampling}")

    effective_screenshot_count = screenshot_count if screenshot_count is not None else 20
    if effective_screenshot_count < 1:
        raise ValueError(f"screenshot_count must be ≥ 1, got {effective_screenshot_count}")

    resolved_crop = _resolve_crop(crop_params, work_dir, source_video)

    screenshots_only = len(target_videos) == 0

    # ------------------------------------------------------------------
    # 10.2 — Resolution validation and parameter summary logging
    # ------------------------------------------------------------------

    source_meta = VideoMetadata(path=source_video)

    if not screenshots_only:
        target_metas: list[VideoMetadata] = [VideoMetadata(path=t) for t in target_videos]

        for target_video, target_meta in zip(target_videos, target_metas):
            try:
                _check_resolution_match(source_meta, target_meta, resolved_crop, width)
            except ValueError as exc:
                raise ValueError(
                    f"Resolution check failed for {target_video.name}: {exc}"
                ) from exc
    else:
        target_metas = []

    # Build screenshot mode description for the summary table
    if screenshot_interval is not None:
        mode_str = f"every {screenshot_interval}s"
        if screenshot_count is not None:
            mode_str += f" (cap {effective_screenshot_count})"
    else:
        mode_str = f"count {effective_screenshot_count}"

    fmt_key_value_table({
        "source":   str(source_video),
        "targets":  [t.name for t in target_videos] if target_videos else ["(none — screenshots only)"],
        "crop":     str(resolved_crop) if not resolved_crop.is_empty() else "none",
        "width":    str(width) if width else "none",
        "sampling": str(metrics_sampling),
        "mode":     mode_str,
    })

    # ------------------------------------------------------------------
    # 10.3 — Directory creation and duration probing
    # ------------------------------------------------------------------

    measure_dir            = work_dir / MEASURE_DIR
    source_stem            = source_video.stem
    source_screenshots_dir = measure_dir / f"{source_stem}{SCREENSHOTS_SUBDIR_SUFFIX}"

    metrics_dirs:             dict[Path, Path]  = {}
    target_screenshots_dirs:  dict[Path, Path]  = {}
    graph_paths:              dict[Path, Path]  = {}
    sidecar_paths:            dict[Path, Path]  = {}

    for target_video in target_videos:
        stem = target_video.stem
        metrics_dirs[target_video]            = measure_dir / f"{stem}{METRICS_SUBDIR_SUFFIX}"
        target_screenshots_dirs[target_video] = measure_dir / f"{stem}{SCREENSHOTS_SUBDIR_SUFFIX}"
        graph_paths[target_video]             = measure_dir / f"{stem}.png"
        sidecar_paths[target_video]           = measure_dir / f"{stem}.yaml"

    # Create all directories upfront
    source_screenshots_dir.mkdir(parents=True, exist_ok=True)
    for target_video in target_videos:
        metrics_dirs[target_video].mkdir(parents=True, exist_ok=True)
        target_screenshots_dirs[target_video].mkdir(parents=True, exist_ok=True)

    # Probe durations
    source_duration: float | None = source_meta.duration_seconds
    if source_duration is None:
        logger.warning("Duration unavailable for source video: %s", source_video.name)

    target_durations: dict[Path, float | None] = {}
    for target_video, target_meta in zip(target_videos, target_metas):
        dur = target_meta.duration_seconds
        if dur is None:
            logger.warning("Duration unavailable for target video: %s", target_video.name)
        target_durations[target_video] = dur

    # Compute effective duration per target and shared duration
    effective_durations: dict[Path, float | None] = {}
    for target_video in target_videos:
        t_dur = target_durations[target_video]
        if source_duration is not None and t_dur is not None:
            eff = min(source_duration, t_dur)
            if abs(source_duration - t_dur) > 1.0:
                logger.warning(
                    "Duration mismatch for %s: source=%.2fs, target=%.2fs — using effective=%.2fs",
                    target_video.name, source_duration, t_dur, eff,
                )
            effective_durations[target_video] = eff
        else:
            effective_durations[target_video] = None

    # Shared duration = min of all effective durations (for screenshot timestamps)
    # In screenshots-only mode, use source duration directly.
    if screenshots_only:
        shared_duration: float | None = source_duration
    else:
        valid_effs = [d for d in effective_durations.values() if d is not None]
        shared_duration = min(valid_effs) if valid_effs else None

    # ------------------------------------------------------------------
    # 10.4 — Screenshot timestamp computation and source screenshot capture
    # ------------------------------------------------------------------

    if screenshots_only:
        logger.info("Running in screenshots-only mode (no target videos provided)")

    source_fps         = source_meta.fps
    source_has_ts      = source_duration is not None  # proxy: if duration known, timestamps embedded

    shared_timestamps: list[float] = []

    if shared_duration is not None:
        if screenshot_interval is not None:
            raw_timestamps = _screenshot_timestamps_interval(shared_duration, screenshot_interval)
            # Apply cap
            raw_timestamps = raw_timestamps[:effective_screenshot_count]
        else:
            raw_timestamps = _screenshot_timestamps_count(shared_duration, effective_screenshot_count)

        if not raw_timestamps:
            logger.error(
                "No screenshot timestamps fit within shared duration %.2fs — skipping all screenshots",
                shared_duration,
            )
        else:
            if len(raw_timestamps) < effective_screenshot_count and screenshot_interval is None:
                logger.warning(
                    "Only %d screenshot(s) fit within %.2fs (requested %d)",
                    len(raw_timestamps), shared_duration, effective_screenshot_count,
                )
            shared_timestamps = raw_timestamps
    else:
        # Duration unknown
        if screenshot_interval is not None:
            logger.info(
                "Source duration unknown — screenshots will be captured until EOF using interval %.2fs",
                screenshot_interval,
            )
            # Generate a large set; ffmpeg will stop at EOF naturally.
            # Use a generous upper bound of 24 hours.
            _FALLBACK_DURATION = 86400.0
            raw_timestamps = _screenshot_timestamps_interval(_FALLBACK_DURATION, screenshot_interval)
            shared_timestamps = raw_timestamps[:effective_screenshot_count]
        else:
            logger.error(
                "Cannot compute equally-spaced screenshot timestamps: duration unknown for source. "
                "Skipping screenshot capture. Use --every to capture by interval instead."
            )

    # Capture source screenshots
    source_screenshots: list[Path] = []
    if shared_timestamps:
        logger.info("Capturing %d source screenshots from %s", len(shared_timestamps), source_video.name)
        source_screenshots = await _capture_screenshots(
            video_path      = source_video,
            timestamps_s    = shared_timestamps,
            screenshots_dir = source_screenshots_dir,
            crop_params     = resolved_crop if not resolved_crop.is_empty() else None,
            fps             = source_fps,
            has_timestamps  = source_has_ts,
        )
        logger.info("Captured %d/%d source screenshots", len(source_screenshots), len(shared_timestamps))

    # ------------------------------------------------------------------
    # 10.5 — Per-target loop: metrics, sidecar, screenshots
    # ------------------------------------------------------------------

    target_results: list[TargetMeasureResult] = []
    total_target_screenshots = 0

    for target_video, target_meta in zip(target_videos, target_metas):
        logger.info("Processing target: %s", target_video.name)

        eff_dur = effective_durations[target_video]

        # Run metrics
        metrics = _run_metrics(
            source_video     = source_video,
            target_video     = target_video,
            crop_params      = resolved_crop,
            width            = width,
            metrics_dir      = metrics_dirs[target_video],
            graph_path       = graph_paths[target_video],
            subsample_factor = metrics_sampling,
        )

        # Log per-target metric summary
        for metric_type, stats in metrics.items():
            median = stats.get("median")
            if median is not None:
                logger.info(
                    "  %s median: %.3f", metric_type.value, median
                )

        # Write sidecar
        _write_sidecar(
            path                       = sidecar_paths[target_video],
            source_video               = source_video,
            target_video               = target_video,
            subsample_factor           = metrics_sampling,
            crop_params                = resolved_crop,
            metrics                    = metrics,
            source_duration_seconds    = source_duration,
            target_duration_seconds    = target_durations[target_video],
            effective_duration_seconds = eff_dur,
        )

        # Capture target screenshots
        target_screenshots: list[Path] = []
        if shared_timestamps:
            t_fps    = target_meta.fps
            t_has_ts = target_durations[target_video] is not None
            target_screenshots = await _capture_screenshots(
                video_path      = target_video,
                timestamps_s    = shared_timestamps,
                screenshots_dir = target_screenshots_dirs[target_video],
                crop_params     = None,   # no crop on target
                fps             = t_fps,
                has_timestamps  = t_has_ts,
            )
            total_target_screenshots += len(target_screenshots)

        target_results.append(TargetMeasureResult(
            target_video    = target_video,
            graph           = graph_paths[target_video],
            sidecar         = sidecar_paths[target_video],
            screenshots_dir = target_screenshots_dirs[target_video],
            metrics         = metrics,
        ))

    # Final summary
    total_screenshots = len(source_screenshots) + total_target_screenshots
    logger.info(
        "Measure complete: %d target(s) processed, %d total screenshots captured",
        len(target_results), total_screenshots,
    )

    return MeasureResult(
        source_screenshots_dir = source_screenshots_dir,
        targets                = target_results,
    )
