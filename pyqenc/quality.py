"""
Quality evaluation and CRF adjustment for encoding pipeline.

This module provides quality evaluation against targets and CRF adjustment
algorithms for iterative encoding optimization.
"""
# CHerSun 2026

import logging
import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import TypedDict, assert_never

import pandas as pd

from pyqenc.constants import CRF_METRIC_POSITIVE_DELTA, PADDING_QUALITY_NUMBER
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

    Attributes:
        name:              Human-readable display name (e.g. ``"PSNR"``).
        id:                Lowercase string key matching ``MetricType.value``
                           (e.g. ``"psnr"``).  Used for dict keys and filenames.
        higher_is_better:  ``True`` when a higher normalized value is better
                           (VMAF, SSIM, PSNR).  ``False`` for metrics like VIF
                           where 0 is lossless and higher means worse.
        scale_factor:      Multiply the raw value by this before clipping.
                           SSIM raw range is 0–1, so ``scale_factor=100.0``.
                           All others use ``1.0``.
        clip_upper:        After scaling, clip values above this threshold.
                           ``None`` means no upper clipping.
                           PSNR: ``100.0`` (caps ∞ dB).
        clip_lower:        After scaling, clip values below this threshold.
                           ``None`` means no lower clipping.
        lossless_value:    Normalized value that represents lossless quality
                           (e.g. ``100.0`` for VMAF/SSIM/PSNR, ``0.0`` for VIF).
        lossless_raw_repr: Human-readable string for the raw lossless value
                           (e.g. ``"∞ dB"`` for PSNR, ``"100.0"`` for SSIM/VMAF).
        display_unit:      Unit suffix for display (e.g. ``" dB"``, ``"%"``).
        plot_y_min:        Lower bound for the Y-axis in plots (normalized scale).
        plot_y_max:        Upper bound for the Y-axis in plots (normalized scale).
                           Slightly above ``lossless_value`` to leave headroom.
        complexity:        Relative computational cost compared to SSIM/PSNR (baseline 1.0).
                           Used to weight progress bar totals so that slower metrics
                           (e.g. VMAF) contribute proportionally more to the reported
                           duration.  SSIM and PSNR are 1.0; VMAF is ~10.0 (empirical
                           estimate — actual ratio varies by content and hardware).
    """

    name:              str
    id:                str
    higher_is_better:  bool
    scale_factor:      float
    clip_upper:        float | None
    clip_lower:        float | None
    lossless_value:    float
    lossless_raw_repr: str
    display_unit:      str
    plot_y_min:        float
    plot_y_max:        float
    complexity:        float

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
            result = value * self.scale_factor
            if self.clip_lower is not None:
                result = result.clip(lower=self.clip_lower)
            if self.clip_upper is not None:
                result = result.clip(upper=self.clip_upper)
            return result
        else:
            # scalar path — handle inf explicitly
            result_f = value * self.scale_factor
            if self.clip_lower is not None:
                result_f = max(result_f, self.clip_lower)
            if self.clip_upper is not None:
                result_f = min(result_f, self.clip_upper)
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

    @property
    def info(self) -> MetricInfo:
        """Return the ``MetricInfo`` descriptor for this metric type."""
        return _METRIC_INFO[self]


_METRIC_INFO: dict[MetricType, MetricInfo] = {
    MetricType.VMAF: MetricInfo(
        name              = "VMAF",
        id                = "vmaf",
        higher_is_better  = True,
        scale_factor      = 1.0,
        clip_upper        = None,
        clip_lower        = None,
        lossless_value    = 100.0,
        lossless_raw_repr = "100.0",
        display_unit      = "%",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        complexity        = 10.0,  # empirical estimate; VMAF is significantly slower than PSNR/SSIM
    ),
    MetricType.SSIM: MetricInfo(
        name              = "SSIM",
        id                = "ssim",
        higher_is_better  = True,
        scale_factor      = 100.0,
        clip_upper        = None,
        clip_lower        = None,
        lossless_value    = 100.0,
        lossless_raw_repr = "100.0",
        display_unit      = "%",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        complexity        = 1.0,
    ),
    MetricType.PSNR: MetricInfo(
        name              = "PSNR",
        id                = "psnr",
        higher_is_better  = True,
        scale_factor      = 1.0,
        clip_upper        = 100.0,
        clip_lower        = None,
        lossless_value    = 100.0,
        lossless_raw_repr = "∞ dB",
        display_unit      = " dB",
        plot_y_min        = 0.0,
        plot_y_max        = 103.0,
        complexity        = 1.0,
    ),
    # VIF placeholder (not yet active and not checked for correctness):
    # MetricType.VIF: MetricInfo(
    #     name              = "VIF",
    #     id                = "vif",
    #     higher_is_better  = False,
    #     scale_factor      = 1.0,
    #     clip_upper        = None,
    #     clip_lower        = 0.0,
    #     lossless_value    = 0.0,
    #     lossless_raw_repr = "0.0",
    #     display_unit      = "",
    #     plot_y_min        = -0.1,
    #     plot_y_max        = 2.0,
    # ),
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
    """Track the tightest known pass/fail quality bracket, attempt count, and observed
    metric values at the bracket boundaries.

    ``fail_crf`` and ``pass_crf`` are initialized to the codec limits as
    sentinels, then narrowed on each attempt.  All quality values are ``Decimal``
    so arithmetic and string formatting are exact.

    ``fail_metrics`` and ``pass_metrics`` store the full measured quality dict
    (keyed as ``"<metric>_<stat>"``) observed at the current ``fail_crf`` and
    ``pass_crf`` respectively.  They are ``None`` until the corresponding
    boundary has been observed at least once with a real encoding result.

    Storing the full dict (rather than a single scalar) is essential: the
    worst-performing target can change between attempts, so Phase 2
    interpolation must look up the *current* worst target's key in both
    boundary dicts to get two real observations of that specific metric.

    Attributes:
        fail_crf:      Lowest quality that still failed (upper bracket bound).
                       Initialized to ``crf_max``; narrows downward on misses.
        pass_crf:      Highest quality that still passed (lower bracket bound).
                       Initialized to ``crf_min``; narrows upward on passes.
        fail_metrics:  Full quality_results dict observed at ``fail_crf``.
                       ``None`` until a real fail result is recorded.
        pass_metrics:  Full quality_results dict observed at ``pass_crf``.
                       ``None`` until a real pass result is recorded.
        attempts:      Number of encoding attempts recorded.
    """

    fail_crf:     Decimal
    pass_crf:     Decimal
    fail_metrics: dict[str, float] | None = None
    pass_metrics: dict[str, float] | None = None
    attempts:     int                     = 0

    def add(
        self,
        crf:     Decimal,
        passed:  bool,
        metrics: dict[str, float] | None = None,
    ) -> None:
        """Record an encoding attempt and update the pass/fail bracket.

        When *metrics* is provided it is stored at the updated boundary so
        Phase 2 proportional interpolation can look up any target key later.

        Args:
            crf:     Quality value used.
            passed:  Whether all quality targets were met.
            metrics: Full measured quality dict (``"<metric>_<stat>"`` keys).
                     Pass ``None`` when metrics are unavailable (history
                     pre-population from sidecars without full metric data).
        """
        self.attempts += 1
        if not passed:
            if crf < self.fail_crf:
                self.fail_crf     = crf
                self.fail_metrics = metrics
        else:
            if crf > self.pass_crf:
                self.pass_crf     = crf
                self.pass_metrics = metrics

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
        # Normalize inf (e.g. PSNR lossless) to the metric's lossless_value
        if math.isinf(actual):
            actual = info.lossless_value
        deficit = info.deficit(actual, target.value)
        if deficit < worst_deficit:
            worst_deficit = deficit
            worst_target  = target
            worst_actual  = actual

    if worst_target is None:
        return None
    return worst_target, worst_deficit, worst_actual


def adjust_crf(
    current_crf:     Decimal,
    quality_results: dict[str, float],
    quality_targets: list[QualityTarget],
    history:         CRFHistory,
    granularity:     Decimal = Decimal("0.5"),
) -> Decimal | None:
    """Calculate the next quality value to try, updating *history* with the current result.

    ## Algorithm

    Builds a candidate list of interpolation fractions ``t ∈ [0, 1]`` along
    ``[pass_crf, fail_crf]`` and takes the first valid one:

    1. **Primary**: interpolate from the current worst target's metric curve
       between the two boundary dicts.  Valid when the metric actually straddles
       the target inside the bracket (``t ∈ [0, 1]``).
    2. **Reverse**: re-find the worst target from the *opposite* boundary's
       metrics (the boundary not just updated), then interpolate.  Catches the
       case where the current worst metric passes at both boundaries.
    3. **Binary**: ``t = 0.5`` (midpoint) — always valid, always last.

    When boundary metrics are absent (early attempts), interpolation candidates
    return ``None`` and binary is used automatically.

    **Early acceptance**: when all targets pass and the tightest surplus is
    within ``CRF_METRIC_POSITIVE_DELTA``, the current value is accepted without
    trying to squeeze further — saves one encoding pass.

    **Exhaustion**: when ``fail_crf - pass_crf ≤ granularity`` the bracket
    is too tight to improve; return ``None`` so the caller keeps the last pass.

    **Boundary inclusivity**: when a boundary sentinel has no real metrics yet
    (``fail_metrics`` or ``pass_metrics`` is ``None``), the boundary value itself
    is a valid candidate — the algorithm hasn't tested it yet.  Once a real
    result is recorded at a boundary, that value is excluded (already tested).

    **Quantization**: the raw interpolated value is quantized to *granularity*
    using ``Decimal.quantize``.  When the current attempt passed, rounding is
    biased upward (``ROUND_CEILING``); when it failed, rounding is biased
    downward (``ROUND_FLOOR``) — keeping the result on the correct side.
    The returned ``Decimal`` is already quantized, so ``str()`` produces the
    correct display string for both logs and ffmpeg args.

    Args:
        current_crf:     Quality value used in the most recent attempt.
        quality_results: Measured quality metrics keyed as ``"<metric>_<stat>"``.
        quality_targets: Quality targets to meet.
        history:         Quality bracket history; updated in-place with this result.
        granularity:     Step size as a ``Decimal``.  Comes from
                         ``strategy.codec.quality_granularity``.

    Returns:
        Next quality value to try (quantized ``Decimal``), or ``None`` when the
        search is exhausted or the current result is accepted as final.
    """
    fail_crf, pass_crf = history.fail_crf, history.pass_crf

    # Preemptive exhaustion check — bracket too tight to improve.
    # Skip when no pass_metrics or no fail_metrics - no true bracket yet, allow inclusive boundaries.
    if history.pass_metrics and history.fail_metrics and (fail_crf - pass_crf <= granularity):
        return None

    found = _find_worst_target(quality_results, quality_targets)
    if found is None:
        logger.warning("No valid metric results found, cannot adjust CRF")
        return None

    worst_target, worst_deficit, worst_actual = found
    current_passed = worst_deficit >= 0

    # Update history with this attempt's full metrics dict.
    history.add(current_crf, current_passed, quality_results)

    # Re-read bounds after update (they may have narrowed).
    fail_crf, pass_crf = history.fail_crf, history.pass_crf
    if fail_crf - pass_crf <= granularity:
        return None

    # --- Early acceptance ---
    if current_passed and worst_deficit <= CRF_METRIC_POSITIVE_DELTA:
        logger.debug(
            "Least-proficient metric surplus %.3f ≤ CRF_METRIC_POSITIVE_DELTA (%.3f), "
            "accepting %s as final.",
            worst_deficit, CRF_METRIC_POSITIVE_DELTA, str(current_crf).rjust(PADDING_QUALITY_NUMBER),
        )
        return None

    crf_span = fail_crf - pass_crf

    def _clamp_interior(crf: Decimal) -> Decimal | None:
        """Clamp *crf* to the valid search range.

        When a boundary sentinel has no real metrics yet, the boundary value
        itself is valid — the algorithm hasn't tested it yet.  Once a real
        result is recorded at a boundary, that value is excluded (already tested).

        Returns ``None`` when there is no valid point at least one *granularity*
        step away from both boundaries.
        """
        lo = pass_crf if history.pass_metrics is None else pass_crf + granularity
        hi = fail_crf if history.fail_metrics is None else fail_crf - granularity
        if lo > hi:
            return None
        return max(lo, min(hi, crf))

    def _t_for_target(
        target:    QualityTarget,
        p_metrics: dict[str, float] | None,
        f_metrics: dict[str, float] | None,
    ) -> float | None:
        """Return interpolation t for *target*, or ``None`` if outside [0, 1]."""
        if p_metrics is None or f_metrics is None:
            return None
        key      = f"{target.metric}_{target.statistic}"
        p_val    = p_metrics.get(key)
        f_val    = f_metrics.get(key)
        if p_val is None or f_val is None:
            return None
        tgt_info = MetricType(target.metric).info
        d_pass   = tgt_info.deficit(p_val, target.value)
        d_fail   = tgt_info.deficit(f_val, target.value)
        span     = d_pass - d_fail
        if abs(span) < 1e-9:
            return None
        t = d_pass / span
        return t if 0.0 <= t <= 1.0 else None

    opposite_metrics = history.fail_metrics if current_passed else history.pass_metrics
    opp_worst        = _find_worst_target(opposite_metrics, quality_targets) if opposite_metrics else None
    opp_target       = opp_worst[0] if opp_worst is not None else None

    candidates: list[tuple[float, str]] = []

    t_primary = _t_for_target(worst_target, history.pass_metrics, history.fail_metrics)
    if t_primary is not None:
        candidates.append((t_primary, f"primary worst={worst_target.metric}_{worst_target.statistic}"))

    if opp_target is not None:
        t_reverse = _t_for_target(opp_target, history.pass_metrics, history.fail_metrics)
        if t_reverse is not None:
            candidates.append((t_reverse, f"reverse worst={opp_target.metric}_{opp_target.statistic}"))

    candidates.append((0.5, "binary"))

    t_chosen, label = candidates[0]
    # Compute raw value in Decimal to avoid float drift, then quantize.
    raw_crf  = pass_crf + Decimal(str(t_chosen)) * crf_span
    rounding = ROUND_CEILING if current_passed else ROUND_FLOOR
    next_crf = (raw_crf / granularity).to_integral_value(rounding=rounding) * granularity
    # Ensure the result carries the same exponent as granularity (e.g. "18.5" not "18.50")
    next_crf = next_crf.quantize(granularity)

    result = _clamp_interior(next_crf)

    logger.debug(
        "CRF [%s]: pass=%s fail=%s t=%.3f raw=%s → %s",
        label,
        str(pass_crf).rjust(PADDING_QUALITY_NUMBER),
        str(fail_crf).rjust(PADDING_QUALITY_NUMBER),
        t_chosen,
        str(raw_crf.quantize(granularity)).rjust(PADDING_QUALITY_NUMBER),
        str(result).rjust(PADDING_QUALITY_NUMBER) if result is not None else "None (exhausted)",
    )
    return result


