"""Standalone measure phase — data models and helpers.

Provides data models for measure results and helper functions for
crop resolution, resolution validation, duration parsing, screenshot
timestamp generation, and screenshot filename formatting.
"""

import asyncio
import hashlib
import logging
import re
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, Field

from pyqenc.constants import (
    DEFAULT_SCREENSHOT_COUNT,
    FFMPEG_ARG_VF,
    FFMPEG_MUXER_IMAGE2,
    MEASURE_DIR,
    TEMP_SUFFIX,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import CropParams, Resolution
from pyqenc.phases.extraction import _probe_streams_json
from pyqenc.phases.job import JobSidecar
from pyqenc.phases.probe import ProbeSidecar
from pyqenc.quality import MetricType, PerMetricStats, flatten_metric_stats
from pyqenc.stream_model import File, LongPathYaml, VideoStream, VideoStreamInfo
from pyqenc.utils.alive import AdvanceState, BarAdvance, ProgressBar
from pyqenc.utils.concurrency import run_bounded
from pyqenc.utils.ffmpeg_runner import FFmpegInput, FFmpegRequest, run_ffmpeg_async
from pyqenc.utils.fs import remove_stale_tmp_files
from pyqenc.utils.log_format import fmt_key_value_table, fmt_metric_value, fmt_size_mb
from pyqenc.utils.visualization import QualityMeasurer
from pyqenc.utils.yaml_utils import load_model, save_model

# ---------------------------------------------------------------------------
# The standalone-measure sidecar (re-homed, Req 21)
# ---------------------------------------------------------------------------

class MeasureSidecar(BaseModel):
    """Standalone measure sidecar (``<distorted_stem>.yaml``).

    Written by the ``measure`` command alongside the quality graph and
    screenshots.  Records source/distorted paths, durations, crop parameters,
    the frame subsampling factor, and per-metric statistics.

    The ``metrics`` field uses the same flat ``{metric_stat: value}`` format
    as ``AttemptSidecar`` (e.g. ``vmaf_min``, ``ssim_median``), with all
    ten statistics: min, p05, p10, p25, median, p75, p90, p95, max, std.
    The YAML key for the sampling factor is ``sampling``.
    """

    source_video:               LongPathYaml
    distorted_video:               LongPathYaml
    sampling:                   int
    crop_params:                dict[str, int] | None = None
    source_duration_seconds:    float | None          = None
    distorted_duration_seconds:    float | None          = None
    effective_duration_seconds: float | None          = None
    metrics:                    dict[str, float]      = Field(default_factory=dict)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DistortedMeasureResult:
    """Artifacts produced for a single distorted video."""

    distorted_video:    Path
    graph:           Path | None       # <distorted_stem>.png in measure_dir
    sidecar:         Path | None       # <distorted_stem>.yaml in measure_dir
    screenshots_dir: Path              # <distorted_stem>.screenshots/ in measure_dir
    metrics:         PerMetricStats # parsed metric statistics
    failed:          bool = False      # measurement failed (contained; empty metrics)


@dataclass
class MeasureResult:
    """All artifacts produced by a measure run."""

    source_screenshots_dir: Path                      # <source_stem>.screenshots/ in measure_dir
    distorted:              list[DistortedMeasureResult] # one entry per distorted video


@dataclass(frozen=True)
class ScreenshotPositions:
    """Immutable set of screenshot positions computed from the source video.

    frame_nums are 0-based frame indices from the source. fps and step are
    shared across all positions in a run. Timestamp derivation uses exact
    rational arithmetic via fractions.Fraction to avoid float drift.
    """
    frame_nums: list[int]   # canonical 0-based frame indices
    fps:        Fraction    # exact rational source fps (info.fps_fraction)
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
        fps:           Exact rational FPS (``VideoStreamInfo.fps_fraction``).
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
        fps:          Exact rational FPS (``VideoStreamInfo.fps_fraction``).
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
    2. If *crop_params* is ``None``: attempt to load ``probe.yaml`` from
       *work_dir*.

       - If the file exists and contains crop data: use that crop and log at
         debug level.
       - Otherwise: fall back to ``job.yaml`` source-path validation, then
         return an empty ``CropParams`` and log at info level.

    Neither ``probe.yaml`` nor ``job.yaml`` is ever written or modified by
    this function.

    Args:
        crop_params:  Explicit crop override, or ``None`` to auto-load.
        work_dir:     Directory that may contain ``probe.yaml`` and ``job.yaml``.
        source_video: Path to the source video (used for job.yaml source-match
                      check).

    Returns:
        Resolved ``CropParams`` (never ``None``).
    """
    if crop_params is not None:
        logger.debug("Using explicit crop params: %s", crop_params)
        return crop_params

    # Prefer probe.yaml — crop is owned by ProbePhase. Loading materializes
    # an empty CropParams when the key is absent, and an empty crop is a
    # concrete resolution ("no crop") — never a reason to keep probing.
    probe = load_model(work_dir / "probe.yaml", ProbeSidecar)
    if probe is not None:
        logger.debug("Loaded crop from probe.yaml: %s", probe.crop)
        return probe.crop

    # Fall back: validate source via job.yaml, then give up
    job_yaml = work_dir / "job.yaml"
    job = _load_job_source_path(job_yaml)

    if job is None:
        logger.info("No job.yaml found in %s — proceeding without crop", work_dir)
        return CropParams()

    if job.resolve() != source_video.resolve():
        logger.info(
            "job.yaml source (%s) does not match source video (%s) — proceeding without crop",
            job,
            source_video,
        )
        return CropParams()

    logger.info("probe.yaml found but contains no crop data — proceeding without crop")
    return CropParams()


def _load_job_source_path(job_yaml: Path) -> Path | None:
    """Read the source path recorded in ``job.yaml`` (the File dump).

    Serves the crop fallback's source-identity check.

    Args:
        job_yaml: The ``job.yaml`` path.

    Returns:
        The recorded source path, or ``None`` when absent/unparseable.
    """
    sidecar = load_model(job_yaml, JobSidecar)
    return sidecar.path if sidecar is not None else None


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


async def _run_metrics(
    source_video:     Path,
    distorted_video:     Path,
    crop_params:      CropParams,
    width:            int | None,
    metrics_dir:      Path,
    graph_path:       Path,
    sampling:         int,
    show_progress:    BarAdvance,
    duration_seconds: float,
    fps_value:        Fraction | None,
) -> PerMetricStats:
    """Run quality metric computation for one source/distorted pair.

    Delegates to ``QualityMeasurer.measure_chunk_async`` (measurement only —
    the measurer takes no target set, Req 51-53); metric statistics are
    returned. Progress feeds the caller's shared bar through *show_progress*
    (weighted ``PROGRESS`` ticks during the run).

    Args:
        source_video:     The reference (original) video path.
        distorted_video:     The distorted (encoded) video path.
        crop_params:      Crop applied to the source during metric computation.
        width:            Scale both inputs to this width after cropping
                          (``None`` or ``0`` = no scaling).
        metrics_dir:      Directory for raw metric log files (PSNR/SSIM/VMAF).
        graph_path:       Destination path for the quality metrics PNG plot.
        sampling:         Frame subsampling factor (≥1).
        show_progress:    The run's shared bar advance callable.
        duration_seconds: The comparison duration (progress/plot extent).
        fps_value:        Average fps for plot x-axis conversion.

    Returns:
        ``PerMetricStats`` mapping each ``MetricType`` to its key statistics.
    """

    measurer    = QualityMeasurer(metrics_dir)
    measurement = await measurer.measure_chunk_async(
        encoded          = distorted_video,
        reference        = FFmpegInput(path=source_video),
        ref_crop         = crop_params,
        output_dir       = metrics_dir,
        duration_seconds = duration_seconds,
        fps_value        = fps_value,
        sampling         = sampling,
        show_progress    = show_progress,
        plot_path        = graph_path,
        width            = width or 0,
    )
    return measurement.metrics



def _load_video_stream(path: Path) -> VideoStream:
    """Load an arbitrary video file as a :class:`VideoStream` (measure loader).

    Composes a :class:`~pyqenc.stream_model.File` with a freshly probed
    :class:`~pyqenc.stream_model.VideoStreamInfo` (fast facet of the first
    video stream).

    Args:
        path: The video file to probe.

    Returns:
        The video stream; missing ffprobe fields stay ``None``.
    """

    try:
        data = _probe_streams_json(path)
        raw = next(
            (r for r in data.get("streams", [])
             if r.get("codec_type") == "video"
             and (r.get("disposition") or {}).get("attached_pic", 0) != 1),
            None,
        )
    except (RuntimeError, OSError) as exc:
        logger.warning("Could not probe %s: %s", path.name, exc)
        raw = None
    info = VideoStreamInfo.from_ffprobe(raw) if raw is not None else VideoStreamInfo(track_id=0)
    return VideoStream(file=File(path=path), info=info)


def _effective_resolution(
    stream:     VideoStream,
    crop_params: CropParams,
    width:       int | None,
) -> tuple[int, int]:
    """Compute effective (width, height) after crop and optional scale.

    Args:
        stream:      Video stream with a probed ``resolution`` string.
        crop_params: Crop to apply (may be empty/no-op).
        width:       Scale-to width, or ``None`` for no scaling.

    Returns:
        ``(effective_width, effective_height)`` as integers.

    Raises:
        ValueError: If the stream's resolution is unavailable or unparseable.
    """
    if not stream.info.resolution:
        raise ValueError(f"Resolution unavailable for {stream.file.path}")
    cropped = Resolution.parse(stream.info.resolution) - crop_params

    if width is None:
        return cropped.width, cropped.height

    # Scale preserving aspect ratio: scale=width:-1 (height rounded to even)
    scale_h = round(cropped.height * width / cropped.width)
    # ffmpeg scale=-1 rounds to nearest even
    if scale_h % 2 != 0:
        scale_h += 1
    return width, scale_h


def _check_resolution_match(
    source_stream: VideoStream,
    distorted_stream: VideoStream,
    crop_params:   CropParams,
    width:         int | None,
) -> None:
    """Verify source and distorted video have matching effective resolution after crop and scale.

    Called once per distorted video, upfront before any processing begins.  Effective
    resolution = raw resolution minus crop pixels (source only), then scaled
    to *width* if provided.

    Args:
        source_stream: The source (reference) video stream.
        target_meta: Metadata for the distorted (encoded) video.
        crop_params: Crop applied to the source.
        width:       Optional scale width applied to both after cropping.

    Raises:
        ValueError: If effective resolutions differ, with an actionable
                    suggestion for ``--crop`` / ``--width`` arguments.
    """
    src_w, src_h = _effective_resolution(source_stream, crop_params, width)
    # Target is never cropped — pass empty CropParams
    tgt_w, tgt_h = _effective_resolution(distorted_stream, CropParams(), width)

    if src_w == tgt_w and src_h == tgt_h:
        return  # match — nothing to do

    # Build an actionable suggestion.
    # Raw dimensions (before any crop/scale applied here)
    src_parts = (source_stream.info.resolution or "?x?").split("x")
    tgt_parts = (distorted_stream.info.resolution or "?x?").split("x")
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
        f"vs distorted-video effective {tgt_w}x{tgt_h}. {suggestion}"
    )


def _build_resolution_suggestion(
    src_raw_w: int,
    src_raw_h: int,
    tgt_raw_w: int,
    tgt_raw_h: int,
    width:     int | None,
) -> str:
    """Build a human-readable ``--crop`` / ``--width`` suggestion string.

    Computes the vertical crop needed to align source height to the distorted video's height,
    then determines whether ``--width`` is also needed.

    Args:
        src_raw_w: Raw source width (before any crop/scale).
        src_raw_h: Raw source height (before any crop/scale).
        tgt_raw_w: Raw width of the distorted video.
        tgt_raw_h: Raw height of the distorted video.
        width:     Currently requested scale width (may be ``None``).

    Returns:
        A suggestion string suitable for inclusion in an error message.
    """
    if src_raw_h < tgt_raw_h:
        return (
            "Target is taller than source — vertical crop cannot fix this. "
            "Re-encode the distorted video at the correct resolution."
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
            f"Did you mean: --crop {top},{bottom}? "
            f"This would bring both videos to {final_w}x{final_h} for metric computation."
        )

    # Need --width to align horizontal dimension too
    suggested_width = tgt_raw_w
    # After crop+scale: source becomes suggested_width × (tgt_raw_h * suggested_width / src_raw_w)
    scaled_h = round(tgt_raw_h * suggested_width / src_raw_w)
    if scaled_h % 2 != 0:
        scaled_h += 1
    return (
        f"Did you mean: --crop {top},{bottom} --width {suggested_width}? "
        f"This would bring both videos to {suggested_width}x{scaled_h} for metric computation."
    )


# ---------------------------------------------------------------------------
# Sidecar YAML
# ---------------------------------------------------------------------------


def _write_sidecar(
    path:                       Path,
    source_video:               Path,
    distorted_video:            Path,
    sampling:                   int,
    crop_params:                CropParams,
    metrics:                    PerMetricStats,
    source_duration_seconds:    float | None,
    distorted_duration_seconds: float | None,
    effective_duration_seconds: float | None,
) -> None:
    """Write a metrics sidecar YAML file using the ``.tmp``-then-rename protocol.

    Records source/distorted paths, durations, sampling factor, crop parameters,
    and all computed metric statistics in the flat ``{metric_stat: value}``
    format consistent with ``AttemptSidecar`` (e.g. ``vmaf_min``, ``ssim_median``).
    On write failure logs a warning and returns without raising so that the
    graph and screenshots are not lost.

    Args:
        path:                       Destination path for the sidecar YAML.
        source_video:               Reference video path.
        distorted_video:            The distorted (encoded) video path.
        sampling:                   Frame subsampling factor used during computation.
        crop_params:                Resolved crop parameters applied to the source.
        metrics:                    Computed quality statistics per metric type.
        source_duration_seconds:    Source video duration in seconds, or ``None``.
        distorted_duration_seconds: Distorted video duration in seconds, or ``None``.
        effective_duration_seconds: ``min(source, distorted)`` duration, or ``None``.
    """
    crop_dict: dict[str, int] | None = {
        "top":    crop_params.top,
        "bottom": crop_params.bottom,
        "left":   crop_params.left,
        "right":  crop_params.right,
    }

    flat_metrics = flatten_metric_stats(metrics)

    sidecar = MeasureSidecar(
        source_video               = source_video,
        distorted_video            = distorted_video,
        source_duration_seconds    = source_duration_seconds,
        distorted_duration_seconds = distorted_duration_seconds,
        effective_duration_seconds = effective_duration_seconds,
        sampling                   = sampling,
        crop_params                = crop_dict,
        metrics                    = flat_metrics,
    )
    save_model(path, sidecar)
    logger.debug("Wrote metrics sidecar: %s", path)


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
    # Single-frame capture goes through the runner's .tmp protocol with the
    # image2 muxer (the .tmp extension hides the image container hint).
    output_args: tuple[str, ...] = ()
    if crop_params is not None and not crop_params.is_empty():
        output_args += (FFMPEG_ARG_VF, crop_params.to_ffmpeg_filter())
    output_args += ("-frames:v", "1", "-c:v", "png")
    request = FFmpegRequest(
        inputs        = [FFmpegInput(path=video_path, start_seconds=float(seek_ts))],
        output_args   = output_args,
        output        = output_path,
        output_format = FFMPEG_MUXER_IMAGE2,
    )
    try:
        result = await run_ffmpeg_async(request)
        if not result.success:
            reason = f"ffmpeg exit {result.returncode}"
            logger.debug("Strategy C frame failed (%s) seek_ts=%s output=%s", reason, seek_ts, output_path.name)
            return reason
        if not output_path.exists():
            reason = "no output file produced"
            logger.debug("Strategy C frame failed (%s) seek_ts=%s output=%s", reason, seek_ts, output_path.name)
            return reason
        return None
    except OSError as exc:
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
    # Multi-frame pattern output: writes into this caller-managed temp
    # directory with its own lifecycle — the sanctioned bypass of the .tmp
    # substitution protocol (the trailing null sink shares the input decode).
    request = FFmpegRequest(
        inputs      = [FFmpegInput(path=video_path)],
        output_args = (
            FFMPEG_ARG_VF, ",".join(vf_parts),
            "-vsync", "0",
            str(output_pattern),
        ),
    )
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        result = await run_ffmpeg_async(request)
        if not result.success:
            logger.debug("Single-pass capture failed (ffmpeg non-zero) tmp_dir=%s", tmp_dir)
            return []
    except OSError as exc:
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
        except OSError as exc:
            logger.warning("Failed to write screenshot %s: %s", final_name, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as e:
                logger.debug("Failed to remove screenshot temp file %s: %s", tmp_path.name, e)
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

    Partial results (fewer screenshots than positions, e.g. the distorted video shorter than
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
            except OSError as exc:
                logger.warning("Failed to rename screenshot %s: %s", final_name, exc)
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.debug("Failed to remove screenshot temp file %s: %s", tmp_path.name, e)
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


def _log_measure_summary(distorted: list[DistortedMeasureResult]) -> None:
    """Emit a summary table at INFO level after all metric computations complete.

    One row per distorted video showing: stem (truncated to 30 chars), file size in MB,
    and each metric's ``min…med`` pair for every MetricType that appears in
    any distorted video's metrics.  Missing metrics display as ``N/A``.

    Args:
        distorted: List of completed per-distorted-video results.
    """

    if not distorted:
        return

    # Collect all metric types that appear across any distorted video
    all_metric_types: list[MetricType] = []
    seen: set[MetricType] = set()
    for t in distorted:
        for mt in t.metrics:
            if mt not in seen:
                all_metric_types.append(mt)
                seen.add(mt)

    # Column widths
    STEM_WIDTH   = 30
    SIZE_WIDTH   = 9
    METRIC_WIDTH = 13
    range_suffix = " min…med"

    # Header
    metric_headers = "   ".join(
        f"{mt.value.upper()[:METRIC_WIDTH - len(range_suffix)]}{range_suffix}".rjust(METRIC_WIDTH)
        for mt in all_metric_types
    )
    header = f"{'Distorted':<{STEM_WIDTH}}   {'Size (MB)':>{SIZE_WIDTH}}   {metric_headers}"
    sep    = "─" * STEM_WIDTH + "   " + "─" * SIZE_WIDTH + "   " + ("─" * METRIC_WIDTH + "   ") * len(all_metric_types)

    logger.info(header)
    logger.info(sep.rstrip())

    for t in distorted:
        stem      = t.distorted_video.stem[:STEM_WIDTH]
        if t.failed:
            logger.info("   ".join([f"✘ {stem:<{STEM_WIDTH - 2}}", f"{'FAILED':>{SIZE_WIDTH}}"]))
            continue
        size_str  = fmt_size_mb(t.distorted_video.stat().st_size) if t.distorted_video.exists() else "N/A"
        row_parts = [f"{stem:<{STEM_WIDTH}}", f"{size_str:>{SIZE_WIDTH}}"]
        for mt in all_metric_types:
            stats = t.metrics.get(mt, {})
            low   = stats.get("min")
            med   = stats.get("median")
            cell  = f"{fmt_metric_value(low)}…{fmt_metric_value(med)}" if low is not None and med is not None else "N/A"
            row_parts.append(f"{cell:>{METRIC_WIDTH}}")
        logger.info("   ".join(row_parts))


# ---------------------------------------------------------------------------
# Top-level async entry point
# ---------------------------------------------------------------------------


async def run_measure(
    source_video:             Path,
    distorted_videos:         list[Path],
    work_dir:                 Path,
    crop_params:              CropParams | None,
    sampling:                 int,
    concurrency:              int,
    width:                    int | None,
    screenshot_count:         int | None,
    screenshot_interval:      float | None      = None,
    screenshot_include_edges: bool              = False,
) -> MeasureResult:
    """Execute a standalone quality measurement run.

    Execution order: (1) compute screenshot positions from source, (2) one
    task per video through the bounded runner at *concurrency* — the source
    task takes its screenshots; each distorted task takes that video's
    screenshots, then measures and writes the sidecar — (3) emit the summary
    table. A per-video measurement failure is contained (the video gets a
    failed result row; the run continues) and surfaces in the summary.

    Args:
        source_video:             Reference video path.
        distorted_videos:         Encoded videos to measure.  Pass an empty list to
                                  run in screenshots-only mode (no metrics, graph, or
                                  sidecar).
        work_dir:                 Working directory; outputs go under
                                  ``work_dir/measure/``.
        crop_params:              Explicit crop (or empty ``CropParams`` for no-crop).
                                  Pass ``None`` to auto-load from ``job.yaml`` if
                                  present.
        sampling:                 Frame subsampling factor (≥1).  Ignored in
                                  screenshots-only mode.
        concurrency:              Maximum videos processed simultaneously (≥1).
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
        ``MeasureResult`` with source screenshots directory and per-distorted-video
        results.

    Raises:
        FileNotFoundError: If ``source_video`` or any path in ``distorted_videos``
                           does not exist.
        ValueError:        If ``sampling`` < 1, ``concurrency`` < 1,
                           ``screenshot_count`` < 1, or any resolution mismatch is
                           detected.
    """

    # ------------------------------------------------------------------
    # Input validation and crop resolution
    # ------------------------------------------------------------------

    if not source_video.exists():
        raise FileNotFoundError(f"Source video not found: {source_video}")

    for distorted in distorted_videos:
        if not distorted.exists():
            raise FileNotFoundError(f"Distorted video not found: {distorted}")

    if sampling < 1:
        raise ValueError(f"sampling must be ≥ 1, got {sampling}")

    if concurrency < 1:
        raise ValueError(f"concurrency must be ≥ 1, got {concurrency}")

    effective_screenshot_count = screenshot_count if screenshot_count is not None else DEFAULT_SCREENSHOT_COUNT
    if effective_screenshot_count < 1:
        raise ValueError(f"screenshot_count must be ≥ 1, got {effective_screenshot_count}")

    resolved_crop    = _resolve_crop(crop_params, work_dir, source_video)
    screenshots_only = len(distorted_videos) == 0

    # ------------------------------------------------------------------
    # Resolution validation and parameter summary logging
    # ------------------------------------------------------------------

    source_stream = _load_video_stream(source_video)

    if not screenshots_only:
        distorted_streams: list[VideoStream] = [_load_video_stream(t) for t in distorted_videos]

        for distorted_video, distorted_stream in zip(distorted_videos, distorted_streams):
            try:
                _check_resolution_match(source_stream, distorted_stream, resolved_crop, width)
            except ValueError as exc:
                raise ValueError(
                    f"Resolution check failed for {distorted_video.name}: {exc}"
                ) from exc
    else:
        distorted_streams = []

    fmt_key_value_table({
        "source":      str(source_video),
        "distorted":  [f"{i+1:>2} - {t.name}" for i, t in enumerate(distorted_videos)] if distorted_videos else ["(none — screenshots only)"],
        "crop":        str(resolved_crop) if not resolved_crop.is_empty() else "none",
        "width":       str(width) if width else "none",
        "sampling":    str(sampling),
        "screenshots": f"every {screenshot_interval}s (cap {effective_screenshot_count})" if screenshot_interval else str(effective_screenshot_count),
    })

    # ------------------------------------------------------------------
    # Directory creation
    # ------------------------------------------------------------------

    measure_dir            = work_dir / MEASURE_DIR
    source_screenshots_dir = measure_dir / source_video.stem

    distorted_screenshots_dirs: dict[Path, Path] = {}
    graph_paths:             dict[Path, Path] = {}
    sidecar_paths:           dict[Path, Path] = {}

    for distorted_video in distorted_videos:
        stem = distorted_video.stem
        distorted_screenshots_dirs[distorted_video] = measure_dir / stem
        graph_paths[distorted_video]             = measure_dir / f"{stem}.png"
        sidecar_paths[distorted_video]           = measure_dir / f"{stem}.yaml"

    measure_dir.mkdir(parents=True, exist_ok=True)

    # Startup cleanup: remove any stale .tmp metric files from interrupted runs
    remove_stale_tmp_files(measure_dir)

    # ------------------------------------------------------------------
    # Duration probing (needed for sidecar and duration-mismatch warnings)
    # ------------------------------------------------------------------

    source_duration: float | None = source_stream.info.duration_seconds
    if source_duration is None:
        logger.warning("Duration unavailable for source video: %s", source_video.name)

    distorted_durations: dict[Path, float | None] = {}
    for distorted_video, distorted_stream in zip(distorted_videos, distorted_streams):
        dur = distorted_stream.info.duration_seconds
        if dur is None:
            logger.warning("Duration unavailable for distorted video: %s", distorted_video.name)
        distorted_durations[distorted_video] = dur

    effective_durations: dict[Path, float | None] = {}
    for distorted_video in distorted_videos:
        t_dur = distorted_durations[distorted_video]
        if source_duration is not None and t_dur is not None:
            eff = min(source_duration, t_dur)
            if abs(source_duration - t_dur) > 1.0:
                logger.warning(
                    "Duration mismatch for %s: source=%.2fs, distorted=%.2fs — using effective=%.2fs",
                    distorted_video.name, source_duration, t_dur, eff,
                )
            effective_durations[distorted_video] = eff
        else:
            effective_durations[distorted_video] = None

    # ------------------------------------------------------------------
    # (2) One task per video through the bounded runner — the source task
    #     takes its screenshots; each distorted task takes that video's
    #     screenshots, then measures and writes the sidecar.
    # ------------------------------------------------------------------

    if screenshots_only:
        logger.info("Running in screenshots-only mode (no distorted videos provided)")

    positions: ScreenshotPositions | None = None

    # Probe fps first (fast ffprobe call, already triggered by resolution check above)
    source_fps_frac = source_stream.info.fps_fraction

    # frame_count is not available to the standalone measure command (it has
    # no ProbePhase result) — use a duration-based estimate as the fallback.
    source_frame_count: int | None = None

    if source_fps_frac is not None:
        if screenshot_interval is not None:
            # Interval mode: use frame count as upper bound when duration is known;
            # fall back to no upper bound (ffmpeg stops at EOF) when it is not.
            total_frames_bound: int | None = (
                int(source_duration * float(source_fps_frac))
                if source_duration is not None
                else None
            )
            positions = compute_screenshot_positions_interval(
                fps          = source_fps_frac,
                interval_s   = screenshot_interval,
                total_frames = total_frames_bound,
                cap          = effective_screenshot_count,
            )
            logger.debug(
                "Screenshot positions: %d frames, interval=%.3fs, fps=%s",
                len(positions.frame_nums), screenshot_interval, positions.fps,
            )
        else:
            # Count mode: requires a frame count; derive from duration when available.
            if source_duration is not None:
                source_frame_count = int(source_duration * float(source_fps_frac))
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
            else:
                logger.error(
                    "Cannot compute screenshot positions: duration unavailable for source %s"
                    " — skipping screenshots",
                    source_video.name,
                )
    else:
        logger.error(
            "Cannot compute screenshot positions: fps unavailable for source %s — skipping screenshots",
            source_video.name,
        )

    source_screenshots: list[Path] = []
    distorted_screenshots_map: dict[Path, list[Path]] = {}
    results_by_video: dict[Path, DistortedMeasureResult] = {}

    # Duration-based shared bar over the distorted videos' effective
    # durations (screenshots-only mode: zero total → indeterminate spinner).
    bar_total = sum(d for d in effective_durations.values() if d is not None)
    jobs: list[tuple[Path, bool]] = [(source_video, False)] + [
        (video, True) for video in distorted_videos
    ]

    with ProgressBar(bar_total, title="Measure", total_count=len(jobs)) as advance:

        async def process(job: tuple[Path, bool]) -> None:
            """One video's task: screenshots, then (distorted) metrics + sidecar."""
            video, is_distorted = job

            # Screenshots — shared positions, per-video directory.
            shots: list[Path] = []
            if positions is not None:
                logger.info("Taking %d screenshots of %s...", len(positions.frame_nums), video.stem)
                try:
                    shots = await make_screenshots(
                        video_path      = video,
                        positions       = positions,
                        screenshots_dir = (
                            distorted_screenshots_dirs[video] if is_distorted
                            else source_screenshots_dir
                        ),
                        crop_params     = (
                            None if is_distorted
                            else (resolved_crop if not resolved_crop.is_empty() else None)
                        ),
                    )
                except OSError as exc:
                    logger.error("Screenshots failed for %s — skipping: %s", video.stem, exc)

            if not is_distorted:
                nonlocal source_screenshots
                source_screenshots = shots
                advance(0, AdvanceState.SUCCESS)
                return

            distorted_screenshots_map[video] = shots

            # Measurement + sidecar — contained per video (Req 10): a failure
            # yields a failed result row, never an aborted run.
            eff_dur = effective_durations[video]
            logger.info("Measuring %s", video.stem)
            metrics: PerMetricStats = {}
            failed  = False
            try:
                metrics = await _run_metrics(
                    source_video     = source_video,
                    distorted_video  = video,
                    crop_params      = resolved_crop,
                    width            = width,
                    metrics_dir      = measure_dir,
                    graph_path       = graph_paths[video],
                    sampling         = sampling,
                    show_progress    = advance,
                    duration_seconds = eff_dur or 0.0,
                    fps_value        = source_stream.info.fps_fraction,
                )
                _write_sidecar(
                    path                       = sidecar_paths[video],
                    source_video               = source_video,
                    distorted_video            = video,
                    sampling                   = sampling,
                    crop_params                = resolved_crop,
                    metrics                    = metrics,
                    source_duration_seconds    = source_duration,
                    distorted_duration_seconds    = distorted_durations[video],
                    effective_duration_seconds = eff_dur,
                )
            except (OSError, ValueError) as exc:
                failed = True
                logger.error("Measurement failed for %s — skipping: %s", video.stem, exc)

            results_by_video[video] = DistortedMeasureResult(
                distorted_video    = video,
                graph           = graph_paths[video],
                sidecar         = sidecar_paths[video],
                screenshots_dir = distorted_screenshots_dirs[video],
                metrics         = metrics,
                failed          = failed,
            )
            advance(eff_dur or 0, AdvanceState.FAILED if failed else AdvanceState.SUCCESS)

        asyncio.run(run_bounded(jobs, concurrency, process))
        advance(0, AdvanceState.COMPLETE)

    # Summary log after the task run (all screenshots and results are in)
    if positions is not None:
        expected_per_video = len(positions.frame_nums)
        all_videos         = [source_video] + list(distorted_videos)
        total_expected     = expected_per_video * len(all_videos)
        total_taken        = len(source_screenshots) + sum(len(s) for s in distorted_screenshots_map.values())
        symbol             = "✅" if total_taken == total_expected else "⚠"
        logger.info(
            "Screenshots taken: %d out of %d %s",
            total_taken, total_expected, symbol,
        )

    # ------------------------------------------------------------------
    # (3) Summary table
    # ------------------------------------------------------------------

    distorted_results = [results_by_video[v] for v in distorted_videos]

    if distorted_results:
        _log_measure_summary(distorted_results)

    return MeasureResult(
        source_screenshots_dir = source_screenshots_dir,
        distorted              = distorted_results,
    )
