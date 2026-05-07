"""
Quality evaluation and CRF adjustment for encoding pipeline.

This module provides quality evaluation against targets and CRF adjustment
algorithms for iterative encoding optimization.
"""
# CHerSun 2026

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import (
    Iterable,
    TypedDict,
    TypeVar,
    assert_never,
)

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
                           When all targets pass and the least-proficit target's surplus
                           (smallest surplus across all passing targets) is within this
                           delta, the current quality value is accepted as final without
                           further search — saves an extra encoding pass when the binding
                           constraint is already close enough.  Values are in normalized
                           metric units (post-scale_factor).
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
        comparison_range  = 15.0,  # practical target range ~85-100
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
        comparison_range  = 5.0,   # practical target range ~95-100
        acceptance_delta  = 0.05,  # 0.05% after scaling (0.0005 raw). Metric is quite compressed towards 100, so we have to use a low value.
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
        lossless_raw_repr = "inf dB",
        display_unit      = " dB",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        comparison_range  = 20.0,  # practical target range ~40-60 dB
        acceptance_delta  = 0.3,   # 0.3 dB surplus should be negligible
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
        comparison_range  = 20.0,  # Practical target range being ~80-100
        acceptance_delta  = 0.15,  # 0.15 after scaling should be good. VIF scales nicely with ~92 being quite good.
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

    metrics:        ChunkQualityStats
    targets_met:    bool
    failed_targets: list[QualityTarget]
    artifacts:      QualityArtifacts


# ---------------------------------------------------------------------------
# Scoring helpers (module-level, kept for backward compatibility with tests)
# ---------------------------------------------------------------------------

def _score_attempt(
    metrics:         dict[str, float],
    quality_targets: list[QualityTarget],
) -> float:
    """Compute a signed composite score for an encoding attempt.

    Metrics must be already normalized (post ``MetricInfo.normalize``), as stored
    in sidecars.  No normalization is performed here.

    Returns a float encoding both pass/fail and distance from the sweet spot:
    - ``0.0``: all targets pass and the least-proficit target's surplus <= its
      ``acceptance_delta`` (early acceptance).  Only the tightest constraint is
      checked — a large surplus on other metrics does not block early exit.
    - Positive: all targets pass, but the least-proficit surplus > its
      ``acceptance_delta``.  Value is ``sum(surplus / comparison_range)`` for
      all targets.
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

    fail_score        = 0.0
    pass_score        = 0.0
    any_fail          = False
    min_surplus       = float("inf")   # smallest surplus across all passing targets
    min_surplus_delta = 0.0            # acceptance_delta for the least-proficit target

    for target in quality_targets:
        key    = f"{target.metric}_{target.statistic}"
        actual = metrics[key]
        info   = MetricType(target.metric).info

        deficit    = info.deficit(actual, target.value)
        normalized = deficit / info.comparison_range

        logger.debug(
            "_score_attempt: %s_%s actual=%.4f target=%.4f deficit=%.4f normalized=%.6f",
            target.metric, target.statistic, actual, target.value, deficit, normalized,
        )

        if deficit < 0.0:
            fail_score += normalized
            any_fail    = True
        else:
            pass_score += normalized
            if deficit < min_surplus:
                min_surplus       = deficit
                min_surplus_delta = info.acceptance_delta

    # Early acceptance: all targets pass AND the least-proficit metric's surplus
    # is within its acceptance_delta — no further search can meaningfully improve.
    all_within_delta = (not any_fail) and (min_surplus <= min_surplus_delta)

    return fail_score or (0.0 if all_within_delta else pass_score)


# ---------------------------------------------------------------------------
# QualityPoint and range helpers
# ---------------------------------------------------------------------------

@dataclass
class QualityPoint:
    """A single recorded quality attempt with its score and metrics.

    Attributes:
        q:       Quality value of this point.
        score:   Composite score. 0 = winner (targets met within delta) or sentinel.
                 Positive = pass (targets met, surplus > delta). Negative = fail.
        metrics: Metrics dict, or ``None`` for sentinel points.
    """

    q:       Decimal
    score:   float
    metrics: dict[str, float] | None

    @property
    def is_sentinel(self) -> bool:
        """``True`` when this point represents an untested boundary."""
        return self.metrics is None

    @property
    def is_pass(self) -> bool:
        """``True`` when this point passed all targets (score >= 0, not sentinel)."""
        return self.score >= 0 and not self.is_sentinel

    @property
    def is_fail(self) -> bool:
        """``True`` when this point failed at least one target (score < 0, not sentinel)."""
        return self.score < 0 and not self.is_sentinel

    @property
    def is_winner(self) -> bool:
        """``True`` when this point is an early-acceptance winner (score == 0, not sentinel)."""
        return self.score == 0 and not self.is_sentinel


T_numeric = TypeVar("T_numeric", int, float, Decimal)


def _in_range(value: T_numeric, start: T_numeric, end: T_numeric) -> bool:
    """Check if a value belongs to a given range, handling inverted ranges too."""
    low  = min(start, end)
    high = max(start, end)
    return low <= value <= high


def _clamp_to_range(
    q:            Decimal,
    granularity:  Decimal,
    worse_point:  QualityPoint,
    better_point: QualityPoint,
) -> Decimal | None:
    """Clamp *q* to the given range. Respects granularity and sentinels.

    Returns ``None`` when no interior point exists (range collapsed).
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


def _compute_proportional_candidate(
    target:      QualityTarget | None,
    pass_point:  QualityPoint,
    fail_point:  QualityPoint,
    clamp_range: bool = True,
) -> float | None:
    """Return interpolation fraction t for *target* metric value, or ``None`` for fallback.

    When ``clamp_range=True``, returns ``None`` if ``t`` is outside ``[0, 1]``.
    When ``clamp_range=False``, returns ``t`` as-is (allows extrapolation).
    """
    # No metrics? Fallback via None for other options - externally managed.
    if target is None or pass_point.is_sentinel or fail_point.is_sentinel:
        return None

    # Get actual metric values.
    key      = f"{target.metric}_{target.statistic}"
    pass_val = pass_point.metrics.get(key)  # type: ignore[union-attr]
    fail_val = fail_point.metrics.get(key)  # type: ignore[union-attr]
    if pass_val is None or fail_val is None:
        logger.warning(
            "QualitySearch: missing metric %s for pass=%s fail=%s.",
            key, pass_point.q, fail_point.q,
        )
        return None

    # Compute interpolation fraction.
    info   = MetricType(target.metric).info
    d_pass = info.deficit(pass_val, target.value)
    d_fail = info.deficit(fail_val, target.value)
    span   = d_pass - d_fail
    if abs(span) < 1e-9:  # Avoid division by zero
        return None
    t = d_pass / span

    return t if not clamp_range or 0.0 <= t <= 1.0 else None


# ---------------------------------------------------------------------------
# QualitySearchBase — abstract base class for all quality search implementations
# ---------------------------------------------------------------------------

class QualitySearchBase(ABC):
    """Abstract base class for all quality search implementations.

    Owns the shared constructor (with validation), config fields, exhaustion flag,
    and all protected helper methods used by subclasses.

    Constructor raises ``ValueError`` only if ``granularity <= 0``.
    ``quality_better == quality_worse`` is valid and results in a single-point
    search: the first ``record()`` call records the result and returns ``None``.

    Args:
        quality_better:   Better-quality boundary (codec range start, e.g. CRF 0).
        quality_worse:    Worse-quality boundary (codec range end, e.g. CRF 51).
        quality_targets:  Quality targets to meet.
        granularity:      Minimum step size as a ``Decimal``.  Must be > 0.
        quality_max_step: Optional maximum absolute step size per ``record()`` call.

    Raises:
        ValueError: If ``granularity <= 0``.
    """

    def __init__(
        self,
        quality_better:   Decimal,
        quality_worse:    Decimal,
        quality_targets:  list[QualityTarget],
        granularity:      Decimal,
        quality_max_step: Decimal | None = None,
    ) -> None:
        if granularity <= 0:
            raise ValueError(f"granularity must be > 0, got {granularity}")

        self._quality_targets:  list[QualityTarget] = quality_targets
        self._granularity:      Decimal             = granularity
        self._quality_max_step: Decimal | None      = quality_max_step
        self._quality_better:   Decimal             = quality_better
        self._quality_worse:    Decimal             = quality_worse
        self._exhausted:        bool                = False

    # ------------------------------------------------------------------
    # Abstract contract (same as the former QualitySearchProtocol)
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def attempts(self) -> int:
        """Total number of ``record()`` calls made so far."""
        ...

    @property
    @abstractmethod
    def best_quality(self) -> Decimal | None:
        """Best quality value found so far.

        Best-efficiency passing value if any pass exists, otherwise the
        best-fail value (highest score, i.e. closest to zero).
        ``None`` before any ``record()`` call.
        """
        ...

    @property
    @abstractmethod
    def best_metrics(self) -> dict[str, float] | None:
        """Full metrics dict associated with ``best_quality``.
        ``None`` before any ``record()`` call.
        """
        ...

    @property
    @abstractmethod
    def best_targets_met(self) -> bool:
        """``True`` iff ``best_quality`` corresponds to a passing attempt."""
        ...

    @abstractmethod
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

    # ------------------------------------------------------------------
    # Protected helpers — available to all subclasses
    # ------------------------------------------------------------------

    def _score(self, metrics: dict[str, float]) -> float:
        """Compute a signed composite score for an attempt.

        Delegates to the module-level ``_score_attempt`` with ``self._quality_targets``.

        Args:
            metrics: Measured quality metrics keyed as ``"<metric>_<stat>"``.

        Returns:
            Signed float score (0 = winner, positive = pass, negative = fail).

        Raises:
            ValueError: If any target key is absent from ``metrics``.
        """
        return _score_attempt(metrics, self._quality_targets)

    def _find_worst_target(
        self,
        metrics: dict[str, float],
    ) -> tuple[QualityTarget, float, float] | None:
        """Find the worst-performing target and return it with its deficit and actual value.

        "Worst" means the smallest ``MetricInfo.deficit(actual, target)`` — i.e. the
        target with the least surplus (or largest deficit).

        Args:
            metrics: Measured quality metrics keyed as ``"<metric>_<stat>"``.

        Returns:
            ``(worst_target, worst_deficit, worst_actual)`` or ``None`` when no
            target has a valid result in *metrics*.
        """
        worst_deficit: float               = float("inf")
        worst_target:  QualityTarget | None = None
        worst_actual:  float               = 0.0

        for target in self._quality_targets:
            metric_key = f"{target.metric}_{target.statistic}"
            actual     = metrics.get(metric_key)
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

    def _next_or_exhaust(
        self,
        next_q:    Decimal | None,
        attempted: dict[Decimal, QualityPoint],
    ) -> Decimal | None:
        """Return *next_q* if valid and not already attempted; exhaust otherwise.

        Sets ``self._exhausted = True`` and returns ``None`` when *next_q* is
        ``None`` or already present in *attempted*.

        Args:
            next_q:    Candidate next quality value, or ``None``.
            attempted: Dict of already-recorded quality values.

        Returns:
            *next_q* if usable, otherwise ``None``.
        """
        if next_q is None or next_q in attempted:
            self._exhausted = True
            return None
        return next_q

    def _finalize_q(
        self,
        raw_q:        Decimal,
        from_q:       Decimal,
        worse_point:  QualityPoint,
        better_point: QualityPoint,
    ) -> Decimal | None:
        """Apply the full post-processing pipeline to a raw candidate quality value.

        Pipeline (in order):
        1. Max-step clamp: if ``self._quality_max_step`` is set, clamp *raw_q* to
           within ``+/-quality_max_step`` of *from_q*.
        2. Granularity snap: round to nearest granularity step using ``ROUND_HALF_EVEN``.
        3. Range clamp: call ``_clamp_to_range`` — sentinel-aware, returns ``None``
           if no valid interior point exists.

        Does **not** set ``self._exhausted`` — the caller is responsible.

        Args:
            raw_q:        Raw candidate quality value before post-processing.
            from_q:       Reference quality value for max-step clamping.
            worse_point:  Worse-quality boundary point (may be sentinel).
            better_point: Better-quality boundary point (may be sentinel).

        Returns:
            Finalized ``Decimal``, or ``None`` if the range is exhausted.
        """
        # Step 1: max-step clamp
        if self._quality_max_step is not None:
            step = raw_q - from_q
            if abs(step) > self._quality_max_step:
                raw_q = from_q + (self._quality_max_step if step > 0 else -self._quality_max_step)

        # Step 2: granularity snap
        snapped = (raw_q / self._granularity).to_integral_value(ROUND_HALF_EVEN) * self._granularity
        snapped = snapped.quantize(self._granularity)

        # Step 3: range clamp
        return _clamp_to_range(snapped, self._granularity, worse_point, better_point)

    def _compute_next_quality(
        self,
        new_point:    QualityPoint,
        worse_point:  QualityPoint,
        better_point: QualityPoint,
    ) -> Decimal | None:
        """Compute the next quality value via proportional estimation.

        Tries candidates in order:
        1. Primary proportional: worst target from *new_point* metrics.
        2. Reverse proportional: worst target from the opposite boundary metrics.
        3. Binary midpoint (t = 0.5).

        Always calls ``_compute_proportional_candidate`` without range clamping
        (``clamp_range=False``), so ``t`` outside ``[0, 1]`` is allowed.
        When points are on opposite sides the projection naturally lands between
        them; when same-side it extrapolates outward.

        Calls ``_finalize_q(raw_q, new_point.q, worse_point, better_point)`` for
        the final value.  Returns ``None`` when ``_finalize_q`` returns ``None``
        (range exhausted); the caller is responsible for setting ``self._exhausted``.

        Args:
            new_point:    The most recently recorded point.
            worse_point:  Worse-quality boundary (may be sentinel or tested point).
            better_point: Better-quality boundary (may be sentinel or tested point).

        Returns:
            Next quality value, or ``None`` if the range is exhausted.
        """
        q_span = worse_point.q - better_point.q

        # Determine worst target from current result and from opposite boundary.
        opposite_metrics = worse_point.metrics if new_point.is_pass else better_point.metrics
        opp_worst        = self._find_worst_target(opposite_metrics) if opposite_metrics else None
        opp_target       = opp_worst[0] if opp_worst is not None else None

        found_worst  = self._find_worst_target(new_point.metrics)  # type: ignore[arg-type]
        worst_target = found_worst[0] if found_worst is not None else None

        # Build candidates list: primary proportional, reverse proportional, binary.
        candidates: list[tuple[float | None, str]] = []
        if worst_target is not None:
            candidates.append((
                _compute_proportional_candidate(worst_target, better_point, worse_point, clamp_range=False),
                f"primary worst={worst_target.metric}_{worst_target.statistic}",
            ))
        if opp_target is not None:
            candidates.append((
                _compute_proportional_candidate(opp_target, better_point, worse_point, clamp_range=False),
                f"reverse worst={opp_target.metric}_{opp_target.statistic}",
            ))
        candidates.append((0.5, "binary"))

        # Pick first non-None candidate.
        t_chosen, label = next((c for c in candidates if c[0] is not None), (0.5, "binary-fallback"))
        raw_q = better_point.q + Decimal(str(t_chosen)) * q_span

        result = self._finalize_q(raw_q, new_point.q, worse_point, better_point)

        _pad = len(str(max(abs(better_point.q), abs(worse_point.q)).quantize(self._granularity)))
        logger.debug(
            "_compute_next_quality [%s]: better=%s worse=%s t=%.3f raw=%s -> %s",
            label,
            str(better_point.q).rjust(_pad),
            str(worse_point.q).rjust(_pad),
            t_chosen,
            str(raw_q.quantize(self._granularity)).rjust(_pad),
            str(result).rjust(_pad) if result is not None else "None (exhausted)",
        )

        return result


# ---------------------------------------------------------------------------
# QualitySearch — legacy proportional interpolation algorithm
# ---------------------------------------------------------------------------

class QualitySearch(QualitySearchBase):
    """Quality search using proportional interpolation (legacy algorithm).

    Encapsulates the binary-bracket search previously split across
    ``CRFHistory`` and ``adjust_crf()``.  Direction-agnostic: uses
    ``quality_better``/``quality_worse`` instead of assuming lower=better.

    When ``quality_better == quality_worse``, the first ``record()`` call
    records the result and returns ``None`` (single fixed quality value).

    Args:
        quality_better:   Better-quality boundary (codec range start).
        quality_worse:    Worse-quality boundary (codec range end).
        quality_targets:  Quality targets to meet.
        granularity:      Step size as a ``Decimal``.
        quality_max_step: Optional maximum absolute step size per iteration.

    Raises:
        ValueError: If ``granularity <= 0``.
    """

    def __init__(
        self,
        quality_better:   Decimal,
        quality_worse:    Decimal,
        quality_targets:  list[QualityTarget],
        granularity:      Decimal,
        quality_max_step: Decimal | None = None,
    ) -> None:
        super().__init__(quality_better, quality_worse, quality_targets, granularity, quality_max_step)

        self._attempts: int = 0

        # Higher-quality point (lower CRF, higher VBR)
        self._better_point = QualityPoint(quality_better, 0, None)

        # Lower-quality point (higher CRF, lower VBR)
        self._worse_point = QualityPoint(quality_worse, 0, None)

        # Best attempt regardless of pass/fail (for best_quality fallback)
        self._best_point: QualityPoint | None = None

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
        if not self._better_point.is_sentinel:
            return self._better_point.q
        if self._best_point is not None:
            return self._best_point.q
        return None

    @property
    def best_metrics(self) -> dict[str, float] | None:
        """Metrics dict associated with ``best_quality``."""
        if not self._better_point.is_sentinel:
            return self._better_point.metrics
        if self._best_point is not None:
            return self._best_point.metrics
        return None

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

        try:
            new_point = QualityPoint(quality, self._score(quality_results), quality_results)
        except ValueError:
            raise ValueError("QualitySearch: missing metric key for quality=%s" % quality)

        # Early acceptance: score == 0 means targets met within acceptance_delta.
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
            # Track best-fail for best_quality fallback
            if self._best_point is None or new_point.score > self._best_point.score:
                self._best_point = new_point

        # Exhaustion check 1: bracket collapsed to <= granularity (normal case).
        if (
            not (self._better_point.is_sentinel or self._worse_point.is_sentinel)
            and abs(self._better_point.q - self._worse_point.q) <= self._granularity
        ):
            self._exhausted = True
            return None

        # Exhaustion check 2: all-fail — fail bracket reached quality_better boundary.
        if (
            self._better_point.is_sentinel
            and not self._worse_point.is_sentinel
            and self._worse_point.q == self._better_point.q
        ):
            self._exhausted = True
            return None

        # Exhaustion check 3: all-pass — pass bracket reached quality_worse boundary.
        if (
            self._worse_point.is_sentinel
            and not self._better_point.is_sentinel
            and self._better_point.q == self._worse_point.q
        ):
            self._exhausted = True
            return None

        result = self._compute_next_quality(new_point, self._worse_point, self._better_point)
        if result is None:
            self._exhausted = True
        return result


# ---------------------------------------------------------------------------
# QualitySearchV2 — 3-point sweet-spot algorithm
# ---------------------------------------------------------------------------

class QualitySearchV2(QualitySearchBase):
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
    appropriate sentinel.  Terminates when both ranges collapse to <= granularity.

    Direction-agnostic: works for CRF (lower=better) and VBR (higher=better)
    by using ``abs()`` for range sizes and ``min/max`` for boundary checks.

    When ``quality_better == quality_worse``, the first ``record()`` call
    records the result and returns ``None`` (single fixed quality value).

    Args:
        quality_better:   Better-quality boundary (codec range start).
        quality_worse:    Worse-quality boundary (codec range end).
        quality_targets:  Quality targets to meet.
        granularity:      Step size as a ``Decimal``.
        quality_max_step: Optional maximum absolute step size per iteration.

    Raises:
        ValueError: If ``granularity <= 0``.
    """

    def __init__(
        self,
        quality_better:   Decimal,
        quality_worse:    Decimal,
        quality_targets:  list[QualityTarget],
        granularity:      Decimal,
        quality_max_step: Decimal | None = None,
    ) -> None:
        super().__init__(quality_better, quality_worse, quality_targets, granularity, quality_max_step)

        self._best_score_point: QualityPoint | None = None

        # Inclusive limits (sentinels)
        self._upper: QualityPoint = QualityPoint(quality_better, 0, None)
        self._lower: QualityPoint = QualityPoint(quality_worse,  0, None)

        # Real attempts made
        self._attempted_points: dict[Decimal, QualityPoint] = {}

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
        return self._best_score_point is not None and self._best_score_point.score >= 0

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
        try:
            new_point = QualityPoint(quality, self._score(quality_results), quality_results)
        except ValueError:
            raise ValueError("QualitySearchV2: missing metric key for quality=%s" % quality)

        if self._exhausted:
            return None

        # Save the new point
        self._attempted_points[quality] = new_point

        # Winner: early acceptance
        if new_point.is_winner:
            self._best_score_point = new_point
            self._exhausted        = True
            return None

        # Update best score point:
        # - no best yet
        # - new pass beats any fail
        # - new pass with lower score (closer to 0) beats current pass
        # - new fail with higher score (closer to 0) beats current fail
        if (
            not self._best_score_point
            or (new_point.is_pass and self._best_score_point.is_fail)
            or (new_point.is_pass and new_point.score < self._best_score_point.score)
            or (new_point.is_fail and new_point.score > self._best_score_point.score)
        ):
            self._best_score_point = new_point

        # On the first attempt: go binary toward the relevant boundary
        if self.attempts == 1:
            if new_point.is_pass:
                return self._next_or_exhaust(
                    self._compute_next_quality(new_point, self._lower, new_point),
                    self._attempted_points,
                )
            return self._next_or_exhaust(
                self._compute_next_quality(new_point, new_point, self._upper),
                self._attempted_points,
            )

        #! NO `NEW_POINT` BELOW THIS POINT — only the best point and its adjacent points

        # Sort quality values so that higher quality comes first.
        sorted_q = sorted(
            self._attempted_points.keys(),
            reverse=self._upper.q > self._lower.q,
        )

        # Simplest case: there's a passing attempt and a failing attempt — narrow them down.
        first_failing_q = next((q for q in sorted_q if self._attempted_points[q].is_fail), None)
        last_passing_q  = next((q for q in reversed(sorted_q) if self._attempted_points[q].is_pass), None)
        if first_failing_q is not None and last_passing_q is not None:
            fail_p = self._attempted_points[first_failing_q]
            pass_p = self._attempted_points[last_passing_q]
            return self._next_or_exhaust(
                self._compute_next_quality(fail_p, fail_p, pass_p),
                self._attempted_points,
            )

        # Find adjacent points to the best scoring point.
        best_p       = self._best_score_point
        best_q_index = sorted_q.index(best_p.q)  # type: ignore[union-attr]

        # Still doing outward search — go in binary steps between bounds and last attempt.
        # ATTENTION: Proportional search could work here, but it often loses sweet-point
        # curve shape, thus never reaching 3-point mode. Keep it at binary.
        if best_p.is_fail and best_q_index == 0:
            return self._next_or_exhaust(
                self._compute_next_quality(
                    best_p, best_p,
                    self._attempted_points.get(self._upper.q, self._upper),
                ),
                self._attempted_points,
            )
        if best_p.is_pass and best_q_index == len(sorted_q) - 1:
            return self._next_or_exhaust(
                self._compute_next_quality(
                    best_p,
                    self._attempted_points.get(self._lower.q, self._lower),
                    best_p,
                ),
                self._attempted_points,
            )

        # Outward search exhausted but not yet 3-point — proportional between 2 points.
        # Shouldn't be triggered often (the 2-point check above usually handles it).
        if best_p.is_pass and best_q_index == 0:
            return self._next_or_exhaust(
                self._compute_next_quality(best_p, self._attempted_points[sorted_q[1]], best_p),
                self._attempted_points,
            )
        if best_p.is_fail and best_q_index == len(sorted_q) - 1:
            return self._next_or_exhaust(
                self._compute_next_quality(best_p, best_p, self._attempted_points[sorted_q[-2]]),
                self._attempted_points,
            )

        # 3-point decision:
        # - if there's a pass/fail pair — pick that range and do proportional search
        # - if all same side — sweet-spot search: take larger range and do binary steps
        adjacent_points   = [self._attempted_points[q] for q in sorted_q[best_q_index - 1:best_q_index + 2]]
        any_adjacent_pass = any(p.is_pass for p in adjacent_points)
        any_adjacent_fail = any(p.is_fail for p in adjacent_points)

        if any_adjacent_pass and any_adjacent_fail:
            # Prefer lower-quality range to reduce output file size
            if adjacent_points[0].is_fail != adjacent_points[1].is_fail:
                return self._next_or_exhaust(
                    self._compute_next_quality(best_p, adjacent_points[1], adjacent_points[0]),
                    self._attempted_points,
                )
            return self._next_or_exhaust(
                self._compute_next_quality(best_p, adjacent_points[2], adjacent_points[1]),
                self._attempted_points,
            )

        # All 3 points same side — binary sweet-spot search on the larger sub-range
        range_1_len = adjacent_points[1].q - adjacent_points[0].q
        range_2_len = adjacent_points[2].q - adjacent_points[1].q
        if abs(range_1_len) > abs(range_2_len):
            return self._next_or_exhaust(
                self._compute_next_quality(best_p, adjacent_points[1], adjacent_points[0]),
                self._attempted_points,
            )
        return self._next_or_exhaust(
            self._compute_next_quality(best_p, adjacent_points[2], adjacent_points[1]),
            self._attempted_points,
        )



# ---------------------------------------------------------------------------
# QualitySearchV3 — linear extrapolation with midpoint-probe safety net
# ---------------------------------------------------------------------------

class QualitySearchV3(QualitySearchBase):
    """Quality search using linear extrapolation and a midpoint-probe safety net.

    Replaces V2's binary half-steps toward the codec boundary (when both known
    points are on the same side) with **linear extrapolation**: given two
    same-side points, it projects where the quality curve would cross zero and
    jumps directly there.  On a monotonic linear curve this finds the crossing
    in O(1) steps instead of O(log N).

    A **midpoint-probe safety net** is added for the edge case where the outward
    direction is exhausted (the boundary has actually been tested and both points
    are still on the same side): one midpoint probe is inserted before declaring
    full exhaustion, catching non-monotonic curves that V2 would miss.

    V3 is a drop-in replacement for V2 — same ``QualitySearchBase`` interface,
    same constructor signature.

    When ``quality_better == quality_worse``, the first ``record()`` call
    records the result and returns ``None`` (single fixed quality value).

    Args:
        quality_better:   Better-quality boundary (codec range start, e.g. CRF 0).
        quality_worse:    Worse-quality boundary (codec range end, e.g. CRF 51).
        quality_targets:  Quality targets to meet.
        granularity:      Minimum step size as a ``Decimal``.  Must be > 0.
        quality_max_step: Optional maximum absolute step size per ``record()`` call.

    Raises:
        ValueError: If ``granularity <= 0``.
    """

    def __init__(
        self,
        quality_better:   Decimal,
        quality_worse:    Decimal,
        quality_targets:  list[QualityTarget],
        granularity:      Decimal,
        quality_max_step: Decimal | None = None,
    ) -> None:
        super().__init__(quality_better, quality_worse, quality_targets, granularity, quality_max_step)

        self._attempted_points:    dict[Decimal, QualityPoint] = {}
        self._best_score_point:    QualityPoint | None         = None
        self._midpoint_probe_flag: bool                        = False

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
        if self._best_score_point is not None:
            return self._best_score_point.q
        return None

    @property
    def best_metrics(self) -> dict[str, float] | None:
        """Metrics dict associated with ``best_quality``."""
        if self._best_score_point is not None:
            return self._best_score_point.metrics
        return None

    @property
    def best_targets_met(self) -> bool:
        """``True`` if at least one passing attempt was recorded."""
        return self._best_score_point is not None and self._best_score_point.score >= 0

    # ------------------------------------------------------------------
    # record()
    # ------------------------------------------------------------------

    def record(self, quality: Decimal, quality_results: dict[str, float]) -> Decimal | None:
        """Record one attempt and return the next quality value to try.

        Implements the linear-extrapolation state machine with midpoint-probe
        safety net.  Returns ``None`` when the search is exhausted or the
        current result is accepted as final.

        Args:
            quality:         Quality value used for this attempt.
            quality_results: Measured quality metrics keyed as ``"<metric>_<stat>"``.

        Returns:
            Next quality value (quantized ``Decimal``), or ``None`` when exhausted.
        """
        if self._exhausted:
            return None

        try:
            new_point = QualityPoint(quality, self._score(quality_results), quality_results)
        except ValueError:
            raise ValueError("QualitySearchV3: missing metric key for quality=%s" % quality)

        # Record the attempt
        self._attempted_points[quality] = new_point

        # Winner: early acceptance
        if new_point.is_winner:
            self._best_score_point = new_point
            self._exhausted        = True
            logger.debug("QualitySearchV3: early acceptance at quality=%s", quality)
            return None

        # Update best-scoring point (pass precedence over fail)
        prev_best = self._best_score_point
        if (
            self._best_score_point is None
            or (new_point.is_pass and self._best_score_point.is_fail)
            or (new_point.is_pass and new_point.score < self._best_score_point.score)
            or (new_point.is_fail and self._best_score_point.is_fail and new_point.score > self._best_score_point.score)
        ):
            self._best_score_point = new_point

        # Reset midpoint-probe flag when best-scoring point changes
        if self._best_score_point is not prev_best:
            self._midpoint_probe_flag = False

        # Phase 0: first attempt — half-range step toward the relevant boundary
        if self.attempts == 1:
            return self._phase0_first_attempt(new_point)

        # Phase 1+: select best + neighbours and dispatch to the appropriate fork
        return self._phase_select_and_dispatch(new_point)

    # ------------------------------------------------------------------
    # Phase 0: first attempt
    # ------------------------------------------------------------------

    def _phase0_first_attempt(self, new_point: QualityPoint) -> Decimal | None:
        """Compute the next quality after the very first recorded attempt.

        Steps half the distance from the recorded quality toward the relevant
        boundary (``quality_worse`` for a pass, ``quality_better`` for a fail),
        clamped by ``quality_max_step`` and snapped to granularity.

        The boundary on the *from* side is the recorded point itself (non-sentinel),
        so ``_clamp_to_range`` excludes it as an already-tested value.  The boundary
        on the *outward* side is the sentinel (untested), so the sentinel value is
        included as a valid candidate.  When both boundaries collapse to the same
        value (``quality_better == quality_worse``), ``_next_or_exhaust`` catches
        the collision and declares exhaustion.

        Args:
            new_point: The first recorded attempt.

        Returns:
            Next quality value, or ``None`` if the range is already exhausted.
        """
        if new_point.is_pass:
            # Step toward quality_worse (lower quality).
            # worse side = sentinel (open), better side = new_point (tested, closed).
            worse_pt  = QualityPoint(self._quality_worse,  0, None)  # sentinel
            better_pt = new_point                                      # tested — excluded by _clamp_to_range
            boundary  = self._quality_worse
        else:
            # Step toward quality_better (higher quality).
            # better side = sentinel (open), worse side = new_point (tested, closed).
            better_pt = QualityPoint(self._quality_better, 0, None)  # sentinel
            worse_pt  = new_point                                      # tested — excluded by _clamp_to_range
            boundary  = self._quality_better

        half_range = abs(boundary - new_point.q) / 2
        direction  = Decimal("1") if boundary > new_point.q else Decimal("-1")
        raw_q      = new_point.q + direction * half_range

        result = self._finalize_q(raw_q, new_point.q, worse_pt, better_pt)

        # _finalize_q may return new_point.q itself when quality_better==quality_worse
        # (both boundaries at the same value).  Treat that as a collision → exhausted.
        result = self._next_or_exhaust(result, self._attempted_points)

        logger.debug(
            "QualitySearchV3 phase0: quality=%s score=%.3f boundary=%s half_range=%s -> %s",
            new_point.q, new_point.score, boundary, half_range, result,
        )
        return result

    # ------------------------------------------------------------------
    # Phase 1+: point selection and dispatch
    # ------------------------------------------------------------------

    def _phase_select_and_dispatch(self, new_point: QualityPoint) -> Decimal | None:
        """Select best + neighbours and dispatch to the appropriate decision fork.

        Args:
            new_point: The most recently recorded attempt.

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        best_p = self._best_score_point
        assert best_p is not None  # guaranteed: at least 2 attempts recorded

        # Sort by quality value; direction: quality_better first (higher quality first for CRF)
        sort_reverse = self._quality_better > self._quality_worse
        sorted_q     = sorted(self._attempted_points.keys(), reverse=sort_reverse)
        best_idx     = sorted_q.index(best_p.q)

        # Gather up to 3 points: best + lower neighbour + upper neighbour
        # "lower" and "upper" are in sorted order (index-based), not quality-direction-based
        lower_p: QualityPoint | None = self._attempted_points[sorted_q[best_idx - 1]] if best_idx > 0                  else None
        upper_p: QualityPoint | None = self._attempted_points[sorted_q[best_idx + 1]] if best_idx < len(sorted_q) - 1  else None

        logger.debug(
            "QualitySearchV3 dispatch: best=%s(%.3f) lower=%s upper=%s",
            best_p.q, best_p.score,
            lower_p.q if lower_p else "None",
            upper_p.q if upper_p else "None",
        )

        if lower_p is None or upper_p is None:
            # 2-point decision: best + one neighbour
            neighbour = upper_p if lower_p is None else lower_p
            assert neighbour is not None
            return self._fork_2point(best_p, neighbour, new_point)

        # 3-point decision
        return self._fork_3point(best_p, lower_p, upper_p, new_point)

    # ------------------------------------------------------------------
    # 2-point fork
    # ------------------------------------------------------------------

    def _fork_2point(
        self,
        best_p:     QualityPoint,
        neighbour:  QualityPoint,
        new_point:  QualityPoint,
    ) -> Decimal | None:
        """Handle the 2-point decision: best + one neighbour.

        Dispatches to same-side (extrapolation / direction-exhausted) or
        different-sides (proportional interpolation) logic.

        Args:
            best_p:    Best-scoring recorded point.
            neighbour: The single neighbour of best_p.
            new_point: Most recently recorded attempt (used as ``from_q`` reference).

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        if best_p.is_pass == neighbour.is_pass:
            return self._fork_2point_same_side(best_p, neighbour, new_point)
        return self._fork_2point_different_sides(best_p, neighbour, new_point)

    def _fork_2point_same_side(
        self,
        p1:        QualityPoint,
        p2:        QualityPoint,
        new_point: QualityPoint,
    ) -> Decimal | None:
        """2-point same-side: linear extrapolation or direction-exhausted midpoint probe.

        When the outward direction is NOT exhausted: extrapolate linearly outside
        the two points toward the relevant boundary.

        When the outward direction IS exhausted and the midpoint-probe flag is
        not set: probe the midpoint between the two points.

        When the outward direction IS exhausted and the midpoint-probe flag IS
        set: declare exhaustion.

        Args:
            p1:        One of the two same-side points.
            p2:        The other same-side point.
            new_point: Most recently recorded attempt (used as ``from_q`` reference).

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        # Determine which direction is "outward" (away from the other side)
        # Both points are on the same side; outward = toward the boundary on that side.
        if p1.is_pass:
            # Both pass → outward is toward quality_worse
            outward_boundary = self._quality_worse
        else:
            # Both fail → outward is toward quality_better
            outward_boundary = self._quality_better

        # Direction-exhaustion: the outward boundary has actually been tested
        direction_exhausted = outward_boundary in self._attempted_points

        if not direction_exhausted:
            return self._extrapolate_outward(p1, p2, new_point, outward_boundary)

        # Direction exhausted
        if not self._midpoint_probe_flag:
            return self._midpoint_probe(p1, p2, new_point)

        # Midpoint probe already used — fully exhausted
        logger.debug("QualitySearchV3: direction exhausted + midpoint probe used → exhausted")
        self._exhausted = True
        return None

    def _extrapolate_outward(
        self,
        p1:               QualityPoint,
        p2:               QualityPoint,
        new_point:        QualityPoint,
        outward_boundary: Decimal,
    ) -> Decimal | None:
        """Extrapolate linearly outside two same-side points toward the boundary.

        Passes the two same-side points as ``worse_point`` / ``better_point`` to
        ``_compute_next_quality`` so that ``_compute_proportional_candidate`` can
        compute ``t`` from their metrics and extrapolate (``t`` outside ``[0, 1]``).
        The outward clamp boundary is supplied as the sentinel (or best opposite-side
        tested point) so ``_finalize_q`` / ``_clamp_to_range`` enforces the hard limit.

        Args:
            p1:               One of the two same-side points.
            p2:               The other same-side point.
            new_point:        Most recently recorded attempt (``from_q`` reference).
            outward_boundary: The hard boundary in the outward direction.

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        # Order the two same-side points so that the one *closer* to the target
        # (higher score, i.e. less negative for fails or less positive for passes)
        # acts as ``better_point`` and the one further away acts as ``worse_point``.
        # This ensures _compute_proportional_candidate extrapolates in the right
        # direction (t < 0 for fails going outward toward quality_better, t > 1 for
        # passes going outward toward quality_worse).
        if abs(p1.score) <= abs(p2.score):
            closer_p, further_p = p1, p2
        else:
            closer_p, further_p = p2, p1

        if p1.is_pass:
            # Both pass → outward is toward quality_worse.
            # Outward clamp: best-scoring tested fail point, or quality_worse sentinel.
            fail_points = [pt for pt in self._attempted_points.values() if pt.is_fail]
            outward_clamp = min(fail_points, key=lambda pt: abs(pt.score)) if fail_points else QualityPoint(self._quality_worse, 0, None)
            worse_pt  = outward_clamp
            better_pt = closer_p
        else:
            # Both fail → outward is toward quality_better.
            # Outward clamp: best-scoring tested pass point, or quality_better sentinel.
            pass_points = [pt for pt in self._attempted_points.values() if pt.is_pass]
            outward_clamp = min(pass_points, key=lambda pt: abs(pt.score)) if pass_points else QualityPoint(self._quality_better, 0, None)
            better_pt = outward_clamp
            worse_pt  = closer_p

        # Pass further_p as new_point so its metrics drive the proportional candidate —
        # it has the larger deficit/surplus and gives a better extrapolation slope.
        result = self._compute_next_quality(further_p, worse_pt, better_pt)

        logger.debug(
            "QualitySearchV3 extrapolate: p1=%s(%.3f) p2=%s(%.3f) boundary=%s -> %s",
            p1.q, p1.score, p2.q, p2.score, outward_boundary, result,
        )

        return self._next_or_exhaust(result, self._attempted_points)

    def _midpoint_probe(
        self,
        p1:        QualityPoint,
        p2:        QualityPoint,
        new_point: QualityPoint,
    ) -> Decimal | None:
        """Probe the midpoint between two same-side direction-exhausted points.

        Sets ``_midpoint_probe_flag = True`` after computing the probe point.

        Args:
            p1:        One of the two same-side points.
            p2:        The other same-side point.
            new_point: Most recently recorded attempt (``from_q`` reference).

        Returns:
            Midpoint quality value, or ``None`` if the range is exhausted.
        """
        mid = (p1.q + p2.q) / 2

        # Use the two points as the range boundaries for _finalize_q
        # Ensure worse_point and better_point are ordered correctly
        if abs(p1.q - self._quality_worse) < abs(p2.q - self._quality_worse):
            worse_pt, better_pt = p1, p2
        else:
            worse_pt, better_pt = p2, p1

        result = self._finalize_q(mid, new_point.q, worse_pt, better_pt)

        self._midpoint_probe_flag = True

        logger.debug(
            "QualitySearchV3 midpoint probe: p1=%s p2=%s mid=%s -> %s",
            p1.q, p2.q, mid, result,
        )

        if result is None:
            self._exhausted = True
        return result

    def _fork_2point_different_sides(
        self,
        pass_or_fail_p1: QualityPoint,
        pass_or_fail_p2: QualityPoint,
        new_point:       QualityPoint,
    ) -> Decimal | None:
        """2-point different-sides: proportional interpolation between pass and fail.

        Args:
            pass_or_fail_p1: One point (pass or fail).
            pass_or_fail_p2: The other point (opposite side).
            new_point:       Most recently recorded attempt (``from_q`` reference).

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        if pass_or_fail_p1.is_pass:
            pass_p, fail_p = pass_or_fail_p1, pass_or_fail_p2
        else:
            pass_p, fail_p = pass_or_fail_p2, pass_or_fail_p1

        result = self._compute_next_quality(new_point, fail_p, pass_p)

        logger.debug(
            "QualitySearchV3 interpolate: pass=%s(%.3f) fail=%s(%.3f) -> %s",
            pass_p.q, pass_p.score, fail_p.q, fail_p.score, result,
        )

        return self._next_or_exhaust(result, self._attempted_points)

    # ------------------------------------------------------------------
    # 3-point fork
    # ------------------------------------------------------------------

    def _fork_3point(
        self,
        best_p:    QualityPoint,
        lower_p:   QualityPoint,
        upper_p:   QualityPoint,
        new_point: QualityPoint,
    ) -> Decimal | None:
        """Handle the 3-point decision: best + lower neighbour + upper neighbour.

        When the 3 points span both sides (at least one pass and one fail):
        reduce to the 2-point different-sides case using the straddling pair.
        When both pairs straddle, prefer the pair including the worse-quality
        neighbour (to bias toward more efficient encodings).

        When all 3 points are on the same side: sweet-spot search — probe the
        midpoint of the larger sub-range.

        Args:
            best_p:    Best-scoring recorded point.
            lower_p:   Immediate lower neighbour (lower index in sorted order).
            upper_p:   Immediate upper neighbour (higher index in sorted order).
            new_point: Most recently recorded attempt (``from_q`` reference).

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        points = [lower_p, best_p, upper_p]
        any_pass = any(p.is_pass for p in points)
        any_fail = any(p.is_fail for p in points)

        if any_pass and any_fail:
            return self._fork_3point_spanning(best_p, lower_p, upper_p, new_point)

        # All same side — sweet-spot search
        return self._fork_3point_same_side(best_p, lower_p, upper_p, new_point)

    def _fork_3point_spanning(
        self,
        best_p:    QualityPoint,
        lower_p:   QualityPoint,
        upper_p:   QualityPoint,
        new_point: QualityPoint,
    ) -> Decimal | None:
        """3-point spanning both sides: reduce to 2-point different-sides.

        Selects the straddling pair (adjacent points on opposite sides).
        When both pairs straddle, prefers the pair including the worse-quality
        neighbour to bias toward more efficient encodings.

        Args:
            best_p:    Best-scoring recorded point.
            lower_p:   Immediate lower neighbour.
            upper_p:   Immediate upper neighbour.
            new_point: Most recently recorded attempt (``from_q`` reference).

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        lower_straddles = lower_p.is_pass != best_p.is_pass
        upper_straddles = upper_p.is_pass != best_p.is_pass

        if lower_straddles and upper_straddles:
            # Both pairs straddle — prefer the pair including the worse-quality neighbour.
            # "Worse quality" means closer to quality_worse in the codec's direction.
            # We compare which neighbour is further from quality_better.
            lower_dist_from_better = abs(lower_p.q - self._quality_better)
            upper_dist_from_better = abs(upper_p.q - self._quality_better)
            if lower_dist_from_better > upper_dist_from_better:
                # lower_p is the worse-quality neighbour
                selected_pair = (lower_p, best_p)
            else:
                # upper_p is the worse-quality neighbour
                selected_pair = (best_p, upper_p)
        elif lower_straddles:
            selected_pair = (lower_p, best_p)
        else:
            selected_pair = (best_p, upper_p)

        logger.debug(
            "QualitySearchV3 3-point spanning: best=%s lower=%s upper=%s → pair=(%s, %s)",
            best_p.q, lower_p.q, upper_p.q, selected_pair[0].q, selected_pair[1].q,
        )

        return self._fork_2point_different_sides(selected_pair[0], selected_pair[1], new_point)

    def _fork_3point_same_side(
        self,
        best_p:    QualityPoint,
        lower_p:   QualityPoint,
        upper_p:   QualityPoint,
        new_point: QualityPoint,
    ) -> Decimal | None:
        """3-point all same side: sweet-spot search on the larger sub-range.

        Computes the sizes of the two sub-ranges (lower_p→best_p and
        best_p→upper_p), selects the larger, and probes its midpoint.
        Declares exhaustion when the selected sub-range ≤ 1 granularity.

        Args:
            best_p:    Best-scoring recorded point.
            lower_p:   Immediate lower neighbour.
            upper_p:   Immediate upper neighbour.
            new_point: Most recently recorded attempt (``from_q`` reference).

        Returns:
            Next quality value, or ``None`` when exhausted.
        """
        left_range  = abs(best_p.q - lower_p.q)
        right_range = abs(upper_p.q - best_p.q)

        if left_range >= right_range:
            range_start, range_end = lower_p, best_p
            selected_range = left_range
        else:
            range_start, range_end = best_p, upper_p
            selected_range = right_range

        logger.debug(
            "QualitySearchV3 3-point same-side: best=%s lower=%s upper=%s "
            "left_range=%s right_range=%s selected=%s",
            best_p.q, lower_p.q, upper_p.q, left_range, right_range, selected_range,
        )

        if selected_range <= self._granularity:
            logger.debug("QualitySearchV3: sweet-spot sub-range collapsed → exhausted")
            self._exhausted = True
            return None

        mid = (range_start.q + range_end.q) / 2

        # Determine worse/better ordering for _finalize_q
        if abs(range_start.q - self._quality_worse) < abs(range_end.q - self._quality_worse):
            worse_pt, better_pt = range_start, range_end
        else:
            worse_pt, better_pt = range_end, range_start

        result = self._finalize_q(mid, new_point.q, worse_pt, better_pt)

        if result is None:
            self._exhausted = True
        return result
