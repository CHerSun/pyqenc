"""The quality-metrics identity vocabulary — the leaf below the models layer.

Owns the metric vocabulary end to end: the metric and statistic types, the
metric-statistic pair, the measured/targeted set roles, the fixed comparison
pairs, and the flattening of nested per-metric statistics into the flat
measured set. The module imports nothing from ``pyqenc.models`` or
``pyqenc.quality`` — the quality target and the sidecars embed these types,
so the dependency direction is one-way (everything imports this, this
imports nothing of ours).
"""
# CHerSun 2026

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import (
    Annotated,
    TypedDict,
    cast,
)

import pandas as pd
from pydantic import BeforeValidator, PlainSerializer

# ---------------------------------------------------------------------------
# Metric descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricInfo:
    """Descriptor carrying all metric-specific properties.

    Single source of truth for normalization, display, and quality search behaviour.
    Accessed via ``MetricType.PSNR.info``, etc.

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
        acceptance_delta:  Per-metric threshold for early acceptance during the quality search.
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
    ``MetricInfo`` descriptor for normalization, display, and quality search.
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
    MetricType.VIF: MetricInfo(
        name              = "VIF",
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

    Subset of :class:`FullMetricStatistics` stored in sidecars and used for
    targeting. Includes the same percentile selection used by the
    visualization plots: min, p05, p10, p25, median (p50), p75, p90, p95,
    max, std.
    """

    min:    float
    p05:    float
    p10:    float
    p25:    float
    median: float
    p75:    float
    p90:    float
    p95:    float
    max:    float
    std:    float


PerMetricStats = dict[MetricType, MetricStats]
"""Measured statistics per metric (one measurement's full nested form)."""


# ---------------------------------------------------------------------------
# Metric-statistic identity
# ---------------------------------------------------------------------------

class StatisticType(Enum):
    """The closed set of measured statistics.

    Member values are the canonical sidecar tokens — the single spelling
    inside ``{metric}_{statistic}`` keys. The display vocabulary (the plot
    labels: ``Min, 5%, 10%, …, 95%, Max, Std``) lives on :attr:`display`.
    Declaration order is the closed set's order (headers list a metric's
    compared statistics in it).
    """

    MIN    = "min"
    P05    = "p05"
    P10    = "p10"
    P25    = "p25"
    MEDIAN = "median"
    P75    = "p75"
    P90    = "p90"
    P95    = "p95"
    MAX    = "max"
    STD    = "std"

    @property
    def display(self) -> str:
        """The plot-vocabulary label: percentiles as percents, min/max/std as words."""
        return _STATISTIC_DISPLAY[self]


_STATISTIC_DISPLAY: dict[StatisticType, str] = {
    StatisticType.MIN:    "Min",
    StatisticType.P05:    "5%",
    StatisticType.P10:    "10%",
    StatisticType.P25:    "25%",
    StatisticType.MEDIAN: "50%",
    StatisticType.P75:    "75%",
    StatisticType.P90:    "90%",
    StatisticType.P95:    "95%",
    StatisticType.MAX:    "Max",
    StatisticType.STD:    "Std",
}

_STATISTIC_ALIASES: dict[str, str] = {
    "med": "median",
}


@dataclass(frozen=True)
class MetricStatKey:
    """The identity of one measured value: a metric paired with a statistic.

    The single naming authority for metric-statistic keys: ``sidecar_key``
    is THE canonical persistence form (``vmaf_median``), ``cli_key`` is THE
    CLI/config form (``vmaf-min`` — the dash-separated target vocabulary),
    ``display`` is THE display form (``VMAF 50%``), and ``parse`` accepts
    the tolerant input vocabulary (dash or underscore separators, the
    ``med`` alias, case-insensitive). Frozen and hashable — usable directly
    as the key of measured-metrics maps.

    Attributes:
        metric:    The measured metric.
        statistic: The measured statistic.
    """

    metric:    MetricType
    statistic: StatisticType

    def sidecar_key(self) -> str:
        """Return the canonical persistence form, e.g. ``vmaf_median``."""
        return f"{self.metric.value}_{self.statistic.value}"

    def cli_key(self) -> str:
        """Return the CLI/config form, e.g. ``vmaf-min`` (the dash separator)."""
        return f"{self.metric.value}-{self.statistic.value}"

    def display(self) -> str:
        """Return the display form, e.g. ``VMAF 50%`` (plot vocabulary)."""
        return f"{self.metric.info.name} {self.statistic.display}"

    @classmethod
    def parse(cls, text: str) -> MetricStatKey:
        """Parse a metric-statistic key from tolerant input.

        Args:
            text: Key text with a dash or underscore separator and optional
                case variation, e.g. ``vmaf-median`` or ``VMAF_MEDIAN``.

        Returns:
            The typed key.

        Raises:
            ValueError: No separator, unknown metric, or unknown statistic —
                loud, never a silent round-trip.
        """
        normalized = text.strip().lower().replace("-", "_")
        metric_token, sep, statistic_token = normalized.partition("_")
        if not sep or not statistic_token:
            raise ValueError(
                f"Invalid metric-statistic key '{text}': "
                f"expected 'metric-statistic' (e.g. 'vmaf-median')"
            )
        try:
            metric = MetricType(metric_token)
        except ValueError:
            valid = sorted(m.value for m in MetricType)
            raise ValueError(
                f"Invalid metric '{metric_token}' in '{text}'. Must be one of: {valid}"
            ) from None
        try:
            statistic = StatisticType(_STATISTIC_ALIASES.get(statistic_token, statistic_token))
        except ValueError:
            valid = sorted(s.value for s in StatisticType)
            raise ValueError(
                f"Invalid statistic '{statistic_token}' in '{text}'. Must be one of: {valid}"
            ) from None
        return cls(metric=metric, statistic=statistic)


# ---------------------------------------------------------------------------
# The measured/targeted set roles
# ---------------------------------------------------------------------------

_EMPTY: Mapping[MetricStatKey, float] = MappingProxyType({})
"""Immutable shared empty — safe as a constructor default (copied on build)."""


class MetricMap(Mapping[MetricStatKey, float]):
    """A frozen map from metric-statistic pairs to measured values.

    The shared implementation of the two set roles (:class:`MeasuredMetrics`,
    :class:`TargetedMetrics`). The ``Mapping`` mixin derives the read surface
    (``get``/``items``/``keys``/``values``/``__contains__``/``__iter__``/
    ``__len__``) from the three primitives below; there is no mutation
    surface — immutability is structural, not by convention. Equality is
    same-role only: a full set is never equal to a targeted subset even
    content-wise, and a map never equals a plain dict.

    Owned operations (each has live consumers; no operator overloads — set
    algebra stays on the pair sets as plain ``frozenset`` arithmetic):

    - :meth:`narrowed_to` — the single full→targeted narrowing primitive.
    - :meth:`covers` — the presence check (a pick-up's subset test).
    - :meth:`worst_of` — the per-key min aggregation across maps.
    """

    def __init__(self, data: Mapping[MetricStatKey, float] = _EMPTY) -> None:
        self._data: dict[MetricStatKey, float] = dict(data)

    def __getitem__(self, key: MetricStatKey) -> float:
        return self._data[key]

    def __iter__(self) -> Iterator[MetricStatKey]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._data == other._data

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"

    def narrowed_to(self, pairs: Iterable[MetricStatKey]) -> TargetedMetrics:
        """The subset restricted to *pairs* — the narrowing primitive.

        Args:
            pairs: The wanted pair set (a pair absent from this map is
                simply omitted from the result).

        Returns:
            The narrowed map as the targeted role.
        """
        wanted = frozenset(pairs)
        return TargetedMetrics({k: v for k, v in self._data.items() if k in wanted})

    def covers(self, pairs: Iterable[MetricStatKey]) -> bool:
        """Whether every pair in *pairs* is present in this map.

        Args:
            pairs: The required pair set (empty vacuously covered).
        """
        return all(pair in self._data for pair in pairs)

    @staticmethod
    def worst_of(maps: Iterable[TargetedMetrics]) -> TargetedMetrics:
        """The per-key minimum across *maps* (the worst-chunk aggregation).

        A pair present on some maps still aggregates over those maps; a pair
        present on none is absent from the result.

        Args:
            maps: The per-chunk targeted maps.

        Returns:
            The per-key minima as the targeted role.
        """
        mins: dict[MetricStatKey, float] = {}
        for metrics in maps:
            for key, value in metrics.items():
                mins[key] = min(mins[key], value) if key in mins else value
        return TargetedMetrics(mins)


class MeasuredMetrics(MetricMap):
    """The full measured set — one measurement's every metric and statistic.

    The re-judging substrate: attempt, standalone-measure, and merged-output
    sidecars hold this role (a work directory re-judges attempts under new
    targets without re-measuring).
    """


class TargetedMetrics(MetricMap):
    """A subset narrowed to a pair set — the judging/presentation basis.

    Search runs narrow to the configured targets' identities; fixed runs
    narrow to :data:`FIXED_COMPARISON_PAIRS`. The winner sidecar and the
    comparison rows hold this role.
    """


# ---------------------------------------------------------------------------
# Pydantic boundary aliases (typed maps in memory, canonical strings on disk)
# ---------------------------------------------------------------------------

def _wrap_role(raw: object, role: type[MetricMap]) -> object:
    """The shared BeforeValidator body: string keys parse, mappings wrap."""
    if isinstance(raw, role):
        return raw
    if isinstance(raw, Mapping):
        return role({
            MetricStatKey.parse(key) if isinstance(key, str) else key: float(value)
            for key, value in raw.items()
        })
    return raw


def _parse_measured_metrics(raw: object) -> object:
    """BeforeValidator for :data:`MeasuredMetricsYaml`."""
    return _wrap_role(raw, MeasuredMetrics)


def _parse_targeted_metrics(raw: object) -> object:
    """BeforeValidator for :data:`TargetedMetricsYaml`."""
    return _wrap_role(raw, TargetedMetrics)


def _dump_metric_map(mapping: Mapping[MetricStatKey, float]) -> dict[str, float]:
    """PlainSerializer shared by both roles: typed keys become canonical strings."""
    return {key.sidecar_key(): value for key, value in mapping.items()}


MeasuredMetricsYaml = Annotated[
    MeasuredMetrics,
    BeforeValidator(_parse_measured_metrics),
    PlainSerializer(_dump_metric_map),
]
"""The full measured set as a pydantic field: the role instance in memory,
canonical ``vmaf_median`` string keys on disk."""

TargetedMetricsYaml = Annotated[
    TargetedMetrics,
    BeforeValidator(_parse_targeted_metrics),
    PlainSerializer(_dump_metric_map),
]
"""The targeted subset as a pydantic field: the role instance in memory,
canonical ``vmaf_median`` string keys on disk (both roles dump identically)."""


# ---------------------------------------------------------------------------
# The fixed comparison narrower
# ---------------------------------------------------------------------------

FIXED_COMPARISON_PAIRS: tuple[MetricStatKey, ...] = tuple(
    MetricStatKey(metric=metric, statistic=statistic)
    for metric in MetricType
    for statistic in (StatisticType.P10, StatisticType.MEDIAN)
)
"""The fixed-mode compared pairs — every metric × {P10, MEDIAN}, canonical
metric-major order (stable table columns; the stat rank follows the closed
set's declaration order). The sole narrower authority for fixed runs:
shrinking the compared metric dimension is an edit to this constant alone
(which metrics get measured remains ``measurement.metrics``' job)."""


# ---------------------------------------------------------------------------
# Flattening (the nested per-metric form → the flat measured set)
# ---------------------------------------------------------------------------

def flatten_metric_stats(metrics: PerMetricStats) -> MeasuredMetrics:
    """Flatten per-metric statistics into the flat measured set.

    Owns the single cast over the ``MetricStats`` TypedDict (its ``.items()``
    values type as object to ty) and the single definition of the
    ``{metric}_{stat}`` key construction from the nested form.
    """
    return MeasuredMetrics({
        MetricStatKey(
            metric    = metric,
            statistic = StatisticType(stat),
        ): value
        for metric, stats in metrics.items()
        for stat, value in cast(dict[str, float], stats).items()
    })
