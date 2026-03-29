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

from pyqenc.constants import CRF_GRANULARITY, PADDING_CRF
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
        """Get the tightest known CRF bracket around the quality target boundary.

        Args:
            targets: Quality targets to evaluate against

        Returns:
            Tuple of (fail_crf, pass_crf) where:
            - fail_crf: Lowest known CRF where quality was still below target
              (tightest failing bound — closest to the passing zone)
            - pass_crf: Highest known CRF where quality met all targets
              (tightest passing bound — closest to the failing zone)

            The optimal CRF lies in the open interval (pass_crf, fail_crf).
        """
        fail_crf: float | None = None  # lowest CRF that still failed (tightest upper bracket)
        pass_crf: float | None = None  # highest CRF that still passed (tightest lower bracket)

        for crf, metrics in self.attempts:
            all_met: bool = all(
                metrics.get(f"{t.metric}_{t.statistic}", -1.0) >= t.value
                for t in targets
            )
            if not all_met:
                # Keep the lowest failing CRF — it's the tightest upper bound
                if fail_crf is None or crf < fail_crf:
                    fail_crf = crf
            else:
                # Keep the highest passing CRF — it's the tightest lower bound
                if pass_crf is None or crf > pass_crf:
                    pass_crf = crf

        return (fail_crf, pass_crf)

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

_MAX_METRIC = 100.0
"""Upper bound of the normalized metric scale. All metrics are normalized to 0–100."""


def adjust_crf(
    current_crf:     float,
    quality_results: dict[str, float],
    quality_targets: list[QualityTarget],
    history:         CRFHistory,
    crf_min:         float = 1.0,
    crf_max:         float = 51.0,
) -> float | None:
    """Calculate the next CRF to try using linear interpolation between known bounds.

    Uses a unified two-axis proportional model:
    - CRF axis:    [pass_crf (or crf_min), fail_crf (or crf_max)]
    - Metric axis: [target_val, _MAX_METRIC]

    The worst-performing target determines the metric position.  Its signed
    distance from the target (positive = surplus, negative = deficit) is
    normalized against the metric range above the target, then mapped onto the
    CRF range to produce a proportional estimate of the next CRF.

    When both bounds are known the interpolation naturally converges; once the
    gap between them is ≤ CRF_GRANULARITY the search is exhausted and ``None``
    is returned so the caller keeps the last passing result.

    Args:
        current_crf:     CRF used in the most recent attempt.
        quality_results: Measured quality metrics (e.g. ``{'vmaf_min': 88.4}``).
        quality_targets: Quality targets to meet.
        history:         CRF attempt history for deduplication.
        crf_min:         Minimum valid CRF for the codec (default 1.0).
        crf_max:         Maximum valid CRF for the codec (default 51.0).

    Returns:
        Next CRF to try, or ``None`` when the search space is exhausted
        (caller should keep the last passing result).
    """
    fail_crf, pass_crf = history.get_bounds(quality_targets)

    # Exhaustion check: if the bracket is already tight enough, we're done.
    if fail_crf is not None and pass_crf is not None:
        if fail_crf - pass_crf <= CRF_GRANULARITY:
            return None

    # --- Find the worst-performing target (smallest surplus or largest deficit) ---
    worst_delta:  float            = float("inf")   # actual - target; lower = worse
    worst_target: QualityTarget | None = None
    worst_actual: float            = 0.0

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

    # --- Establish the tightest known CRF bracket and compute next CRF ---
    # pass_crf: highest CRF that passed  → lower bound of the search window
    # fail_crf: lowest  CRF that failed  → upper bound of the search window
    #
    # Signed ratio: (actual - target) / (100 - target), clamped to [-1, +1]
    #   positive → surplus (pass), negative → deficit (miss)
    #
    # Interpolation anchors from the bound on the *same side* as the current result:
    #   pass (ratio ≥ 0): anchor = pass_crf (or crf_min), project toward fail_crf (or crf_max)
    #     next = crf_lo + ratio * (crf_hi - crf_lo)
    #     ratio=0 → crf_lo (on target, stay at passing boundary)
    #     ratio=1 → crf_hi (maximum surplus, jump to failing boundary)
    #   miss (ratio < 0): anchor = fail_crf (or crf_max), project toward pass_crf (or crf_min)
    #     next = crf_hi + ratio * (crf_hi - crf_lo)
    #     ratio=0  → crf_hi (on target, stay at failing boundary)
    #     ratio=-1 → crf_lo (maximum deficit, jump to passing boundary)
    target_val   = worst_target.value
    metric_range = _MAX_METRIC - target_val          # always > 0 for sane targets
    ratio        = (worst_actual - target_val) / metric_range if metric_range > 0 else 0.0
    ratio        = max(-1.0, min(1.0, ratio))

    current_passed = ratio >= 0
    # Anchor selection:
    #   pass: lower anchor = pass_crf (or current_crf if no history), upper = fail_crf (or crf_max)
    #   miss: lower anchor = pass_crf (or crf_min), upper = fail_crf (or current_crf)
    # Using current_crf as anchor when the relevant bound is unknown keeps the first
    # proportional step relative to where we are rather than the codec extreme.
    if current_passed:
        crf_lo = pass_crf if pass_crf is not None else current_crf
        crf_hi = fail_crf if fail_crf is not None else crf_max
    else:
        crf_lo = pass_crf if pass_crf is not None else crf_min
        crf_hi = fail_crf if fail_crf is not None else current_crf

    if current_passed:
        next_crf = crf_lo + ratio * (crf_hi - crf_lo)
    else:
        next_crf = crf_hi + ratio * (crf_hi - crf_lo)

    logger.debug(
        f"CRF interpolation: pass={pass_crf}, fail={fail_crf} "
        f"metric target={target_val:.1f} actual={worst_actual:.2f} ratio={ratio:+.3f} "
        f"→ CRF {next_crf:{PADDING_CRF}}"
    )

    # --- Directional rounding to CRF granularity ---
    # Round away from the anchor to avoid landing back on an already-tried value
    # and to make each step as large as possible within the granularity grid:
    #   pass (moving up from crf_lo) → ceil  (step further from crf_lo)
    #   miss (moving down from crf_hi) → floor (step further from crf_hi)
    if current_passed:
        next_crf = math.ceil(next_crf / CRF_GRANULARITY) * CRF_GRANULARITY
    else:
        next_crf = math.floor(next_crf / CRF_GRANULARITY) * CRF_GRANULARITY
    next_crf = max(crf_min, min(crf_max, next_crf))

    # --- Deduplication: fall back to bracket midpoint ---
    if history.has_attempted(next_crf):
        logger.debug(f"CRF {next_crf:{PADDING_CRF}} already attempted, trying bracket midpoint")
        if fail_crf is not None and pass_crf is not None:
            gap = fail_crf - pass_crf
            if gap <= CRF_GRANULARITY:
                return None
            candidate = math.ceil((pass_crf + gap / 2) / CRF_GRANULARITY) * CRF_GRANULARITY
            if not history.has_attempted(candidate):
                return candidate
        logger.warning("CRF search space exhausted — no untried CRF available")
        return None

    return next_crf
