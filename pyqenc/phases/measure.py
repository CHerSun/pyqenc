"""Standalone measure phase — data models and helpers.

Provides data models for measure results and helper functions for
crop resolution, resolution validation, duration parsing, screenshot
timestamp generation, and screenshot filename formatting.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pyqenc.constants import (
    TEMP_SUFFIX,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import CropParams, VideoMetadata
from pyqenc.quality import ChunkQualityStats, MetricType
from pyqenc.state import JobState, MeasureSidecar
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


@dataclass(frozen=True)
class ScreenshotPositions:
    """Immutable set of screenshot positions computed from the source video.

    frame_nums are 0-based frame indices from the source. fps and step are
    shared across all positions in a run. Timestamp derivation uses exact
    rational arithmetic via fractions.Fraction to avoid float drift.
    """
    frame_nums: list[int]   # canonical 0-based frame indices
    fps:        Fraction    # from source VideoMetadata.fps_fraction
    step:       int         # frame step used for A4 fallback

    def seek_ts(self, frame_num: int) -> str:
        """Seek timestamp for Strategy C: (frame_num - 0.25) / fps, 9 decimal places.

        Guarantees the seek lands before the target frame so ffmpeg decodes
        forward to exactly frame_num.
        """
        ts = Fraction(frame_num * 4 - 1, 4) / self.fps
        return f"{float(ts):.9f}"

    def filename_ts(self, frame_num: int) -> str:
        """Filename timestamp prefix: frame_num / fps as HH꞉MM꞉SS․mmm."""
        ts_frac  = Fraction(frame_num, 1) / self.fps
        total_ms = int(ts_frac * 1000)
        ms       = total_ms % 1000
        total_s  = total_ms // 1000
        h, rem   = divmod(total_s, 3600)
        m, s     = divmod(rem, 60)
        return (
            f"{h:02d}{TIME_SEPARATOR_SAFE}{m:02d}{TIME_SEPARATOR_SAFE}"
            f"{s:02d}{TIME_SEPARATOR_MS}{ms:03d}"
        )


def compute_screenshot_positions(
    total_frames:  int,
    fps:           Fraction,
    count:         int,
    include_edges: bool = False,
) -> ScreenshotPositions:
    """Compute evenly-spaced screenshot positions from source video metadata.

    Positions are computed using exact integer frame arithmetic — no float
    drift. The same (total_frames, fps, count) triple always produces
    identical positions across reruns (deterministic).

    Interior positions: step = total_frames // (count + 1), frames at
    [step, 2*step, ..., count*step].

    When include_edges=True, frame 0 and total_frames-1 are prepended/appended.

    Args:
        total_frames:  Total frame count of the source video.
        fps:           Exact rational FPS from VideoMetadata.fps_fraction.
        count:         Number of interior screenshot positions.
        include_edges: When True, also include frame 0 and the last frame.

    Returns:
        ScreenshotPositions with frame_nums, fps, and step.
    """
    step       = total_frames // (count + 1)
    frame_nums = [i * step for i in range(1, count + 1)]
    if include_edges:
        frame_nums = [0] + frame_nums + [total_frames - 1]
    return ScreenshotPositions(frame_nums=frame_nums, fps=fps, step=step)


def compute_screenshot_positions_interval(
    fps:           Fraction,
    interval_s:    float,
    total_frames:  int | None = None,
    cap:           int | None = None,
) -> ScreenshotPositions:
    """Compute screenshot positions at a fixed time interval.

    Positions start at ``1 × interval`` (frame 0 is skipped), then
    ``2 × interval``, etc.  Each interval is converted to the nearest
    frame number using exact rational arithmetic: ``frame = int(i * interval_s * fps)``.

    When ``total_frames`` is provided, positions beyond the last frame are
    dropped.  When ``cap`` is provided, the list is truncated to at most
    ``cap`` entries.  When ``total_frames`` is ``None``, a generous upper
    bound of 24 hours is used so ffmpeg naturally stops at EOF.

    Args:
        fps:          Exact rational FPS from VideoMetadata.fps_fraction.
        interval_s:   Interval between screenshots in seconds (> 0).
        total_frames: Total frame count of the source video, or None if unknown.
        cap:          Maximum number of positions to return, or None for no cap.

    Returns:
        ScreenshotPositions with frame_nums, fps, and step=0 (no mod-select step).
    """
    _FALLBACK_HOURS   = 24
    _FALLBACK_SECONDS = _FALLBACK_HOURS * 3600
    max_frames = total_frames if total_frames is not None else int(_FALLBACK_SECONDS * float(fps))

    frame_nums: list[int] = []
    i = 1
    while True:
        frame = int(Fraction(i) * Fraction(interval_s) * fps)
        if frame >= max_frames:
            break
        frame_nums.append(frame)
        if cap is not None and len(frame_nums) >= cap:
            break
        i += 1

    # step=0 signals that A4 mod-select fallback is not applicable for interval mode
    return ScreenshotPositions(frame_nums=frame_nums, fps=fps, step=0)


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


async def _run_metrics(
    source_video:     Path,
    target_video:     Path,
    crop_params:      CropParams,
    width:            int | None,
    metrics_dir:      Path,
    graph_path:       Path,
    subsample_factor: int,
    bar_title:        str,
) -> ChunkQualityStats:
    """Run quality metric computation for one source/target pair.

    Delegates to ``QualityEvaluator.evaluate_chunk_async`` with ``targets=[]`` so
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
    evaluation = await evaluator.evaluate_chunk_async(
        encoded          = target_video,
        reference        = source_video,
        ref_crop         = crop_params,
        targets          = [],
        output_dir       = metrics_dir,
        subsample_factor = subsample_factor,
        show_progress    = True,
        plot_path        = graph_path,
        width            = width or 0,
        bar_title        = bar_title,
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

    Records source/target paths, durations, sampling factor, crop parameters,
    and all computed metric statistics in the flat ``{metric_stat: value}``
    format consistent with ``MetricsSidecar`` (e.g. ``vmaf_min``, ``ssim_median``).
    On write failure logs a warning and returns without raising so that the
    graph and screenshots are not lost.

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

    # Flatten ChunkQualityStats → {metric_stat: value}, e.g. vmaf_min, ssim_median
    flat_metrics: dict[str, float] = {
        f"{metric_type.value}_{stat}": value
        for metric_type, stats in metrics.items()
        for stat, value in stats.items()
    }

    sidecar = MeasureSidecar(
        source_video               = source_video,
        target_video               = target_video,
        source_duration_seconds    = source_duration_seconds,
        target_duration_seconds    = target_duration_seconds,
        effective_duration_seconds = effective_duration_seconds,
        sampling                   = subsample_factor,
        crop_params                = crop_dict,
        metrics                    = flat_metrics,
    )

    try:
        write_yaml_atomic(path, sidecar.to_yaml_dict())
        logger.debug("Wrote metrics sidecar: %s", path)
    except Exception as exc:
        logger.warning("Failed to write metrics sidecar %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------


async def _capture_single_frame(
    video_path:  Path,
    seek_ts:     str,
    output_path: Path,
    crop_params: CropParams | None,
) -> str | None:
    """Capture a single frame via fast-seek.

    Returns:
        None on success, or a short human-readable failure reason string.
    """
    cmd: list[str | Path] = ["ffmpeg", "-y", "-ss", seek_ts, "-i", video_path]
    if crop_params is not None and not crop_params.is_empty():
        cmd += ["-vf", crop_params.to_ffmpeg_filter()]
    cmd += ["-frames:v", "1", "-f", "image2", "-c:v", "png", output_path]
    try:
        result = await run_ffmpeg_async(cmd, output_file=None)
        if not result.success:
            reason = f"ffmpeg exit {result.returncode}"
            logger.debug("Strategy C frame failed (%s) seek_ts=%s output=%s", reason, seek_ts, output_path.name)
            return reason
        if not output_path.exists():
            reason = "no output file produced"
            logger.debug("Strategy C frame failed (%s) seek_ts=%s output=%s", reason, seek_ts, output_path.name)
            return reason
        return None
    except Exception as exc:
        reason = str(exc)
        logger.debug("Strategy C frame raised exception seek_ts=%s output=%s: %s", seek_ts, output_path.name, exc)
        return reason


async def _capture_single_pass(
    video_path:  Path,
    select_expr: str,
    tmp_dir:     Path,
    crop_params: CropParams | None,
) -> list[Path]:
    """Run a single-pass select-filter capture. Returns sorted list of output PNGs."""
    output_pattern = tmp_dir / "%04d.png"
    vf_parts = [f"select='{select_expr}'"]
    if crop_params is not None and not crop_params.is_empty():
        vf_parts.append(crop_params.to_ffmpeg_filter())
    vf_parts.append("setpts=N/FRAME_RATE/TB")
    cmd: list[str | Path] = [
        "ffmpeg", "-y", "-i", video_path,
        "-vf", ",".join(vf_parts),
        "-vsync", "0",
        str(output_pattern),
    ]
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        result = await run_ffmpeg_async(cmd, output_file=None)
        if not result.success:
            logger.debug("Single-pass capture failed (ffmpeg non-zero) tmp_dir=%s", tmp_dir)
            return []
    except Exception as exc:
        logger.debug("Single-pass capture raised exception tmp_dir=%s: %s", tmp_dir, exc)
        return []
    return sorted(tmp_dir.glob("*.png"))


def _rename_raw_screenshots(
    raw_files:   list[Path],
    frame_nums:  list[int],
    positions:   ScreenshotPositions,
    video_path:  Path,
    output_dir:  Path,
) -> list[Path]:
    """Rename raw %04d.png files to final timestamped names using .tmp-then-rename.

    Zips raw_files with frame_nums (up to min length). Returns list of
    successfully written paths.
    """
    written: list[Path] = []
    video_stem = video_path.stem
    for raw_file, frame_num in zip(raw_files, frame_nums):
        filename_ts = positions.filename_ts(frame_num)
        final_name  = f"{filename_ts}_{video_stem}.png"
        final_path  = output_dir / final_name
        tmp_path    = output_dir / f"{final_name}{TEMP_SUFFIX}"
        try:
            shutil.copy2(raw_file, tmp_path)
            tmp_path.replace(final_path)
            written.append(final_path)
            logger.debug("Screenshot written: %s", final_path.name)
        except Exception as exc:
            logger.warning("Failed to write screenshot %s: %s", final_name, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
    return written


async def make_screenshots(
    video_path:      Path,
    positions:       ScreenshotPositions,
    screenshots_dir: Path,
    crop_params:     CropParams | None = None,
) -> list[Path]:
    """Capture screenshots for one video at the given positions.

    Attempts Strategy C first (N sequential fast-seek ffmpeg calls), falling
    back to Strategy A2 (single-pass frame-number select) and then Strategy A4
    (single-pass mod select) if the primary strategy yields zero output.

    Partial results (fewer screenshots than positions, e.g. target shorter than
    source) are acceptable and do NOT trigger fallback — only total failure
    (zero output) triggers the fallback chain.

    Args:
        video_path:      Path to the video file to capture from.
        positions:       Pre-computed screenshot positions (frame numbers + fps).
        screenshots_dir: Directory where final named screenshots are written.
        crop_params:     Crop to apply in the filter chain, or None for no crop.

    Returns:
        List of successfully written screenshot paths (may be shorter than
        len(positions.frame_nums) if the video is shorter than the source).
    """
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    video_stem = video_path.stem

    # ------------------------------------------------------------------
    # Strategy C: N sequential fast-seek calls
    # ------------------------------------------------------------------
    written: list[Path] = []
    c_failure_reasons: set[str] = set()
    for frame_num in positions.frame_nums:
        seek_ts     = positions.seek_ts(frame_num)
        filename_ts = positions.filename_ts(frame_num)
        final_name  = f"{filename_ts}_{video_stem}.png"
        final_path  = screenshots_dir / final_name
        tmp_path    = screenshots_dir / f"{final_name}{TEMP_SUFFIX}"

        logger.debug("Strategy C: frame %d seek_ts=%s → %s", frame_num, seek_ts, final_name)
        failure = await _capture_single_frame(video_path, seek_ts, tmp_path, crop_params)
        if failure is None:
            try:
                tmp_path.replace(final_path)
                written.append(final_path)
            except Exception as exc:
                logger.warning("Failed to rename screenshot %s: %s", final_name, exc)
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            c_failure_reasons.add(failure)

    if written:
        return written

    # ------------------------------------------------------------------
    # Strategy A2 fallback: single-pass frame-number select
    # ------------------------------------------------------------------
    reasons_str = "; ".join(sorted(c_failure_reasons)) if c_failure_reasons else "unknown"
    logger.warning(
        "Screenshot strategy C failed for %s (%s) — falling back to A2 (single-pass frame-number select)",
        video_stem, reasons_str,
    )
    select_expr_a2 = "+".join(f"eq(n,{n})" for n in positions.frame_nums)
    stem_hash      = hashlib.sha1(video_stem.encode()).hexdigest()[:12]
    tmp_dir_a2     = screenshots_dir / f".tmp_a2_{stem_hash}"
    raw_a2         = await _capture_single_pass(video_path, select_expr_a2, tmp_dir_a2, crop_params)

    if raw_a2:
        written = _rename_raw_screenshots(raw_a2, positions.frame_nums, positions, video_path, screenshots_dir)
        shutil.rmtree(tmp_dir_a2, ignore_errors=True)
        if written:
            return written

    shutil.rmtree(tmp_dir_a2, ignore_errors=True)

    # ------------------------------------------------------------------
    # Strategy A4 fallback: single-pass mod select
    # ------------------------------------------------------------------
    logger.warning(
        "Screenshot strategy A2 failed for %s — falling back to A4 (single-pass mod select)",
        video_stem,
    )
    select_expr_a4 = f"not(mod(n,{positions.step}))*gt(n,0)"
    tmp_dir_a4     = screenshots_dir / f".tmp_a4_{stem_hash}"
    raw_a4         = await _capture_single_pass(video_path, select_expr_a4, tmp_dir_a4, crop_params)

    if raw_a4:
        written = _rename_raw_screenshots(raw_a4, positions.frame_nums, positions, video_path, screenshots_dir)
        shutil.rmtree(tmp_dir_a4, ignore_errors=True)
        if written:
            return written

    shutil.rmtree(tmp_dir_a4, ignore_errors=True)

    # ------------------------------------------------------------------
    # All strategies failed
    # ------------------------------------------------------------------
    logger.error("All screenshot strategies failed for %s — no screenshots captured", video_stem)
    return []


# ---------------------------------------------------------------------------
# Measure summary table
# ---------------------------------------------------------------------------


def _log_measure_summary(targets: list[TargetMeasureResult]) -> None:
    """Emit a summary table at INFO level after all metric computations complete.

    One row per target showing: stem (truncated to 30 chars), file size in MB,
    and per-metric median for every MetricType that appears in any target's
    metrics.  Missing metrics display as ``N/A``.

    Args:
        targets: List of completed target measure results.
    """
    from pyqenc.utils.log_format import _fmt_size_mb, fmt_metric_value

    if not targets:
        return

    # Collect all metric types that appear across any target
    all_metric_types: list[MetricType] = []
    seen: set[MetricType] = set()
    for t in targets:
        for mt in t.metrics:
            if mt not in seen:
                all_metric_types.append(mt)
                seen.add(mt)

    # Column widths
    STEM_WIDTH   = 30
    SIZE_WIDTH   = 9
    METRIC_WIDTH = 9

    # Header
    metric_headers = "   ".join(
        f"{mt.value.upper()[:METRIC_WIDTH - 4]} med".rjust(METRIC_WIDTH)
        for mt in all_metric_types
    )
    header = f"{'Target':<{STEM_WIDTH}}   {'Size (MB)':>{SIZE_WIDTH}}   {metric_headers}"
    sep    = "─" * STEM_WIDTH + "   " + "─" * SIZE_WIDTH + "   " + ("─" * METRIC_WIDTH + "   ") * len(all_metric_types)

    logger.info(header)
    logger.info(sep.rstrip())

    for t in targets:
        stem      = t.target_video.stem[:STEM_WIDTH]
        size_str  = _fmt_size_mb(t.target_video.stat().st_size) if t.target_video.exists() else "N/A"
        row_parts = [f"{stem:<{STEM_WIDTH}}", f"{size_str:>{SIZE_WIDTH}}"]
        for mt in all_metric_types:
            stats  = t.metrics.get(mt, {})
            median = stats.get("median")
            cell   = fmt_metric_value(median) if median is not None else "N/A"
            row_parts.append(f"{cell:>{METRIC_WIDTH}}")
        logger.info("   ".join(row_parts))


# ---------------------------------------------------------------------------
# Top-level async entry point
# ---------------------------------------------------------------------------


async def run_measure(
    source_video:             Path,
    target_videos:            list[Path],
    work_dir:                 Path,
    crop_params:              CropParams | None,
    metrics_sampling:         int,
    width:                    int | None,
    screenshot_count:         int | None,
    screenshot_interval:      float | None      = None,
    screenshot_include_edges: bool              = False,
) -> MeasureResult:
    """Execute a standalone quality measurement run.

    Execution order: (1) compute screenshot positions from source, (2) capture
    source screenshots, (3) capture all target screenshots, (4) run metrics for
    each target, (5) emit summary table.

    Args:
        source_video:             Reference video path.
        target_videos:            Encoded videos to evaluate.  Pass an empty list to
                                  run in screenshots-only mode (no metrics, graph, or
                                  sidecar).
        work_dir:                 Working directory; outputs go under
                                  ``work_dir/measure/``.
        crop_params:              Explicit crop (or empty ``CropParams`` for no-crop).
                                  Pass ``None`` to auto-load from ``job.yaml`` if
                                  present.
        metrics_sampling:         Frame subsampling factor (≥1).  Ignored in
                                  screenshots-only mode.
        width:                    Scale both inputs to this width during metric
                                  computation (after cropping).  ``None`` = no
                                  scaling.  Does not affect screenshots.  Ignored in
                                  screenshots-only mode.
        screenshot_count:         Screenshots per video in count mode (≥1), or cap
                                  in interval mode.  ``None`` uses the default.
        screenshot_interval:      Interval in seconds between screenshots (>0).
                                  When provided, positions are spaced by this interval
                                  rather than evenly across the full duration.
                                  ``None`` = count mode (evenly spaced).
        screenshot_include_edges: When True, include frame 0 and the last frame in
                                  screenshot positions in addition to interior frames.
                                  Only applies in count mode (ignored in interval mode).

    Returns:
        ``MeasureResult`` with source screenshots directory and per-target
        results.

    Raises:
        FileNotFoundError: If ``source_video`` or any path in ``target_videos``
                           does not exist.
        ValueError:        If ``metrics_sampling < 1``, ``screenshot_count < 1``,
                           or any resolution mismatch is detected.
    """
    from pyqenc.constants import MEASURE_DIR
    from pyqenc.utils.log_format import fmt_key_value_table

    # ------------------------------------------------------------------
    # Input validation and crop resolution
    # ------------------------------------------------------------------

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    for target in target_videos:
        if not target.exists():
            raise FileNotFoundError(f"Target video not found: {target}")

    if metrics_sampling < 1:
        raise ValueError(f"metrics_sampling must be ≥ 1, got {metrics_sampling}")

    from pyqenc.constants import DEFAULT_SCREENSHOT_COUNT
    effective_screenshot_count = screenshot_count if screenshot_count is not None else DEFAULT_SCREENSHOT_COUNT
    if effective_screenshot_count < 1:
        raise ValueError(f"screenshot_count must be ≥ 1, got {effective_screenshot_count}")

    resolved_crop    = _resolve_crop(crop_params, work_dir, source_video)
    screenshots_only = len(target_videos) == 0

    # ------------------------------------------------------------------
    # Resolution validation and parameter summary logging
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

    fmt_key_value_table({
        "source":      str(source_video),
        "targets":     [f"{i+1:>2} - {t.name}" for i, t in enumerate(target_videos)] if target_videos else ["(none — screenshots only)"],
        "crop":        str(resolved_crop) if not resolved_crop.is_empty() else "none",
        "width":       str(width) if width else "none",
        "sampling":    str(metrics_sampling),
        "screenshots": f"every {screenshot_interval}s (cap {effective_screenshot_count})" if screenshot_interval else str(effective_screenshot_count),
    })

    # ------------------------------------------------------------------
    # Directory creation
    # ------------------------------------------------------------------

    measure_dir            = work_dir / MEASURE_DIR
    source_screenshots_dir = measure_dir / source_video.stem

    target_screenshots_dirs: dict[Path, Path] = {}
    graph_paths:             dict[Path, Path] = {}
    sidecar_paths:           dict[Path, Path] = {}

    for target_video in target_videos:
        stem = target_video.stem
        target_screenshots_dirs[target_video] = measure_dir / stem
        graph_paths[target_video]             = measure_dir / f"{stem}.png"
        sidecar_paths[target_video]           = measure_dir / f"{stem}.yaml"

    measure_dir.mkdir(parents=True, exist_ok=True)

    # Startup cleanup: remove any stale .tmp metric files from interrupted runs
    for tmp_file in measure_dir.glob("*.tmp"):
        try:
            tmp_file.unlink()
            logger.debug("Cleaned up stale tmp file: %s", tmp_file.name)
        except Exception as exc:
            logger.warning("Could not delete stale tmp file %s: %s", tmp_file.name, exc)

    # ------------------------------------------------------------------
    # Duration probing (needed for sidecar and duration-mismatch warnings)
    # ------------------------------------------------------------------

    source_duration: float | None = source_meta.duration_seconds
    if source_duration is None:
        logger.warning("Duration unavailable for source video: %s", source_video.name)

    target_durations: dict[Path, float | None] = {}
    for target_video, target_meta in zip(target_videos, target_metas):
        dur = target_meta.duration_seconds
        if dur is None:
            logger.warning("Duration unavailable for target video: %s", target_video.name)
        target_durations[target_video] = dur

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

    # ------------------------------------------------------------------
    # (1) Compute screenshot positions from source
    # ------------------------------------------------------------------

    if screenshots_only:
        logger.info("Running in screenshots-only mode (no target videos provided)")

    positions: ScreenshotPositions | None = None

    # Probe fps first (fast ffprobe call, already triggered by resolution check above)
    source_fps_frac = source_meta.fps_fraction

    # frame_count requires the slow null-encode probe — call async variant to avoid
    # the sync-in-async deadlock that would occur via the lazy property path.
    source_frame_count = source_meta._frame_count  # use cached value if already populated
    if source_frame_count is None:
        await source_meta._probe_frame_count_async()
        source_frame_count = source_meta._frame_count

    if source_frame_count is not None and source_fps_frac is not None:
        if screenshot_interval is not None:
            positions = compute_screenshot_positions_interval(
                fps          = source_fps_frac,
                interval_s   = screenshot_interval,
                total_frames = source_frame_count,
                cap          = effective_screenshot_count,
            )
            logger.debug(
                "Screenshot positions: %d frames, interval=%.3fs, fps=%s",
                len(positions.frame_nums), screenshot_interval, positions.fps,
            )
        else:
            positions = compute_screenshot_positions(
                total_frames  = source_frame_count,
                fps           = source_fps_frac,
                count         = effective_screenshot_count,
                include_edges = screenshot_include_edges,
            )
            logger.debug(
                "Screenshot positions: %d frames, step=%d, fps=%s",
                len(positions.frame_nums), positions.step, positions.fps,
            )
    elif source_fps_frac is not None and screenshot_interval is not None:
        # frame_count unknown but fps known — use interval mode with no upper bound
        positions = compute_screenshot_positions_interval(
            fps          = source_fps_frac,
            interval_s   = screenshot_interval,
            total_frames = None,
            cap          = effective_screenshot_count,
        )
        logger.debug(
            "Screenshot positions (no frame count): %d frames, interval=%.3fs",
            len(positions.frame_nums), screenshot_interval,
        )
    else:
        logger.error(
            "Cannot compute screenshot positions: %s unavailable for source %s — skipping screenshots",
            "frame count" if source_frame_count is None else "fps",
            source_video.name,
        )

    # ------------------------------------------------------------------
    # (2) Capture source screenshots
    # ------------------------------------------------------------------

    source_screenshots: list[Path] = []
    if positions is not None:
        logger.info("Taking %d screenshots of %s...", len(positions.frame_nums), source_video.stem)
        try:
            source_screenshots = await make_screenshots(
                video_path      = source_video,
                positions       = positions,
                screenshots_dir = source_screenshots_dir,
                crop_params     = resolved_crop if not resolved_crop.is_empty() else None,
            )
        except Exception as exc:
            logger.error("Screenshots failed for %s — skipping: %s", source_video.stem, exc)

    # ------------------------------------------------------------------
    # (3) Capture all target screenshots
    # ------------------------------------------------------------------

    target_screenshots_map: dict[Path, list[Path]] = {}
    for target_video in target_videos:
        if positions is not None:
            logger.info("Taking %d screenshots of %s...", len(positions.frame_nums), target_video.stem)
            try:
                shots = await make_screenshots(
                    video_path      = target_video,
                    positions       = positions,
                    screenshots_dir = target_screenshots_dirs[target_video],
                    crop_params     = None,
                )
            except Exception as exc:
                logger.error("Screenshots failed for %s — skipping: %s", target_video.stem, exc)
                shots = []
            target_screenshots_map[target_video] = shots
        else:
            target_screenshots_map[target_video] = []

    # Summary log after all screenshot captures
    if positions is not None:
        expected_per_video = len(positions.frame_nums)
        all_videos         = ([source_video] if not screenshots_only else [source_video]) + list(target_videos)
        total_expected     = expected_per_video * len(all_videos)
        total_taken        = len(source_screenshots) + sum(len(s) for s in target_screenshots_map.values())
        symbol             = "✅" if total_taken == total_expected else "⚠"
        logger.info(
            "Screenshots taken: %d out of %d %s",
            total_taken, total_expected, symbol,
        )

    # ------------------------------------------------------------------
    # (4) Run metrics for each target
    # ------------------------------------------------------------------

    target_results: list[TargetMeasureResult] = []

    for idx, (target_video, target_meta) in enumerate(zip(target_videos, target_metas), start=1):
        eff_dur = effective_durations[target_video]

        logger.info("Measuring target %d of %d: %s", idx, len(target_videos), target_video.stem)
        metrics = await _run_metrics(
            source_video     = source_video,
            target_video     = target_video,
            crop_params      = resolved_crop,
            width            = width,
            metrics_dir      = measure_dir,
            graph_path       = graph_paths[target_video],
            subsample_factor = metrics_sampling,
            bar_title        = f"measuring target {idx:>2}",
        )

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

        target_results.append(TargetMeasureResult(
            target_video    = target_video,
            graph           = graph_paths[target_video],
            sidecar         = sidecar_paths[target_video],
            screenshots_dir = target_screenshots_dirs[target_video],
            metrics         = metrics,
        ))

    # ------------------------------------------------------------------
    # (5) Summary table
    # ------------------------------------------------------------------

    if target_results:
        _log_measure_summary(target_results)

    return MeasureResult(
        source_screenshots_dir = source_screenshots_dir,
        targets                = target_results,
    )
