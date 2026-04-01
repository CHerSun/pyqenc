"""
Quality evaluation and CRF adjustment for encoding pipeline.

This module provides quality evaluation against targets and CRF adjustment
algorithms for iterative encoding optimization.
"""
# CHerSun 2026

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import TypedDict, assert_never

import pandas as pd

from pyqenc.constants import CRF_GRANULARITY, CRF_METRIC_POSITIVE_DELTA, PADDING_CRF
from pyqenc.utils.ffmpeg_runner import (
    FFmpegRunResult,
    ProgressCallback,
    run_ffmpeg_async,
)

from .models import CropParams, QualityTarget

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric types
# ---------------------------------------------------------------------------

class MetricType(Enum):
    """Supported video quality metrics."""

    VMAF = "vmaf"
    SSIM = "ssim"
    PSNR = "psnr"

class MetricStats(TypedDict):
    """Key statistics for a single metric."""

    min:    float
    median: float
    max:    float
    std:    float

ChunkQualityStats = dict[MetricType, MetricStats]
"""Quality statistics for a video chunk across all metrics."""



class _MetricStatistics(TypedDict):
    """Full percentile statistics used internally."""

    min: float
    p5:  float
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float
    p95: float
    max: float
    std: float


@dataclass
class MetricData:
    """Pure data container for a single metric (DataFrame + column name)."""

    df:     pd.DataFrame
    column: str


async def run_metric(
    metric:            MetricType,
    distorted:         Path,
    reference:         Path,
    crop_distorted:    CropParams,
    crop_reference:    CropParams,
    duration:          int,
    width:             int,
    use_gpu:           bool,
    subsample:         int,
    output_prefix:     str,
    cwd:               Path | None             = None,
    progress_callback: ProgressCallback | None = None,
    output_extension:  str | None              = None,
) -> FFmpegRunResult:
    """Build and run a single metric calculation subprocess via FFmpegRunner.

    ffmpeg is run with ``cwd`` set to the distorted file's directory (or an
    explicit override) so that ``output_prefix`` can be a plain UUID-based
    filename with no path separators or special characters.

    Args:
        metric:            Metric to compute.
        distorted:         Path to the distorted (encoded) video.
        reference:         Path to the reference video.
        crop_distorted:    Crop parameters for the distorted input.
        crop_reference:    Crop parameters for the reference input.
        duration:          Limit comparison to this many seconds (0 = full video).
        width:             Scale both inputs to this width (0 = no scaling).
        use_gpu:           Use GPU-accelerated VMAF (``libvmaf_cuda``).
        subsample:         Frame subsampling factor (1 = every frame).
        output_prefix:     Simple filename prefix (no path separators) for metric
                           output files written relative to ``cwd``.
        cwd:               Working directory for the ffmpeg process.  Defaults to
                           the parent directory of ``distorted``.
        progress_callback: Optional ``(frame, out_time_seconds)`` callable
                           invoked once per completed progress block.
        output_extension:  Override the default file extension for the metric
                           output file (e.g. ``".tmp"``).  When ``None``, the
                           default extension for each metric is used (``.log``
                           for PSNR/SSIM, ``.json`` for VMAF).

    Returns:
        ``FFmpegRunResult`` with returncode, success, stderr_lines, and frame_count.
    """
    if cwd is None:
        cwd = distorted.parent

    width_str = f",scale={width}:-1" if width else ""

    if metric != MetricType.VMAF and subsample > 1:
        # PSNR / SSIM: apply frame selection at the video-stream level
        f_distorted = (
            f"[0:v]{crop_distorted.to_ffmpeg_filter()}{width_str}"
            f",select='not(mod(n,{subsample}))',setpts=PTS-STARTPTS[main]"
        )
        f_reference = (
            f"[1:v]{crop_reference.to_ffmpeg_filter()}{width_str}"
            f",select='not(mod(n,{subsample}))',setpts=PTS-STARTPTS[ref]"
        )
    else:
        f_distorted = (
            f"[0:v]{crop_distorted.to_ffmpeg_filter()}{width_str},setpts=PTS-STARTPTS[main]"
        )
        f_reference = (
            f"[1:v]{crop_reference.to_ffmpeg_filter()}{width_str},setpts=PTS-STARTPTS[ref]"
        )

    filter_start = f"{f_distorted};{f_reference};[main][ref]"

    # output_prefix is a plain filename (no path separators) — no escaping needed
    if metric == MetricType.VMAF:
        lib: str     = "libvmaf_cuda" if use_gpu else "libvmaf"
        options: str = "" if use_gpu else "n_threads=4:"
        if subsample > 1:
            options += f"n_subsample={subsample}:"
        vmaf_ext = output_extension if output_extension is not None else ".json"
        filter_metric = (
            f"{lib}={options}log_path={output_prefix}{metric.value}{vmaf_ext}:log_fmt=json"
        )
    elif metric == MetricType.SSIM:
        ssim_ext = output_extension if output_extension is not None else ".log"
        filter_metric = f"ssim=stats_file={output_prefix}{metric.value}{ssim_ext}"
    elif metric == MetricType.PSNR:
        psnr_ext = output_extension if output_extension is not None else ".log"
        filter_metric = f"psnr=stats_file={output_prefix}{metric.value}{psnr_ext}"
    else:
        assert_never(metric)

    cmd: list[str | PathLike] = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-progress", "pipe:1",
    ]
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.extend(["-i", distorted.resolve()])
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.extend(["-i", reference.resolve()])
    cmd.extend(["-filter_complex", filter_start + filter_metric])
    cmd.extend(["-f", "null", "-"])

    return await run_ffmpeg_async(cmd, output_file=None, progress_callback=progress_callback, video_meta=None, cwd=cwd)


@dataclass
class QualityArtifacts:
    """Artifacts generated during quality evaluation.

    Attributes:
        psnr_log: Path to PSNR log file
        ssim_log: Path to SSIM log file
        vmaf_json: Path to VMAF JSON file
        plot: Path to unified metrics plot
        stats_files: Paths to individual statistics files
    """

    psnr_log: Path | None = None
    ssim_log: Path | None = None
    vmaf_json: Path | None = None
    plot: Path | None = None
    stats_files: list[Path] = field(default_factory=list)


@dataclass
class QualityEvaluation:
    """Result of quality evaluation against targets.

    Attributes:
        metrics: Parsed quality metrics with statistics
        targets_met: Whether all quality targets were met
        failed_targets: List of targets that were not met
        artifacts: Paths to generated metric files and plots
    """

    metrics: ChunkQualityStats
    targets_met: bool
    failed_targets: list[QualityTarget]
    artifacts: QualityArtifacts


@dataclass
class CRFHistory:
    """Track the tightest known pass/fail CRF bracket and attempt count.

    ``fail_crf`` and ``pass_crf`` are initialized to the codec limits as
    sentinels, then narrowed on each attempt.  ``get_bounds`` therefore always
    returns valid floats — no ``None`` checks needed downstream.

    Attributes:
        fail_crf:      Lowest CRF that still failed (upper bracket bound).
                       Initialized to ``crf_max``; narrows downward on misses.
        pass_crf:      Highest CRF that still passed (lower bracket bound).
                       Initialized to ``crf_min``; narrows upward on passes.
        attempt_count: Number of encoding attempts recorded.
    """

    fail_crf:      float
    pass_crf:      float
    attempts: int = 0

    def add(self, crf: float, passed: bool) -> None:
        """Record an encoding attempt and update the pass/fail bracket.

        Args:
            crf:    CRF value used.
            passed: Whether all quality targets were met.
        """
        self.attempts += 1
        if not passed:
            if crf < self.fail_crf:
                self.fail_crf = crf
        else:
            if crf > self.pass_crf:
                self.pass_crf = crf

def normalize_metric(metric_type: MetricType, value: float) -> float:
    """Normalize a raw metric value to the 0–100 scale.

    Applies the canonical normalization for each metric type:
    - SSIM: multiply by 100 (raw 0–1 → 0–100)
    - PSNR: cap at 100.0 (unbounded dB → 0–100)
    - VMAF: unchanged (already 0–100)

    After normalization is applied at the parsing boundary (in
    ``analyze_chunk_quality``), all downstream code works with values already
    on the 0–100 scale and should NOT call this function again.

    Args:
        metric_type: Type of the metric (VMAF, SSIM, PSNR).
        value: The raw metric value to normalize.

    Returns:
        The normalized metric value on the 0–100 scale.
    """
    if metric_type == MetricType.SSIM:
        return value * 100
    elif metric_type == MetricType.PSNR:
        return min(value, 100.0)
    elif metric_type == MetricType.VMAF:
        return value
    else:
        assert_never(metric_type)


_MAX_METRIC = 100.0
"""Upper bound of the normalized metric scale. All metrics are normalized to 0–100."""


def adjust_crf(
    current_crf:     float,
    quality_results: dict[str, float],
    quality_targets: list[QualityTarget],
    history:         CRFHistory,
) -> float | None:
    """Calculate the next CRF to try using linear interpolation between known bounds.

    Uses a unified two-axis proportional model:
    - CRF axis:    [pass_crf, fail_crf]  (initialized to codec limits as sentinels)
    - Metric axis: asymmetric range depending on pass/miss

    The worst-performing target determines the metric position.  Its signed
    distance from the target (positive = surplus, negative = deficit) is
    normalized against an asymmetric metric range, then mapped onto the CRF
    range to produce a proportional estimate of the next CRF:
    - Surplus: range = MAX - target  (headroom above the target)
    - Deficit: range = MAX - actual  (headroom above the measured value)

    The deficit range scales naturally with how far the actual value is from
    the ceiling — a small miss near the top of the scale is treated as more
    significant than the same absolute miss at a lower value.

    When the bracket narrows to ≤ CRF_GRANULARITY the search is exhausted and
    ``None`` is returned so the caller keeps the last passing result.

    Additionally, if all targets are met and the least-proficient metric surplus
    is within ``CRF_METRIC_POSITIVE_DELTA``, the result is accepted immediately
    without attempting to squeeze to a higher CRF — saving an extra encoding pass.

    Args:
        current_crf:     CRF used in the most recent attempt.
        quality_results: Measured quality metrics (e.g. ``{'vmaf_min': 88.4}``).
        quality_targets: Quality targets to meet.
        history:         CRF attempt history; carries codec CRF limits as sentinels.

    Returns:
        Next CRF to try, or ``None`` when the search space is exhausted
        (caller should keep the last passing result).
    """
    fail_crf, pass_crf = history.fail_crf, history.pass_crf

    # Exhaustion check: bracket tight enough, we're done.
    if fail_crf - pass_crf <= CRF_GRANULARITY:
        return None

    # --- Find the worst-performing target (smallest surplus or largest deficit) ---
    worst_delta:  float             = float("inf")   # actual - target; lower = worse
    worst_target: QualityTarget | None = None
    worst_actual: float             = 0.0

    for target in quality_targets:
        metric_key = f"{target.metric}_{target.statistic}"
        actual = quality_results.get(metric_key)
        if actual is None:
            continue
        # Treat PSNR=inf as a capped value so arithmetic stays finite
        if MetricType(target.metric) == MetricType.PSNR and actual == float("inf"):
            actual = _MAX_METRIC
        delta = actual - target.value
        if delta < worst_delta:
            worst_delta  = delta
            worst_target = target
            worst_actual = actual

    if worst_target is None:
        logger.warning("No valid metric results found, cannot adjust CRF")
        return None

    # --- Early acceptance: all metrics pass and the tightest surplus is within delta ---
    if worst_delta >= 0 and worst_delta <= CRF_METRIC_POSITIVE_DELTA:
        logger.debug(
            f"Least-proficient metric surplus {worst_delta:.3f} ≤ CRF_METRIC_POSITIVE_DELTA "
            f"({CRF_METRIC_POSITIVE_DELTA}), accepting CRF {current_crf:{PADDING_CRF}} as final."
        )
        return None

    # --- Interpolate next CRF ---
    # Signed ratio: worst_delta / range, clamped to [-1, +1]
    #   positive → surplus (pass), negative → deficit (miss)
    #   Surplus range = MAX - target  (headroom above target)
    #   Deficit range = MAX - actual  (headroom above actual)
    #   min(target, actual) selects the correct denominator without branching.
    #
    # Interpolation from the anchor on the same side as the current result:
    #   pass (ratio ≥ 0): next = pass_crf + ratio * (fail_crf - pass_crf)
    #     ratio=0 → pass_crf (on target), ratio=1 → fail_crf (max surplus)
    #   miss (ratio < 0): next = fail_crf + ratio * (fail_crf - pass_crf)
    #     ratio=0 → fail_crf (on target), ratio=-1 → pass_crf (max deficit)
    target_val   = worst_target.value
    metric_range = _MAX_METRIC - min(target_val, worst_actual)
    ratio        = worst_delta / metric_range if metric_range > 0 else 0.0
    ratio        = max(-1.0, min(1.0, ratio))

    current_passed = ratio >= 0
    crf_span       = fail_crf - pass_crf
    if current_passed:
        next_crf = pass_crf + ratio * crf_span
        next_crf = math.ceil(next_crf / CRF_GRANULARITY) * CRF_GRANULARITY
    else:
        next_crf = fail_crf + ratio * crf_span
        next_crf = math.floor(next_crf / CRF_GRANULARITY) * CRF_GRANULARITY
    next_crf = max(history.pass_crf, min(history.fail_crf, next_crf))

    logger.debug(
        f"CRF interpolation: pass={pass_crf}, fail={fail_crf} "
        f"metric target={target_val:.1f} actual={worst_actual:.2f} ratio={ratio:+.3f} "
        f"→ CRF {next_crf:{PADDING_CRF}}"
    )

    return next_crf
