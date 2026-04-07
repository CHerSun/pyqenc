"""
Quality evaluation and CRF adjustment for encoding pipeline.

This module provides quality evaluation against targets and CRF adjustment
algorithms for iterative encoding optimization.
"""
# CHerSun 2026

import logging
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import Protocol, TypedDict, assert_never, runtime_checkable

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
    comparison_range:  float
    acceptance_delta:  float

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
        comparison_range  = 20.0,  # practical target range ~80–100
        acceptance_delta  = 0.15,  # 0.15% surplus should be negligible
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
        comparison_range  = 10.0,  # practical target range ~90–100
        acceptance_delta  = 0.05,  # 0.05% after scaling (0.0005 raw)
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
        comparison_range  = 30.0,  # practical target range ~40–70 dB
        acceptance_delta  = 0.5,   # 0.5 dB surplus is negligible
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
    #     complexity        = 1.0,
    #     comparison_range  = 10.0,
    #     acceptance_delta  = 0.2,
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

    all_pass     = True
    fail_score   = 0.0
    pass_score   = 0.0
    early_accept = True

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
            all_pass     = False
            early_accept = False
            fail_score  += normalized
        else:
            pass_score += normalized
            if deficit > info.acceptance_delta:
                early_accept = False

    if not all_pass:
        return fail_score

    if early_accept:
        return 0.0

    return pass_score


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
            raise ValueError(
                f"quality_better ({quality_better}) must differ from quality_worse ({quality_worse})"
            )
        if granularity <= 0:
            raise ValueError(f"granularity must be > 0, got {granularity}")

        self._better_q:       Decimal                = quality_better
        self._worse_q:        Decimal                = quality_worse
        self._better_metrics: dict[str, float] | None = None
        self._worse_metrics:  dict[str, float] | None = None
        self._attempts:       int                    = 0
        self._exhausted:      bool                   = False
        self._quality_targets  = quality_targets
        self._granularity      = granularity
        self._quality_max_step = quality_max_step

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
        if self._better_metrics is not None:
            return self._better_q
        if self._worse_metrics is not None:
            return self._worse_q
        return None

    @property
    def best_metrics(self) -> dict[str, float] | None:
        """Metrics dict associated with ``best_quality``."""
        if self._better_metrics is not None:
            return self._better_metrics
        return self._worse_metrics

    @property
    def best_targets_met(self) -> bool:
        """``True`` iff at least one passing attempt was recorded."""
        return self._better_metrics is not None

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

        # Score the attempt — catch missing-key errors and treat as hard fail.
        try:
            score = _score_attempt(quality_results, self._quality_targets)
        except ValueError:
            logger.warning(
                "QualitySearch: missing metric key for quality=%s — treating as fail, using binary step",
                quality,
            )
            # Fall through with score = -inf; do NOT update bracket metrics.
            score = -float("inf")
            # Use binary interpolation toward better quality.
            raw = self._better_q + Decimal("0.5") * (self._worse_q - self._better_q)
            if self._quality_max_step is not None:
                step = raw - quality
                if abs(step) > self._quality_max_step:
                    raw = quality + (self._quality_max_step if step > 0 else -self._quality_max_step)
            next_q = (raw / self._granularity).to_integral_value(ROUND_HALF_EVEN) * self._granularity
            next_q = next_q.quantize(self._granularity)
            result = self._clamp_interior(next_q)
            if result is None:
                self._exhausted = True
                return None
            return result

        if score >= 0.0:
            # Pass (or early acceptance).
            self._better_q       = quality
            self._better_metrics = quality_results
            if score == 0.0:
                # Early acceptance — sweet spot found.
                self._exhausted = True
                return None
        else:
            # Fail.
            self._worse_q       = quality
            self._worse_metrics = quality_results

        # Exhaustion check: bracket collapsed to ≤ granularity.
        if (
            (self._better_metrics is not None or self._worse_metrics is not None)
            and abs(self._worse_q - self._better_q) <= self._granularity
        ):
            self._exhausted = True
            return None

        # Compute next quality via proportional interpolation.
        q_span = self._worse_q - self._better_q

        def _t_for_target(
            target:    QualityTarget,
            p_metrics: dict[str, float] | None,
            f_metrics: dict[str, float] | None,
        ) -> float | None:
            """Return interpolation fraction t ∈ [0, 1] for *target*, or ``None``."""
            if p_metrics is None or f_metrics is None:
                return None
            key   = f"{target.metric}_{target.statistic}"
            p_val = p_metrics.get(key)
            f_val = f_metrics.get(key)
            if p_val is None or f_val is None:
                return None
            info   = MetricType(target.metric).info
            d_pass = info.deficit(p_val, target.value)
            d_fail = info.deficit(f_val, target.value)
            span   = d_pass - d_fail
            if abs(span) < 1e-9:
                return None
            t = d_pass / span
            return t if 0.0 <= t <= 1.0 else None

        # Determine worst target from current result and from opposite boundary.
        current_passed   = score >= 0.0
        opposite_metrics = self._worse_metrics if current_passed else self._better_metrics
        opp_worst        = _find_worst_target(opposite_metrics, self._quality_targets) if opposite_metrics else None
        opp_target       = opp_worst[0] if opp_worst is not None else None

        found_worst = _find_worst_target(quality_results, self._quality_targets)
        worst_target = found_worst[0] if found_worst is not None else None

        candidates: list[tuple[float, str]] = []

        if worst_target is not None:
            t_primary = _t_for_target(worst_target, self._better_metrics, self._worse_metrics)
            if t_primary is not None:
                candidates.append((t_primary, f"primary worst={worst_target.metric}_{worst_target.statistic}"))

        if opp_target is not None:
            t_reverse = _t_for_target(opp_target, self._better_metrics, self._worse_metrics)
            if t_reverse is not None:
                candidates.append((t_reverse, f"reverse worst={opp_target.metric}_{opp_target.statistic}"))

        candidates.append((0.5, "binary"))

        t_chosen, label = candidates[0]
        raw = self._better_q + Decimal(str(t_chosen)) * q_span

        # Apply max-step clamping before quantization.
        if self._quality_max_step is not None:
            step = raw - quality
            if abs(step) > self._quality_max_step:
                raw = quality + (self._quality_max_step if step > 0 else -self._quality_max_step)

        # Snap to nearest granularity step.
        next_q = (raw / self._granularity).to_integral_value(ROUND_HALF_EVEN) * self._granularity
        next_q = next_q.quantize(self._granularity)

        result = self._clamp_interior(next_q)

        _pad = len(str(max(abs(self._better_q), abs(self._worse_q)).quantize(self._granularity)))
        logger.debug(
            "QualitySearch [%s]: better=%s worse=%s t=%.3f raw=%s → %s",
            label,
            str(self._better_q).rjust(_pad),
            str(self._worse_q).rjust(_pad),
            t_chosen,
            str(raw.quantize(self._granularity)).rjust(_pad),
            str(result).rjust(_pad) if result is not None else "None (exhausted)",
        )

        if result is None:
            self._exhausted = True
            return None
        return result

    def _clamp_interior(self, q: Decimal) -> Decimal | None:
        """Clamp *q* to the valid interior of the current search bracket.

        Excludes a boundary only once a real result has been recorded there.
        Returns ``None`` when no interior point exists.
        """
        lower = min(self._better_q, self._worse_q)
        upper = max(self._better_q, self._worse_q)
        lo = (
            lower
            if (self._better_metrics is None and lower == self._better_q)
            or (self._worse_metrics is None and lower == self._worse_q)
            else lower + self._granularity
        )
        hi = (
            upper
            if (self._better_metrics is None and upper == self._better_q)
            or (self._worse_metrics is None and upper == self._worse_q)
            else upper - self._granularity
        )
        if lo > hi:
            return None
        return max(lo, min(hi, q))




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

        self._pass_q:       Decimal                  = quality_better   # sentinel
        self._pass_metrics: dict[str, float] | None  = None
        self._best_q:       Decimal                  = quality_worse    # sentinel (updated to first real attempt)
        self._best_metrics: dict[str, float] | None  = None
        self._best_score:   float                    = -math.inf
        self._fail_q:       Decimal                  = quality_worse    # sentinel (lags behind _best_q in all-failing phase)
        self._fail_metrics: dict[str, float] | None  = None
        self._attempts:     int                      = 0
        self._exhausted:    bool                     = False
        # Phase tracking: True once the first passing result is seen (all-passing phase).
        # False means all-failing phase (or not yet started).
        self._seen_pass:    bool                     = False
        self._quality_targets:  list[QualityTarget]  = quality_targets
        self._granularity:      Decimal              = granularity
        self._quality_max_step: Decimal | None       = quality_max_step

    # ------------------------------------------------------------------
    # Protocol properties
    # ------------------------------------------------------------------

    @property
    def attempts(self) -> int:
        """Total number of ``record()`` calls made so far."""
        return self._attempts

    @property
    def best_quality(self) -> Decimal | None:
        """Best quality found: best-efficiency passing value if any pass, else best-fail value."""
        if self._best_metrics is None:
            return None
        # When any pass has been seen, _best_q always holds the best-efficiency passing value.
        # This is true in all-passing phase and in 3-point mode (both entry paths).
        if self._seen_pass:
            return self._best_q
        # All-failing phase: _best_q is the best-fail value.
        return self._best_q

    @property
    def best_metrics(self) -> dict[str, float] | None:
        """Metrics dict associated with ``best_quality``."""
        if self._best_metrics is None:
            return None
        # _best_metrics always corresponds to _best_q.
        return self._best_metrics

    @property
    def best_targets_met(self) -> bool:
        """``True`` iff at least one passing attempt was recorded."""
        return self._seen_pass

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
        if self._exhausted:
            return None

        self._attempts += 1

        # Score the attempt — catch missing-key errors and treat as worst fail.
        try:
            score = _score_attempt(quality_results, self._quality_targets)
        except ValueError:
            logger.warning(
                "QualitySearchV2: missing metric key for quality=%s — treating as worst fail",
                quality,
            )
            score = -math.inf

        # Early acceptance: score == 0.0 means sweet spot found.
        if score == 0.0:
            self._best_q       = quality
            self._best_metrics = quality_results
            self._best_score   = 0.0
            self._pass_q       = quality
            self._pass_metrics = quality_results
            self._seen_pass    = True
            self._exhausted    = True
            logger.debug("QualitySearchV2: early acceptance at quality=%s", quality)
            return None

        # Determine current phase.
        # _seen_pass tracks whether any passing result has been recorded.
        # Phase priority: 3-point > all-failing (has real fails) > all-passing > first-call routing.
        in_3point_mode = self._pass_metrics is not None and self._fail_metrics is not None

        if in_3point_mode:
            is_new_best = abs(score) < abs(self._best_score)
            self._update_3point(quality, quality_results, score, is_new_best)
        elif self._fail_metrics is not None:
            # Has real fails (lagged), no passes yet → all-failing phase.
            is_new_best = abs(score) < abs(self._best_score) or self._best_metrics is None
            self._update_all_failing(quality, quality_results, score, is_new_best)
        elif self._seen_pass or score >= 0.0:
            # All-passing phase: first pass seen or continuing all-passing.
            self._seen_pass = True
            is_new_best = abs(score) < abs(self._best_score) or self._best_metrics is None
            self._update_all_passing(quality, quality_results, score, is_new_best)
        else:
            # All-failing phase.
            is_new_best = abs(score) < abs(self._best_score) or self._best_metrics is None
            self._update_all_failing(quality, quality_results, score, is_new_best)

        return self._compute_next(quality)

    # ------------------------------------------------------------------
    # Phase update helpers
    # ------------------------------------------------------------------

    def _update_all_failing(
        self,
        quality:         Decimal,
        quality_results: dict[str, float],
        score:           float,
        is_new_best:     bool,
    ) -> None:
        """Update state during the all-failing phase (``_pass_metrics is None``)."""
        if is_new_best:
            # New best: lag _fail_q one step behind, advance _best_q.
            self._fail_q       = self._best_q
            self._fail_metrics = self._best_metrics
            self._best_q       = quality
            self._best_metrics = quality_results
            self._best_score   = score
            logger.debug(
                "QualitySearchV2 all-failing new-best: best_q=%s fail_q=%s score=%.4f",
                quality, self._fail_q, score,
            )
        else:
            # Worse than current best — sweet spot passed; transition to 3-point mode.
            self._pass_q       = quality
            self._pass_metrics = quality_results
            logger.debug(
                "QualitySearchV2 all-failing transition→3pt: pass_q=%s best_q=%s fail_q=%s",
                quality, self._best_q, self._fail_q,
            )

    def _update_all_passing(
        self,
        quality:         Decimal,
        quality_results: dict[str, float],
        score:           float,
        is_new_best:     bool,
    ) -> None:
        """Update state during the all-passing phase (``_fail_metrics is None``)."""
        if is_new_best:
            # New best: lag _pass_q one step behind (only when there's a real prior best).
            if self._best_metrics is not None:
                self._pass_q       = self._best_q
                self._pass_metrics = self._best_metrics
            self._best_q       = quality
            self._best_metrics = quality_results
            self._best_score   = score
            logger.debug(
                "QualitySearchV2 all-passing new-best: best_q=%s pass_q=%s score=%.4f",
                quality, self._pass_q, score,
            )
        else:
            # Worse than current best — sweet spot passed; transition to 3-point mode.
            self._fail_q       = quality
            self._fail_metrics = quality_results
            logger.debug(
                "QualitySearchV2 all-passing transition→3pt: pass_q=%s best_q=%s fail_q=%s",
                self._pass_q, self._best_q, quality,
            )

    def _update_3point(
        self,
        quality:         Decimal,
        quality_results: dict[str, float],
        score:           float,
        is_new_best:     bool,
    ) -> None:
        """Update state in 3-point mode (both sentinels have real metrics)."""
        in_range_b = self._in_range_b(quality)

        if is_new_best:
            if in_range_b:
                # Promote: _pass_q = old _best_q, _best_q = quality.
                self._pass_q       = self._best_q
                self._pass_metrics = self._best_metrics
                self._best_q       = quality
                self._best_metrics = quality_results
                self._best_score   = score
                logger.debug(
                    "QualitySearchV2 3pt Range-B new-best promote: best_q=%s pass_q=%s score=%.4f",
                    quality, self._pass_q, score,
                )
            else:
                # Demote: _fail_q = old _best_q, _best_q = quality.
                self._fail_q       = self._best_q
                self._fail_metrics = self._best_metrics
                self._best_q       = quality
                self._best_metrics = quality_results
                self._best_score   = score
                logger.debug(
                    "QualitySearchV2 3pt Range-A new-best demote: best_q=%s fail_q=%s score=%.4f",
                    quality, self._fail_q, score,
                )
        else:
            if in_range_b:
                # Tighten fail boundary.
                self._fail_q       = quality
                self._fail_metrics = quality_results
                logger.debug(
                    "QualitySearchV2 3pt Range-B tighten: fail_q=%s", quality,
                )
            else:
                # Tighten pass boundary.
                self._pass_q       = quality
                self._pass_metrics = quality_results
                logger.debug(
                    "QualitySearchV2 3pt Range-A tighten: pass_q=%s", quality,
                )

    def _in_range_b(self, quality: Decimal) -> bool:
        """Return ``True`` if *quality* is in Range B ``[_best_q ... _fail_q]`` (exclusive of _best_q).

        Uses ``min/max`` to handle both CRF (lower=better) and VBR (higher=better) directions.
        Edge case: ``quality == _best_q`` is treated as Range B.
        """
        lo = min(self._best_q, self._fail_q)
        hi = max(self._best_q, self._fail_q)
        # Range A is (pass_q, best_q) exclusive of best_q.
        # Range B is [best_q, fail_q] — includes best_q as edge case.
        lo_a = min(self._pass_q, self._best_q)
        hi_a = max(self._pass_q, self._best_q)
        in_range_a = lo_a <= quality < hi_a if self._pass_q < self._best_q else lo_a < quality <= hi_a
        # Anything not in Range A (exclusive of best_q) is Range B.
        _ = lo, hi  # suppress unused warning
        return not in_range_a

    # ------------------------------------------------------------------
    # Next quality computation
    # ------------------------------------------------------------------

    def _compute_next(self, current_quality: Decimal) -> Decimal | None:
        """Compute the next quality value to try, or ``None`` if exhausted.

        Picks the midpoint of the larger active range, quantizes to granularity,
        and exhausts when no untested interior point remains.
        """
        range_a = abs(self._best_q - self._pass_q)
        range_b = abs(self._fail_q - self._best_q)

        # Exhaustion: both ranges collapsed to ≤ granularity.
        if range_a <= self._granularity and range_b <= self._granularity:
            self._exhausted = True
            logger.debug(
                "QualitySearchV2: exhausted — range_a=%s range_b=%s gran=%s",
                range_a, range_b, self._granularity,
            )
            return None

        # Determine phase after update — mirrors the phase detection in record().
        in_3point_mode    = self._pass_metrics is not None and self._fail_metrics is not None
        still_all_failing = not in_3point_mode and not self._seen_pass
        still_all_passing = not in_3point_mode and self._seen_pass

        if still_all_failing:
            # Interpolate toward _pass_q (better quality): midpoint of [_pass_q, _best_q].
            span  = self._best_q - self._pass_q
            raw_q = self._pass_q + Decimal("0.5") * span
        elif still_all_passing:
            # Interpolate toward _fail_q (worse quality): midpoint of [_best_q, _fail_q].
            span  = self._fail_q - self._best_q
            raw_q = self._best_q + Decimal("0.5") * span
        else:
            # 3-point mode: pick midpoint of larger range.
            if range_a >= range_b:
                span  = self._best_q - self._pass_q
                raw_q = self._pass_q + Decimal("0.5") * span
            else:
                span  = self._fail_q - self._best_q
                raw_q = self._best_q + Decimal("0.5") * span

        # Apply max-step clamping.
        if self._quality_max_step is not None:
            step = raw_q - current_quality
            if abs(step) > self._quality_max_step:
                raw_q = current_quality + (
                    self._quality_max_step if step > 0 else -self._quality_max_step
                )

        # Quantize to granularity.
        next_q = (raw_q / self._granularity).to_integral_value(ROUND_HALF_EVEN) * self._granularity
        next_q = next_q.quantize(self._granularity)

        # Clamp to the full active range spanning all three anchors.
        lower = min(self._pass_q, self._best_q, self._fail_q)
        upper = max(self._pass_q, self._best_q, self._fail_q)
        next_q = max(lower, min(upper, next_q))

        # Already-tested set: all three anchors have been recorded.
        already_tested = {self._best_q}
        if self._pass_metrics is not None:
            already_tested.add(self._pass_q)
        if self._fail_metrics is not None:
            already_tested.add(self._fail_q)

        if next_q in already_tested:
            # Try the other range's interior point.
            if range_a >= range_b:
                # Was picking from Range A — try Range B instead.
                if range_b > self._granularity:
                    alt_span = self._fail_q - self._best_q
                    alt_raw  = self._best_q + Decimal("0.5") * alt_span
                    alt_q    = (alt_raw / self._granularity).to_integral_value(ROUND_HALF_EVEN) * self._granularity
                    alt_q    = alt_q.quantize(self._granularity)
                    alt_q    = max(lower, min(upper, alt_q))
                    if alt_q not in already_tested and lower <= alt_q <= upper:
                        next_q = alt_q
                    else:
                        self._exhausted = True
                        return None
                else:
                    self._exhausted = True
                    return None
            else:
                # Was picking from Range B — try Range A instead.
                if range_a > self._granularity:
                    alt_span = self._best_q - self._pass_q
                    alt_raw  = self._pass_q + Decimal("0.5") * alt_span
                    alt_q    = (alt_raw / self._granularity).to_integral_value(ROUND_HALF_EVEN) * self._granularity
                    alt_q    = alt_q.quantize(self._granularity)
                    alt_q    = max(lower, min(upper, alt_q))
                    if alt_q not in already_tested and lower <= alt_q <= upper:
                        next_q = alt_q
                    else:
                        self._exhausted = True
                        return None
                else:
                    self._exhausted = True
                    return None

        # Final safety: must be strictly inside [lower, upper] and untested.
        if next_q in already_tested or not (lower <= next_q <= upper):
            self._exhausted = True
            return None

        _pad = len(str(max(abs(self._pass_q), abs(self._fail_q)).quantize(self._granularity)))
        logger.debug(
            "QualitySearchV2: pass_q=%s best_q=%s fail_q=%s → next=%s (range_a=%s range_b=%s)",
            str(self._pass_q).rjust(_pad),
            str(self._best_q).rjust(_pad),
            str(self._fail_q).rjust(_pad),
            str(next_q).rjust(_pad),
            range_a,
            range_b,
        )
        return next_q
