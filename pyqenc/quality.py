"""
Quality evaluation and CRF adjustment for encoding pipeline.

This module provides quality evaluation against targets and CRF adjustment
algorithms for iterative encoding optimization.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import TypedDict, assert_never

import pandas as pd

from pyqenc.constants import CRF_GRANULARITY, PADDING_CRF
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult, ProgressCallback, run_ffmpeg_async

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
    """Track CRF attempts to prevent cycles and enable smart adjustment.

    Attributes:
        attempts: List of (crf, metrics) tuples for all attempts
    """

    attempts: list[tuple[float, dict[str, float]]] = field(default_factory=list)

    def add_attempt(self, crf: float, metrics: dict[str, float]) -> None:
        """Record an encoding attempt.

        Args:
            crf: CRF value used
            metrics: Quality metrics achieved
        """
        self.attempts.append((crf, metrics))

    def has_attempted(self, crf: float, tolerance: float = 0.1) -> bool:
        """Check if CRF has been attempted (within tolerance).

        Args:
            crf: CRF value to check
            tolerance: Tolerance for CRF comparison

        Returns:
            True if CRF has been attempted within tolerance
        """
        return any(
            abs(attempted_crf - crf) < tolerance
            for attempted_crf, _ in self.attempts
        )

    def get_bounds(
        self,
        targets: list[QualityTarget]
    ) -> tuple[float | None, float | None]:
        """Get CRF bounds where quality was too low/high.

        Args:
            targets: Quality targets to evaluate against

        Returns:
            Tuple of (too_low_crf, too_high_crf) where:
            - too_low_crf: Highest CRF where quality was below target
            - too_high_crf: Lowest CRF where quality was above target
        """
        too_low_crf:  float | None = None  # CRF where quality was below target
        too_high_crf: float | None = None  # CRF where quality was above target

        for crf, metrics in self.attempts:
            # Check if this attempt met all targets
            all_met: bool = True
            for target in targets:
                metric_key = f"{target.metric}_{target.statistic}"
                actual = metrics.get(metric_key)
                if actual is None or actual < target.value:
                    all_met = False
                    break

            if not all_met:
                # Quality was below target
                if too_low_crf is None or crf < too_low_crf:
                    too_low_crf = crf
            else:
                # Quality was above target
                if too_high_crf is None or crf > too_high_crf:
                    too_high_crf = crf

        return (too_low_crf, too_high_crf)

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

def normalize_metric_deficit(
    metric_type: MetricType,
    actual:      float,
    target:      float,
) -> float:
    """Compute quality deficit on the 0–100 scale for consistent CRF adjustment.

    Both ``actual`` and ``target`` must already be on the 0–100 scale
    (i.e. values returned by ``analyze_chunk_quality`` or ``normalize_metric``).

    Args:
        metric_type: Metric type enum value (unused; kept for API compatibility).
        actual:      Actual measured value, already normalized to 0–100.
        target:      Target value on the 0–100 scale.

    Returns:
        Deficit — positive when quality exceeds target, negative when below.
    """
    return actual - target

def adjust_crf(
    current_crf:     float,
    quality_results: dict[str, float],
    quality_targets: list[QualityTarget],
    history:         CRFHistory,
    crf_min:         float = 1.0,
    crf_max:         float = 51.0,
) -> float | None:
    """Calculate next CRF using proportional interpolation with binary-search refinement.

    The search has two phases:
    1. **Find a passing CRF** — if targets are not met, interpolate proportionally
       toward crf_min to increase quality.
    2. **Squeeze toward optimal** — if targets ARE met, try a higher CRF (larger
       value = smaller file) to find the efficiency boundary.  Binary search
       between the last-passing and last-failing CRF until the gap is ≤ CRF_GRANULARITY
       (minimum granularity), at which point the search is exhausted and None
       is returned so the caller keeps the last passing result.

    Args:
        current_crf:     CRF used in the most recent attempt.
        quality_results: Measured quality metrics (e.g. ``{'vmaf_median': 92.0}``).
        quality_targets: Quality targets to meet.
        history:         CRF attempt history for deduplication.
        crf_min:         Minimum valid CRF for the codec (default 1.0).
        crf_max:         Maximum valid CRF for the codec (default 51.0).

    Returns:
        Next CRF to try, or ``None`` when the search space is exhausted
        (caller should keep the last successful result).
    """
    too_low_crf, too_high_crf = history.get_bounds(quality_targets)

    # Determine whether current attempt passed
    current_passed = all(
        quality_results.get(f"{t.metric}_{t.statistic}", 0) >= t.value
        for t in quality_targets
    )

    if current_passed:
        # Targets met — try squeezing higher (larger CRF = smaller file)
        if too_low_crf is not None:
            # We have a known failing CRF above us — binary search between
            # current (passing) and that failing point
            gap = too_low_crf - current_crf
            if gap <= CRF_GRANULARITY:
                # Search space exhausted — current_crf is optimal
                return None
            next_crf = current_crf + gap / 2
        else:
            # No known failing CRF yet — make a proportional jump upward.
            # Use the worst-passing target to estimate how much headroom we have.
            worst_margin = float("inf")
            for target in quality_targets:
                metric_key = f"{target.metric}_{target.statistic}"
                actual = quality_results.get(metric_key)
                if actual is None:
                    continue
                deficit = normalize_metric_deficit(MetricType(target.metric), actual, target.value)
                worst_margin = min(worst_margin, deficit)

            if worst_margin == float("inf") or worst_margin <= 0:
                return None

            # Proportional upward step: margin / (max_metric - target) * remaining CRF range
            metric_type = MetricType(quality_targets[0].metric)
            max_metric = 100.0 # we adjust PSNR to 100 on inf.
            target_val = quality_targets[0].value
            headroom = max_metric - target_val
            ratio = min(1.0, worst_margin / headroom) if headroom > 0 else 0.5
            next_crf = current_crf + ratio * (crf_max - current_crf)

    else:
        # Targets not met — interpolate toward crf_min to increase quality
        worst_deficit: float       = 0.0
        worst_target:  QualityTarget | None = None
        worst_actual:  float       = 0.0

        for target in quality_targets:
            metric_key = f"{target.metric}_{target.statistic}"
            actual = quality_results.get(metric_key)
            if actual is None:
                continue
            deficit = normalize_metric_deficit(MetricType(target.metric), actual, target.value)
            if deficit < worst_deficit or worst_target is None:
                worst_deficit = deficit
                worst_target  = target
                worst_actual  = actual

        if worst_target is None:
            logger.warning("No valid deficits calculated, cannot adjust CRF")
            return None

        metric_type = MetricType(worst_target.metric)
        actual_pct = worst_actual if not (metric_type == MetricType.PSNR and worst_actual == float("inf")) else 60.0
        target_pct = worst_target.value
        max_metric = 100.0

        headroom = max_metric - actual_pct
        if headroom > 0:
            ratio = min(1.0, (target_pct - actual_pct) / headroom)
        else:
            ratio = 1.0

        next_crf = current_crf - ratio * (current_crf - crf_min)

        # Clamp: must be strictly below any known failing CRF
        if too_low_crf is not None:
            next_crf = min(next_crf, too_low_crf - CRF_GRANULARITY)

    # Clamp: must be strictly above any known passing CRF
    if too_high_crf is not None:
        next_crf = max(next_crf, too_high_crf + CRF_GRANULARITY)

    # Binary search fallback when both bounds are known and we're outside the window
    if too_low_crf is not None and too_high_crf is not None:
        gap = too_low_crf - too_high_crf
        if gap <= CRF_GRANULARITY:
            return None  # Optimal found
        if next_crf >= too_low_crf or next_crf <= too_high_crf:
            next_crf = too_high_crf + gap / 2

    # Round to CRF granularity and hard-clamp
    next_crf = round(next_crf / CRF_GRANULARITY) * CRF_GRANULARITY
    next_crf = max(crf_min, min(crf_max, next_crf))

    # Deduplication
    if history.has_attempted(next_crf):
        logger.debug(f"CRF {next_crf:{PADDING_CRF}} already attempted, falling back to binary search")
        if too_low_crf is not None and too_high_crf is not None:
            gap = too_low_crf - too_high_crf
            if gap <= CRF_GRANULARITY:
                return None
            candidate = round((too_high_crf + gap / 2) * (1 / CRF_GRANULARITY)) / (1 / CRF_GRANULARITY)
            if not history.has_attempted(candidate):
                return candidate
        logger.warning("CRF search space exhausted — no untried CRF available")
        return None

    return next_crf
