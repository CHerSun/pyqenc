"""
Quality evaluation and CRF adjustment for encoding pipeline.

This module provides quality evaluation against targets and CRF adjustment
algorithms for iterative encoding optimization.
"""
# CHerSun 2026

import logging
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import Iterable, Protocol, TypedDict, TypeVar, assert_never, runtime_checkable

import pandas as pd

from pyqenc.utils.ffmpeg_runner import (
    FFmpegRunResult,
    ProgressCallback,
    run_ffmpeg_async,
)

from .models import CropParams, QualityTarget

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricInfo:
    """Descriptor carrying all metric-specific properties.

    Single source of truth for normalization, display, and CRF search behaviour.
    Accessed via ``MetricType.PSNR.info``, etc.

    Public fields: ``name``, ``id``, ``higher_is_better``, ``lossless_value``,
    ``lossless_raw_repr``, ``display_unit``, ``plot_y_min``, ``plot_y_max``,
    ``comparison_range``, ``acceptance_delta``.

    Internal fields (not part of public API — used only by ``normalize()``):
    ``_offset``, ``_scale_factor``, ``_clip_lower``, ``_clip_upper``.

    Attributes:
        name:              Human-readable display name (e.g. ``"PSNR"``).
        id:                Lowercase string key matching ``MetricType.value``
                           (e.g. ``"psnr"``).  Used for dict keys and filenames.
        higher_is_better:  ``True`` when a higher normalized value is better
                           (VMAF, SSIM, PSNR, VIF after normalization).
        _offset:           Additive offset applied before scaling in the
                           normalization formula: ``normalized = _offset + raw * _scale_factor``.
                           VIF uses ``100.0`` (inverts the scale); all others use ``0.0``.
        _scale_factor:     Multiply the raw value by this after adding ``_offset``.
                           SSIM raw range is 0–1, so ``_scale_factor=100.0``.
                           VIF uses ``-1.0`` to invert. All others use ``1.0``.
        _clip_upper:       After applying the formula, clip values above this threshold.
                           ``None`` means no upper clipping.
                           PSNR: ``100.0`` (caps ∞ dB).
        _clip_lower:       After applying the formula, clip values below this threshold.
                           ``None`` means no lower clipping.
                           VIF: ``0.0`` (prevents negative normalized values).
        lossless_value:    Normalized value that represents lossless quality
                           (``100.0`` for all current metrics after normalization).
        lossless_raw_repr: Human-readable string for the raw lossless value
                           (e.g. ``"∞ dB"`` for PSNR, ``"100.0"`` for SSIM/VMAF/VIF).
        display_unit:      Unit suffix for display (e.g. ``" dB"``, ``"%"``).
        plot_y_min:        Lower bound for the Y-axis in plots (normalized scale).
        plot_y_max:        Upper bound for the Y-axis in plots (normalized scale).
                           Slightly above ``lossless_value`` to leave headroom.
        comparison_range:  Practical value span used *only* for normalizing
                           cross-metric deficit comparisons (``_score_failing_attempt``).
                           Not the theoretical lossless ceiling — the realistic range
                           where quality targets are set and misses occur.
                           VMAF ≈ 20 (targets typically 80–100), SSIM ≈ 10 (90–100),
                           PSNR ≈ 30 (40–70 dB), VIF ≈ 5 (empirical; limited data).
        acceptance_delta:  Per-metric threshold for early acceptance during CRF search.
                           When all targets pass and every surplus is within this delta,
                           the current quality value is accepted as final without further
                           search — saves an extra encoding pass when the result is
                           already close enough.  Values are in normalized metric units
                           (post-scale_factor).
        subsample_via_filter: When ``True``, the metric uses a stream-level
                           ``select='not(mod(n,factor))'`` filter on its branch in the
                           combined ffmpeg pass (PSNR, SSIM).  When ``False``,
                           subsampling is handled internally by the filter itself
                           (VMAF via ``n_subsample``) or is not applicable (VIF).
    """

    name:                 str
    id:                   str
    higher_is_better:     bool
    _offset:              float
    _scale_factor:        float
    _clip_upper:          float | None
    _clip_lower:          float | None
    lossless_value:       float
    lossless_raw_repr:    str
    display_unit:         str
    plot_y_min:           float
    plot_y_max:           float
    comparison_range:     float
    acceptance_delta:     float
    subsample_via_filter: bool

    def normalize(self, value: float | pd.Series) -> float | pd.Series:
        """Normalize a raw metric value (or Series) to the display scale.

        Applies ``scale_factor``, then ``clip_lower``, then ``clip_upper``
        in that order.  Handles ``float("inf")`` by treating it as
        ``clip_upper`` (or ``lossless_value`` when no upper clip is set).

        Args:
            value: Raw scalar or ``pd.Series`` from the metric log file.

        Returns:
            Normalized scalar or ``pd.Series`` on the display scale.
        """
        if isinstance(value, pd.Series):
            result = self._offset + value * self._scale_factor
            if self._clip_lower is not None:
                result = result.clip(lower=self._clip_lower)
            if self._clip_upper is not None:
                result = result.clip(upper=self._clip_upper)
            return result
        else:
            # scalar path — handle inf explicitly
            result_f = self._offset + value * self._scale_factor
            if self._clip_lower is not None:
                result_f = max(result_f, self._clip_lower)
            if self._clip_upper is not None:
                result_f = min(result_f, self._clip_upper)
            return result_f

    def passes(self, actual: float, target: float) -> bool:
        """Return ``True`` when *actual* satisfies *target* for this metric.

        For ``higher_is_better`` metrics: ``actual >= target``.
        For inverted metrics (e.g. VIF): ``actual <= target``.

        Args:
            actual: Normalized measured value.
            target: Normalized target threshold.
        """
        return actual >= target if self.higher_is_better else actual <= target

    def deficit(self, actual: float, target: float) -> float:
        """Signed distance from target; negative means the target is not met.

        For ``higher_is_better``: ``actual - target`` (positive = surplus).
        For inverted: ``target - actual`` (positive = surplus, i.e. actual is
        lower than target which is good).

        Args:
            actual: Normalized measured value.
            target: Normalized target threshold.
        """
        return (actual - target) if self.higher_is_better else (target - actual)


# ---------------------------------------------------------------------------
# Metric types
# ---------------------------------------------------------------------------

class MetricType(Enum):
    """Supported video quality metrics.

    Each member's ``.value`` is the lowercase string id used in filenames,
    dict keys, and ffmpeg filter names.  Use ``.info`` to access the full
    ``MetricInfo`` descriptor for normalization, display, and CRF search.
    """

    VMAF = "vmaf"
    SSIM = "ssim"
    PSNR = "psnr"
    VIF  = "vif"

    @property
    def info(self) -> MetricInfo:
        """Return the ``MetricInfo`` descriptor for this metric type."""
        return _METRIC_INFO[self]


_METRIC_INFO: dict[MetricType, MetricInfo] = {
    MetricType.VMAF: MetricInfo(
        name              = "VMAF",
        id                = "vmaf",
        higher_is_better  = True,
        _offset           = 0.0,
        _scale_factor     = 1.0,
        _clip_upper       = None,
        _clip_lower       = None,
        lossless_value    = 100.0,
        lossless_raw_repr = "100.0",
        display_unit      = "",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        comparison_range  = 20.0,  # practical target range ~80–100
        acceptance_delta  = 0.15,  # 0.15% surplus should be negligible
        subsample_via_filter = False,  # VMAF uses n_subsample internally
    ),
    MetricType.SSIM: MetricInfo(
        name              = "SSIM",
        id                = "ssim",
        higher_is_better  = True,
        _offset           = 0.0,
        _scale_factor     = 100.0,
        _clip_upper       = None,
        _clip_lower       = None,
        lossless_value    = 100.0,
        lossless_raw_repr = "100.0",
        display_unit      = "",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        comparison_range  = 10.0,  # practical target range ~90–100
        acceptance_delta  = 0.05,  # 0.05% after scaling (0.0005 raw)
        subsample_via_filter = True,   # uses select='not(mod(n,factor))' on its branch
    ),
    MetricType.PSNR: MetricInfo(
        name              = "PSNR",
        id                = "psnr",
        higher_is_better  = True,
        _offset           = 0.0,
        _scale_factor     = 1.0,
        _clip_upper       = 100.0,
        _clip_lower       = None,
        lossless_value    = 100.0,
        lossless_raw_repr = "∞ dB",
        display_unit      = " dB",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        comparison_range  = 30.0,  # practical target range ~40–70 dB
        acceptance_delta  = 0.5,   # 0.5 dB surplus is negligible
        subsample_via_filter = True,   # uses select='not(mod(n,factor))' on its branch
    ),
    # VIF
    MetricType.VIF: MetricInfo(
        name              = "VIF",
        id                = "vif",
        higher_is_better  = True,
        _offset           = 0.0,
        _scale_factor     = 100.0,
        _clip_upper       = 100.0,
        _clip_lower       = 0.0,
        lossless_value    = 100.0,
        lossless_raw_repr = "100.0",
        display_unit      = "",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        comparison_range  = 10.0,
        acceptance_delta  = 0.2,
        subsample_via_filter = False,  # VIF has no independent branch; embedded in VMAF
    ),
}


class MetricStats(TypedDict):
    """Key statistics for a single metric.

    Subset of ``_MetricStatistics`` stored in sidecars and used for targeting.
    Includes the same percentile selection used by the visualization plots:
    min, p05, p25, median (p50), p75, p95, max, std.
    """

    min:    float
    p05:    float
    p25:    float
    median: float
    p75:    float
    p95:    float
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


async def run_metrics(
    metrics:           Iterable[MetricType],
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
    """Build and run a single ffmpeg pass computing all requested metrics simultaneously.

    Uses ``split[]`` to fan decoded frames to multiple metric filters in one process,
    saving the cost of repeated video decoding.  PSNR and SSIM branches apply
    ``select='not(mod(n,subsample))'`` when ``subsample > 1``
    (``MetricInfo.subsample_via_filter == True``).  VMAF uses ``n_subsample``
    internally.  VIF is always embedded in the VMAF pass via ``feature=name=vif``
    and is not a separate branch.

    ffmpeg is run with ``cwd`` set to the distorted file's directory (or an
    explicit override) so that ``output_prefix`` can be a plain UUID-based
    filename with no path separators or special characters.

    Args:
        metrics:           Metrics to compute.  VIF is automatically included when
                           VMAF is present (or when VIF is requested, VMAF is added).
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
        output_extension:  Override the default file extension for metric output
                           files (e.g. ``".tmp"``).  When ``None``, defaults are
                           used (``.log`` for PSNR/SSIM, ``.json`` for VMAF).

    Returns:
        ``FFmpegRunResult`` with returncode, success, stderr_lines, and frame_count.

    Raises:
        ValueError: If ``metrics`` is empty after deduplication.
    """
    if cwd is None:
        cwd = distorted.parent

    # Deduplicate and ensure VMAF is present when VIF is requested
    active: set[MetricType] = set(metrics)
    if not active:
        raise ValueError("run_metrics: metrics set is empty")
    if MetricType.VIF in active:
        active.add(MetricType.VMAF)

    # Independent branches: VIF is not a branch (embedded in VMAF)
    branches: list[MetricType] = [m for m in MetricType if m in active and m != MetricType.VIF]
    n_branches = len(branches)

    width_str = f",scale={width}:-1" if width else ""
    crop_d    = crop_distorted.to_ffmpeg_filter()
    crop_r    = crop_reference.to_ffmpeg_filter()

    # Build shared input streams with split
    if n_branches == 1:
        # No split needed — single branch uses [main]/[ref] directly
        sel = f",select='not(mod(n,{subsample}))',setpts=PTS-STARTPTS" if (
            subsample > 1 and branches[0].info.subsample_via_filter
        ) else ",setpts=PTS-STARTPTS"
        f_dist = f"[0:v]{crop_d}{width_str}{sel}[main]"
        f_ref  = f"[1:v]{crop_r}{width_str}{sel}[ref]"
        branch_labels_d = ["main"]
        branch_labels_r = ["ref"]
    else:
        # Split into N branches; each branch gets its own label
        split_labels_d = "".join(f"[d{i}]" for i in range(n_branches))
        split_labels_r = "".join(f"[r{i}]" for i in range(n_branches))
        f_dist_base = f"[0:v]{crop_d}{width_str},split={n_branches}{split_labels_d}"
        f_ref_base  = f"[1:v]{crop_r}{width_str},split={n_branches}{split_labels_r}"

        # Per-branch select/setpts
        branch_parts_d: list[str] = []
        branch_parts_r: list[str] = []
        branch_labels_d = []
        branch_labels_r = []
        for i, branch in enumerate(branches):
            label_d = f"main{i}"
            label_r = f"ref{i}"
            if subsample > 1 and branch.info.subsample_via_filter:
                sel = f"select='not(mod(n,{subsample}))',setpts=PTS-STARTPTS"
                branch_parts_d.append(f"[d{i}]{sel}[{label_d}]")
                branch_parts_r.append(f"[r{i}]{sel}[{label_r}]")
            else:
                branch_parts_d.append(f"[d{i}]setpts=PTS-STARTPTS[{label_d}]")
                branch_parts_r.append(f"[r{i}]setpts=PTS-STARTPTS[{label_r}]")
            branch_labels_d.append(label_d)
            branch_labels_r.append(label_r)

        f_dist = f_dist_base + ";" + ";".join(branch_parts_d)
        f_ref  = f_ref_base  + ";" + ";".join(branch_parts_r)

    # Build per-branch metric filters
    metric_filters: list[str] = []
    for i, branch in enumerate(branches):
        ld = branch_labels_d[i]
        lr = branch_labels_r[i]
        if branch == MetricType.VMAF:
            lib: str     = "libvmaf_cuda" if use_gpu else "libvmaf"
            options: str = "" if use_gpu else "n_threads=4:"
            if subsample > 1:
                options += f"n_subsample={subsample}:"
            ext = output_extension if output_extension is not None else ".json"
            metric_filters.append(
                f"[{ld}][{lr}]{lib}={options}"
                f"log_path={output_prefix}{branch.value}{ext}:log_fmt=json:feature=name=vif"
            )
        elif branch == MetricType.SSIM:
            ext = output_extension if output_extension is not None else ".log"
            metric_filters.append(
                f"[{ld}][{lr}]ssim=stats_file={output_prefix}{branch.value}{ext}"
            )
        elif branch == MetricType.PSNR:
            ext = output_extension if output_extension is not None else ".log"
            metric_filters.append(
                f"[{ld}][{lr}]psnr=stats_file={output_prefix}{branch.value}{ext}"
            )

    filter_complex = f"{f_dist};{f_ref};" + ";".join(metric_filters)

    cmd: list[str | PathLike] = ["ffmpeg", "-hide_banner", "-nostats", "-progress", "pipe:1"]
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.extend(["-i", distorted.resolve()])
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.extend(["-i", reference.resolve()])
    cmd.extend(["-filter_complex", filter_complex, "-f", "null", "-"])

    return await run_ffmpeg_async(
        cmd, output_file=None, progress_callback=progress_callback, video_meta=None, cwd=cwd,
    )



@dataclass
class QualityArtifacts:
    """Artifacts generated during quality evaluation.

    All log paths point to ``.tmp``-suffixed files during their lifetime —
    they are never renamed to canonical names.  Each file is deleted
    immediately after successful parsing by ``analyze_chunk_quality``.

    Attributes:
        psnr_log:  Path to PSNR ``.tmp`` log file, or ``None`` if not generated.
        ssim_log:  Path to SSIM ``.tmp`` log file, or ``None`` if not generated.
        vmaf_json: Path to VMAF ``.tmp`` JSON file, or ``None`` if not generated.
        vif_log:   Path to VIF ``.tmp`` log file, or ``None`` if not generated.
        plot:      Path to unified metrics PNG plot, or ``None`` before plotting.
    """

    psnr_log:  Path | None = None
    ssim_log:  Path | None = None
    vmaf_json: Path | None = None
    vif_log:   Path | None = None
    plot:      Path | None = None


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



def normalize_metric(metric_type: MetricType, value: float) -> float:
    """Normalize a raw scalar metric value using the metric's ``MetricInfo`` descriptor.

    Delegates to ``metric_type.info.normalize(value)``.  Kept as a module-level
    function for call-site compatibility; prefer ``metric_type.info.normalize``
    for new code.

    Args:
        metric_type: Type of the metric.
        value:       Raw scalar value from the metric log file.

    Returns:
        Normalized scalar on the metric's display scale.
    """
    result = metric_type.info.normalize(value)
    assert isinstance(result, float)
    return result


def _find_worst_target(
    quality_results: dict[str, float],
    quality_targets: list[QualityTarget],
) -> tuple[QualityTarget, float, float] | None:
    """Find the worst-performing target and return it with its deficit and actual value.

    "Worst" means the smallest ``MetricInfo.deficit(actual, target)`` — i.e. the
    target with the least surplus (or largest deficit).  Uses ``MetricInfo.deficit``
    so the comparison is direction-aware for both higher-is-better and inverted metrics.

    Metrics are expected to be already normalized (post ``MetricInfo.normalize``),
    as stored in sidecars.

    Args:
        quality_results: Measured quality metrics keyed as ``"<metric>_<stat>"``.
        quality_targets: Quality targets to evaluate.

    Returns:
        ``(worst_target, worst_deficit, worst_actual)`` or ``None`` when no
        target has a valid result in *quality_results*.
    """
    worst_deficit: float              = float("inf")
    worst_target:  QualityTarget | None = None
    worst_actual:  float              = 0.0

    for target in quality_targets:
        metric_key = f"{target.metric}_{target.statistic}"
        actual     = quality_results.get(metric_key)
        if actual is None:
            continue
        info    = MetricType(target.metric).info
        deficit = info.deficit(actual, target.value)
        if deficit < worst_deficit:
            worst_deficit = deficit
            worst_target  = target
            worst_actual  = actual

    if worst_target is None:
        return None
    return worst_target, worst_deficit, worst_actual


def _score_attempt(
    metrics:         dict[str, float],
    quality_targets: list[QualityTarget],
) -> float:
    """Compute a signed composite score for an encoding attempt.

    Metrics must be already normalized (post ``MetricInfo.normalize``), as stored
    in sidecars.  No normalization is performed here.

    Returns a float encoding both pass/fail and distance from the sweet spot:
    - ``0.0``: all targets pass and every surplus ≤ ``acceptance_delta`` (early acceptance).
    - Positive: all targets pass, at least one surplus > ``acceptance_delta``.
      Value is ``sum(surplus / comparison_range)`` for all targets.
    - Negative: at least one target fails.
      Value is ``sum(deficit / comparison_range)`` for failing targets only.

    ``abs(score)`` measures distance from the sweet spot — closest to zero = best attempt.

    Args:
        metrics:         Measured quality metrics keyed as ``"<metric>_<stat>"``,
                         already normalized.
        quality_targets: Quality targets to evaluate against.

    Returns:
        Signed float score.

    Raises:
        ValueError: If any target key is absent from ``metrics``.
    """
    # Validate all keys are present up front.
    for target in quality_targets:
        key = f"{target.metric}_{target.statistic}"
        if key not in metrics:
            raise ValueError(
                f"Missing metric key '{key}' in metrics dict. "
                f"Available keys: {list(metrics.keys())}"
            )

    fail_score   = 0.0
    pass_score   = 0.0
    early_accept = False

    for target in quality_targets:
        key    = f"{target.metric}_{target.statistic}"
        actual = metrics[key]
        info   = MetricType(target.metric).info

        deficit    = info.deficit(actual, target.value)
        normalized = deficit / info.comparison_range

        logger.debug("_score_attempt: %s_%s actual=%.4f target=%.4f deficit=%.4f normalized=%.6f",
                     target.metric, target.statistic, actual, target.value, deficit, normalized)

        if deficit < 0.0:
            fail_score  += normalized
        else:
            pass_score += normalized
            if abs(deficit) <= info.acceptance_delta:
                early_accept = True

    return fail_score or (0.0 if early_accept else pass_score)

@dataclass
class QualityPoint:
    q: Decimal
    '''Quality value of this point'''
    score: float
    '''Composite score of this point. 0 = either missing (initial) or matched targets (final). Otherwise always >0 or <0'''
    metrics: dict[str, float]|None
    '''Metrics of this point'''

    @property
    def is_sentinel(self) -> bool:
        return self.metrics is None

    @property
    def is_pass(self) -> bool:
        return self.score >= 0 and not self.is_sentinel

    @property
    def is_fail(self) -> bool:
        return self.score < 0 and not self.is_sentinel

    @property
    def is_winner(self) -> bool:
        return self.score == 0 and not self.is_sentinel

T_numeric = TypeVar('T_numeric', int, float, Decimal)

def _in_range(value: T_numeric, start: T_numeric, end: T_numeric) -> bool:
    """Check if a value belongs to a given range, handling inverted ranges too."""
    low = min(start, end)
    high = max(start, end)
    return low <= value <= high

def _clamp_to_range(q: Decimal, granularity: Decimal, worse_point: QualityPoint, better_point: QualityPoint) -> Decimal | None:
    """Clamp *q* to the given range. Respects granularity and sentinels. Returns ``None`` when no interior point exists.
    """
    lower = min(better_point.q, worse_point.q)
    upper = max(better_point.q, worse_point.q)
    lo = (
        lower
        if (better_point.is_sentinel and lower == better_point.q)
        or (worse_point.is_sentinel and lower == worse_point.q)
        else lower + granularity
    )
    hi = (
        upper
        if (better_point.is_sentinel and upper == better_point.q)
        or (worse_point.is_sentinel and upper == worse_point.q)
        else upper - granularity
    )
    if lo > hi:
        return None
    return max(lo, min(hi, q))

def _compute_proportional_candidate(target: QualityTarget|None, pass_point: QualityPoint, fail_point: QualityPoint, clamp_range: bool = True) -> float | None:
    """Return interpolation fraction t ∈ [0, 1] for *target* metric value, or ``None`` for fallback."""
    # No metrics? Fallback via None for other options - externally managed.
    if target is None or pass_point.is_sentinel or fail_point.is_sentinel:
        return None

    # Get actual metric values.
    key   = f"{target.metric}_{target.statistic}"
    pass_val = pass_point.metrics.get(key) # ty:ignore[unresolved-attribute] # we've checked for this via is_sentinel
    fail_val = fail_point.metrics.get(key) # ty:ignore[unresolved-attribute] # we've checked for this via is_sentinel
    if pass_val is None or fail_val is None:
        logger.warning(f"QualitySearch: missing metric {key} for pass={pass_point.q} fail={fail_point.q}.")
        return None

    # Compute interpolation fraction.
    info   = MetricType(target.metric).info
    d_pass = info.deficit(pass_val, target.value)
    d_fail = info.deficit(fail_val, target.value)
    span   = d_pass - d_fail
    if abs(span) < 1e-9: # Avoid division by zero
        return None
    t = d_pass / span

    return t if not clamp_range or 0.0 <= t <= 1.0 else None



@runtime_checkable
class QualitySearchProtocol(Protocol):
    """Structural interface for quality search algorithms.

    All quality search implementations must satisfy this protocol.
    Constructor arguments (``quality_better``, ``quality_worse``,
    ``quality_targets``, ``granularity``, ``quality_max_step``) are
    supplied at construction time, not per-call.
    """

    @property
    def attempts(self) -> int:
        """Total number of ``record()`` calls made so far."""
        ...

    @property
    def best_quality(self) -> Decimal | None:
        """Best quality value found so far.

        Best-efficiency passing value if any pass exists, otherwise the
        best-fail value (highest ``_score_attempt`` score).
        ``None`` before any ``record()`` call.
        """
        ...

    @property
    def best_metrics(self) -> dict[str, float] | None:
        """Full metrics dict associated with ``best_quality``.
        ``None`` before any ``record()`` call.
        """
        ...

    @property
    def best_targets_met(self) -> bool:
        """``True`` iff ``best_quality`` corresponds to a passing attempt."""
        ...

    def record(self, quality: Decimal, quality_results: dict[str, float]) -> Decimal | None:
        """Record one attempt result and return the next quality value to try.

        Updates internal state with the result of encoding at *quality*.
        Returns the next quality value to try, or ``None`` when the search
        is exhausted or the current result is accepted as final.

        Once ``None`` is returned, all subsequent calls must also return ``None``
        without mutating state.

        Args:
            quality:         Quality value used for this attempt.
            quality_results: Measured quality metrics keyed as ``"<metric>_<stat>"``.

        Returns:
            Next quality value (quantized ``Decimal``), or ``None``.
        """
        ...


class QualitySearch:
    """Quality search using proportional interpolation (legacy algorithm).

    Encapsulates the binary-bracket search previously split across
    ``CRFHistory`` and ``adjust_crf()``.  Direction-agnostic: uses
    ``quality_better``/``quality_worse`` instead of assuming lower=better.

    Args:
        quality_better:   Better-quality boundary (codec range start).
        quality_worse:    Worse-quality boundary (codec range end).
        quality_targets:  Quality targets to meet.
        granularity:      Step size as a ``Decimal``.
        quality_max_step: Optional maximum absolute step size per iteration.

    Raises:
        ValueError: If ``quality_better == quality_worse`` or ``granularity <= 0``.
    """

    def __init__(
        self,
        quality_better:   Decimal,
        quality_worse:    Decimal,
        quality_targets:  list[QualityTarget],
        granularity:      Decimal,
        quality_max_step: Decimal | None = None,
    ) -> None:
        if quality_better == quality_worse:
            raise ValueError(f"quality_better ({quality_better}) must differ from quality_worse ({quality_worse})")
        if granularity <= 0:
            raise ValueError(f"granularity must be > 0, got {granularity}")

        self._quality_targets:  list[QualityTarget]  = quality_targets
        self._granularity:      Decimal              = granularity
        self._quality_max_step: Decimal | None       = quality_max_step
        self._attempts:     int                      = 0
        self._exhausted:    bool                     = False

        # Higher-quality point (lower CRF, higher VBR)
        self._better_point = QualityPoint(quality_better, 0, None)

        # Lower-quality point (higher CRF, lower VBR)
        self._worse_point = QualityPoint(quality_worse, 0, None)

    # ------------------------------------------------------------------
    # Protocol properties
    # ------------------------------------------------------------------

    @property
    def attempts(self) -> int:
        """Total number of ``record()`` calls made so far."""
        return self._attempts

    @property
    def best_quality(self) -> Decimal | None:
        """Best quality found: passing value if any pass, else best-fail value."""
        if self._better_point.is_sentinel:
            return None
        return self._better_point.q

    @property
    def best_metrics(self) -> dict[str, float] | None:
        """Metrics dict associated with ``best_quality``."""
        if self._better_point.is_sentinel:
            return None
        return self._better_point.metrics

    @property
    def best_targets_met(self) -> bool:
        """``True`` if at least one passing attempt was recorded."""
        return not self._better_point.is_sentinel

    # ------------------------------------------------------------------
    # record()
    # ------------------------------------------------------------------

    def record(self, quality: Decimal, quality_results: dict[str, float]) -> Decimal | None:
        """Record one attempt and return the next quality value to try.

        Mirrors the logic of the former ``adjust_crf()`` function using
        instance state instead of a ``CRFHistory`` object.

        Args:
            quality:         Quality value used for this attempt.
            quality_results: Measured quality metrics keyed as ``"<metric>_<stat>"``.

        Returns:
            Next quality value (quantized ``Decimal``), or ``None`` when exhausted.
        """
        if self._exhausted:
            return None

        self._attempts += 1

        # Score the attempt — catch missing-key errors and treat as worst fail.
        try:
            new_point = QualityPoint(quality, _score_attempt(quality_results, self._quality_targets), quality_results)
        except ValueError:
            raise ValueError("QualitySearch: missing metric key for quality=%s" % quality)

        # Early acceptance if score is zero (or None) - meaning we reach acceptable quality within delta.
        if new_point.is_winner:
            self._middle_point = new_point
            self._exhausted    = True
            logger.debug("QualitySearch: early acceptance at quality=%s", quality)
            return None

        # Update the bracket
        if new_point.is_pass:
            self._better_point = new_point
        else:
            self._worse_point = new_point

        # Exhaustion check: bracket collapsed to ≤ granularity.
        if (
            not (self._better_point.is_sentinel or self._worse_point.is_sentinel)
            and abs(self._better_point.q - self._worse_point.q) <= self._granularity
        ):
            self._exhausted = True
            return None

        return self._compute_next(new_point, self._worse_point, self._better_point)

    def _compute_next(self, new_point: QualityPoint, worse_point: QualityPoint, better_point: QualityPoint) -> Decimal|None:
        """Compute the next quality value to try. Returns ``None`` if exhausted."""
        # Compute next quality via proportional interpolation.
        q_span = worse_point.q - better_point.q

        # Determine worst target from current result and from opposite boundary.
        opposite_metrics = worse_point.metrics if new_point.is_pass else better_point.metrics
        opp_worst        = _find_worst_target(opposite_metrics, self._quality_targets) if opposite_metrics else None
        opp_target       = opp_worst[0] if opp_worst is not None else None

        found_worst = _find_worst_target(new_point.metrics, self._quality_targets) # ty: ignore
        worst_target = found_worst[0] if found_worst is not None else None

        # Make candidates list - direct proportional, reversed proportional, true binary.
        candidates: list[tuple[float|None, str]] = []

        if worst_target is not None:
            candidates.append((_compute_proportional_candidate(worst_target, better_point, worse_point),
                               f"primary worst={worst_target.metric}_{worst_target.statistic}"))
        if opp_target is not None:
            candidates.append((_compute_proportional_candidate(opp_target, better_point, worse_point),
                               f"reverse worst={opp_target.metric}_{opp_target.statistic}"))
        candidates.append((0.5, "binary"))

        # Get first non-None candidate.
        # TODO: No fallback to reversed if direct is too close to boundary?
        t_chosen, label = next((c for c in candidates if c[0]))
        raw_q = better_point.q + Decimal(str(t_chosen)) * q_span

        # Apply max-step clamping before quantization.
        if self._quality_max_step is not None:
            step = raw_q - new_point.q
            if abs(step) > self._quality_max_step:
                raw_q = new_point.q + (self._quality_max_step if step > 0 else -self._quality_max_step)

        # Snap to nearest granularity step.
        next_q = (raw_q / self._granularity).to_integral_value(ROUND_HALF_EVEN) * self._granularity
        next_q = next_q.quantize(self._granularity)

        result = _clamp_to_range(next_q, self._granularity, worse_point, better_point)

        _pad = len(str(max(abs(better_point.q), abs(worse_point.q)).quantize(self._granularity)))
        logger.debug(
            "QualitySearch [%s]: better=%s worse=%s t=%.3f raw=%s → %s",
            label,
            str(better_point.q).rjust(_pad),
            str(worse_point.q).rjust(_pad),
            t_chosen,
            str(raw_q.quantize(self._granularity)).rjust(_pad),
            str(result).rjust(_pad) if result is not None else "None (exhausted)",
        )

        if result is None:
            self._exhausted = True
            return None
        return result

class QualitySearchV2:
    """Quality search using the 3-point sweet-spot algorithm.

    Tracks three anchor points — pass sentinel, best, and fail sentinel — and
    converges by repeatedly halving the larger of the two active ranges:

    - Range A: ``[_pass_q ... _best_q]``  (between pass sentinel and best)
    - Range B: ``[_best_q ... _fail_q]``  (between best and fail sentinel)

    **Phase 1 — all-failing** (``_pass_metrics is None``):
    Searches toward ``quality_better`` until a result scores *worse* than the
    current best, at which point that result becomes the pass sentinel and the
    algorithm transitions to 3-point mode.

    **Phase 1 — all-passing** (``_fail_metrics is None``):
    Searches toward ``quality_worse`` until a result scores *worse* than the
    current best, at which point that result becomes the fail sentinel and the
    algorithm transitions to 3-point mode.

    **Phase 2 — 3-point mode** (both sentinels have real metrics):
    Picks the midpoint of the larger range each iteration.  A new best
    promotes/demotes the surrounding sentinels; a non-best tightens the
    appropriate sentinel.  Terminates when both ranges collapse to ≤ granularity.

    Direction-agnostic: works for CRF (lower=better) and VBR (higher=better)
    by using ``abs()`` for range sizes and ``min/max`` for boundary checks.

    Args:
        quality_better:   Better-quality boundary (codec range start).
        quality_worse:    Worse-quality boundary (codec range end).
        quality_targets:  Quality targets to meet.
        granularity:      Step size as a ``Decimal``.
        quality_max_step: Optional maximum absolute step size per iteration.

    Raises:
        ValueError: If ``quality_better == quality_worse`` or ``granularity <= 0``.
    """

    def __init__(
        self,
        quality_better:   Decimal,
        quality_worse:    Decimal,
        quality_targets:  list[QualityTarget],
        granularity:      Decimal,
        quality_max_step: Decimal | None = None,
    ) -> None:
        if quality_better == quality_worse:
            raise ValueError(
                f"quality_better ({quality_better}) must differ from quality_worse ({quality_worse})"
            )
        if granularity <= 0:
            raise ValueError(f"granularity must be > 0, got {granularity}")

        self._quality_targets:  list[QualityTarget]  = quality_targets
        self._granularity:      Decimal              = granularity
        self._quality_max_step: Decimal | None       = quality_max_step
        self._best_score_point: QualityPoint | None  = None

        # Inclusive limits
        self._upper : QualityPoint = QualityPoint(quality_better, 0, None)
        self._lower : QualityPoint = QualityPoint(quality_worse, 0, None)

        # Real attempts made
        self._attempted_points : dict[Decimal, QualityPoint] = {}


    # ------------------------------------------------------------------
    # Protocol properties
    # ------------------------------------------------------------------

    @property
    def attempts(self) -> int:
        """Total number of ``record()`` calls made so far."""
        return len(self._attempted_points)

    @property
    def best_quality(self) -> Decimal | None:
        """Best quality found: best-efficiency passing value if any pass, else best-fail value."""
        if self._best_score_point:
            return self._best_score_point.q
        return None

    @property
    def best_metrics(self) -> dict[str, float] | None:
        """Metrics dict associated with ``best_quality``."""
        if self._best_score_point:
            return self._best_score_point.metrics
        return None

    @property
    def best_targets_met(self) -> bool:
        """``True`` if at least one passing attempt was recorded."""
        return self._best_score_point is not None and self._best_score_point.score>=0

    # ------------------------------------------------------------------
    # record()
    # ------------------------------------------------------------------

    def record(self, quality: Decimal, quality_results: dict[str, float]) -> Decimal | None:
        """Record one attempt and return the next quality value to try.

        Implements the 3-point sweet-spot state machine.  Returns ``None``
        when the search is exhausted or the current result is accepted as final.

        Args:
            quality:         Quality value used for this attempt.
            quality_results: Measured quality metrics keyed as ``"<metric>_<stat>"``.

        Returns:
            Next quality value (quantized ``Decimal``), or ``None`` when exhausted.
        """
        # Score the attempt — catch missing-key errors and treat as worst fail.
        try:
            new_point = QualityPoint(quality, _score_attempt(quality_results, self._quality_targets), quality_results)
        except ValueError:
            raise ValueError("QualitySearchV2: missing metric key for quality=%s" % quality)

        # Save the new point
        self._attempted_points[quality] = new_point

        # Do we have a winner?
        if new_point.is_winner:
            self._best_score_point = new_point
            return None
        # Update best score point if:
        # - no best score point is present yet
        # - if we have better pass score (lower positive value - closer to 0)
        # - if we have better fail score (higher negative value - closer to 0)
        if not self._best_score_point or \
            (new_point.is_pass and self._best_score_point.is_fail) or \
            (new_point.is_pass and new_point.score < self._best_score_point.score) or \
            (new_point.is_fail and new_point.score > self._best_score_point.score):
                self._best_score_point = new_point

        # On the first attempt - we won't have 2 points, so shortcut is to go true binary into direction of pass/fail of new point
        if self.attempts == 1:
            if new_point.is_pass:
                # find new attempt placement between current result and the WORSE result
                return self._compute_next(new_point, self._lower, new_point)
            # find new attempt placement between current result and the BETTER result
            return self._compute_next(new_point, new_point, self._upper)

        #! NO `NEW_POINT` BELOW THIS POINT, only the best point and its adjacent points

        # Get quality values sorted so that higher quality comes first. Actual q values could be reversed, depends on codec configuration / quality units
        sorted_q = sorted(self._attempted_points.keys(), reverse=self._upper.q > self._lower.q)

        # Check for the dumbest case - there's a passing attempt and a failing attempt - just narrow them down.
        first_failing_q = next((q for q in sorted_q if self._attempted_points[q].is_fail), None)
        last_passing_q = next((q for q in reversed(sorted_q) if self._attempted_points[q].is_pass), None)
        if first_failing_q is not None and last_passing_q is not None:
            return self._compute_next(self._attempted_points[first_failing_q], self._attempted_points[first_failing_q], self._attempted_points[last_passing_q])



        # Find adjacent points to the best scoring point. Best scoring point is always present, regardless of if there were passing attempts
        best_p = self._best_score_point
        best_q_index = sorted_q.index(best_p.q)

        # HERE: still doing outwards search - go in true binary steps between bounds and last attempt
        # ATTENTION: Proportional search could work here, but it often looses sweet-point curve shape, thus never reaching 3-point mode. Keep it at binary.
        if best_p.is_fail and best_q_index == 0:
            # we are still searching into higher-quality direction (i.e. not exhausted). Continue with a safeguard against endless upper-bound (sentinel) reuse
            return self._compute_next(best_p, best_p, self._attempted_points.get(self._upper.q, self._upper))
        if best_p.is_pass and best_q_index == len(sorted_q) - 1:
            # we are still searching into lower-quality direction (i.e. not exhausted). Continue with a safeguard against endless lower-bound (sentinel) reuse
            return self._compute_next(best_p, self._attempted_points.get(self._lower.q, self._lower), best_p)

        # HERE: we've exhausted outwards search, but didn't reach 3-point yet - proportional search between 2 points.
        # Shouldn't be triggered ever (dumb-preliminary 2-point search already did it).
        if best_p.is_pass and best_q_index == 0:
            return self._compute_next(best_p, self._attempted_points[sorted_q[1]], best_p)
        if best_p.is_fail and best_q_index == len(sorted_q) - 1:
            return self._compute_next(best_p, best_p, self._attempted_points[sorted_q[-2]])

        # HERE: make 3-point decision
        # Algorithm:
        # - if there's a pass/fail available - pick this range and do proportional search next
        # - if there all points are a fail or a pass - search for sweet spot - take larger range and do binary steps next
        adjacent_points = [self._attempted_points[q] for q in sorted_q[best_q_index-1:best_q_index+2]]
        any_adjacent_pass = any(p.is_pass for p in adjacent_points)
        any_adjacent_fail = any(p.is_fail for p in adjacent_points)
        if any_adjacent_pass and any_adjacent_fail:
            # we do have both pass and failing points in our 3 point range. Pick the range (current_point ... either -1 or +1)
            # first go towards lower quality in this case - to reduce the size of resulting video
            if adjacent_points[0].is_fail != adjacent_points[1].is_fail:
                return self._compute_next(best_p, adjacent_points[1], adjacent_points[0])
            # if not - go towards higher quality in this case - to increase the size of resulting video
            return self._compute_next(best_p, adjacent_points[2], adjacent_points[1])

        # HERE: we have 3 points, all are passing or all are failing. We need to search for sweet spot.
        # Problem - the curve isn't monotonic (otherwise we would've been at 2-point decision point)
        # The only thing we could do here is true binary search of both adjanced intervals. So just take the larger one
        range_1_len = adjacent_points[1].q - adjacent_points[0].q
        range_2_len = adjacent_points[2].q - adjacent_points[1].q
        if abs(range_1_len) > abs(range_2_len):
            return self._compute_next(best_p, adjacent_points[1], adjacent_points[0])
        return self._compute_next(best_p, adjacent_points[2], adjacent_points[1])


    # ------------------------------------------------------------------
    # Next quality computation
    # ------------------------------------------------------------------

    def _compute_next(self, new_point: QualityPoint, worse_point: QualityPoint, better_point: QualityPoint) -> Decimal | None:
        """Compute the next quality value to try, or ``None`` if exhausted."""
        # HACK: Use the same logic as the legacy algorithm. Hackish, fragile.
        # TODO: Make standalone reusable function
        return QualitySearch._compute_next(self, new_point, worse_point, better_point)
