"""The quality-metrics identity vocabulary — the leaf below the models layer.

Owns the metric vocabulary end to end: the metric and statistic types, the
metric-statistic pair, the measured/targeted set roles, the fixed comparison
pairs, and the per-metric log parsing (each metric's ``MetricInfo`` subclass
parses its own measurement output into plain per-frame floats — pandas and
the shared quantile step stay in the measurer). The module imports nothing
from ``pyqenc.models`` or ``pyqenc.quality`` — the quality target and the
sidecars embed these types, so the dependency direction is one-way
(everything imports this, this imports nothing of ours).
"""
# CHerSun 2026

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import (
    Annotated,
    cast,
)

import pandas as pd
from pydantic import BeforeValidator, PlainSerializer

# ---------------------------------------------------------------------------
# Metric descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricInfo(ABC):
    """Descriptor carrying all metric-specific properties.

    Single source of truth for normalization, display, quality search
    behaviour, and the parsing of the metric's own measurement output.
    Accessed via ``MetricType.PSNR.info`` etc. — one static singleton
    subclass instance per metric, never re-instantiated.

    Attributes:
        name:              Human-readable display name (e.g. ``"PSNR"``).
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
        log_extension:     The metric's measurement-output file extension
                           (``.log`` stats files or the ``.json`` VMAF report).
        std_cutoff_max:    Upper bound on the values entering the std statistic
                           (``None`` = every value); PSNR caps at its clip ceiling.
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
    log_extension:        str
    std_cutoff_max:       float | None

    @abstractmethod
    def parse_log(self, path: Path) -> list[float]:
        """Parse the metric's measurement output into per-frame raw values.

        Args:
            path: The metric's log/report file.

        Returns:
            Per-frame raw values in frame order (plain floats; the
            subsample-factor index mapping and the shared quantile step
            live in the measurer).
        """

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
# Per-metric log parsing (each subclass owns its own output format)
# ---------------------------------------------------------------------------

def _parse_psnr_line(line: str) -> float | None:
    """Parse one PSNR log line into its average value, or ``None``."""
    try:
        parts = line.split()
        parsed: dict[str, int | float] = {}
        for part in parts:
            if ":" in part:
                key, value = part.split(":", 1)
                parsed[key] = int(value) if key == "n" else float(value)
        if "n" in parsed and "psnr_avg" in parsed:
            return float(parsed["psnr_avg"])
        return None
    except ValueError, IndexError:
        return None


def _parse_ssim_line(line: str) -> float | None:
    """Parse one SSIM log line into its ``All`` value, or ``None``."""
    try:
        parts = line.split()
        parsed: dict[str, int | float] = {}
        for part in parts:
            if ":" in part:
                key, value = part.split(":", 1)
                parsed[key] = int(value) if key == "n" else float(value)
        if "n" in parsed and "All" in parsed:
            return float(parsed["All"])
        return None
    except ValueError, IndexError:
        return None


def _load_vmaf_frames(path: Path) -> list[dict[str, object]]:
    """Load and validate the ``frames`` array of a VMAF JSON report.

    Raises:
        ValueError: The file is not valid JSON or carries no frames.
        TypeError:  The JSON structure has wrong types.
    """
    try:
        with path.open("r") as fh:
            vmaf_data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in VMAF file {path}: {exc}") from exc

    frames = vmaf_data.get("frames") if isinstance(vmaf_data, dict) else None
    if not isinstance(frames, list):
        raise TypeError(f"VMAF 'frames' is not an array: {path}")
    if not frames:
        raise ValueError(f"VMAF 'frames' array is empty: {path}")
    return cast(list[dict[str, object]], frames)


@dataclass(frozen=True)
class _PsnrMetricInfo(MetricInfo):
    """PSNR: ffmpeg ``psnr`` stats-file lines (``n:1 psnr_avg:42.0 …``)."""

    def parse_log(self, path: Path) -> list[float]:
        """Parse the PSNR stats file into per-frame average PSNR values.

        Leading non-matching lines end the scan (the file is not a PSNR
        log); trailing junk after the first match is skipped.

        Raises:
            ValueError: No PSNR lines were found.
        """
        values: list[float] = []
        with path.open("r") as fh:
            for line in fh:
                parsed = _parse_psnr_line(line)
                if parsed is not None:
                    values.append(parsed)
                elif not values:
                    break
        if not values:
            raise ValueError(f"Not a PSNR log file: {path}")
        return values


@dataclass(frozen=True)
class _SsimMetricInfo(MetricInfo):
    """SSIM: ffmpeg ``ssim`` stats-file lines (the ``All`` column)."""

    def parse_log(self, path: Path) -> list[float]:
        """Parse the SSIM stats file into per-frame ``All`` values.

        Leading non-matching lines end the scan (the file is not an SSIM
        log); trailing junk after the first match is skipped.

        Raises:
            ValueError: No SSIM lines were found.
        """
        values: list[float] = []
        with path.open("r") as fh:
            for line in fh:
                parsed = _parse_ssim_line(line)
                if parsed is not None:
                    values.append(parsed)
                elif not values:
                    break
        if not values:
            raise ValueError(f"Not an SSIM log file: {path}")
        return values


@dataclass(frozen=True)
class _VmafMetricInfo(MetricInfo):
    """VMAF: the libvmaf JSON report's per-frame ``vmaf`` scores."""

    def parse_log(self, path: Path) -> list[float]:
        """Parse the VMAF JSON report into per-frame VMAF scores.

        Raises:
            ValueError: No valid frame entries carry a ``vmaf`` score.
            TypeError:  The JSON structure has wrong types.
        """
        values: list[float] = []
        for frame in _load_vmaf_frames(path):
            if not isinstance(frame, dict):
                raise TypeError(f"VMAF frame is not a dictionary: {path}")
            metrics = frame.get("metrics")
            vmaf    = metrics.get("vmaf") if isinstance(metrics, dict) else None
            if vmaf is None:
                raise ValueError(f"NOT a VMAF file: {path}")
            values.append(float(vmaf))
        return values


_VIF_SCALES: tuple[str, ...] = (
    "integer_vif_scale0",
    "integer_vif_scale1",
    "integer_vif_scale2",
    "integer_vif_scale3",
)


@dataclass(frozen=True)
class _VifMetricInfo(MetricInfo):
    """VIF: the four per-scale scores embedded in the VMAF JSON, averaged."""

    def parse_log(self, path: Path) -> list[float]:
        """Parse VIF per-frame scores from the VMAF JSON report.

        libvmaf embeds VIF when run with ``feature=name=vif``: each frame
        carries ``integer_vif_scale0`` … ``integer_vif_scale3`` (range 0–1,
        1.0 = lossless); the combined score is the average of the four.

        Raises:
            ValueError: A frame lacks the VIF scale data.
            TypeError:  The JSON structure has wrong types.
        """
        values: list[float] = []
        for frame in _load_vmaf_frames(path):
            if not isinstance(frame, dict):
                raise TypeError(f"VMAF frame is not a dictionary: {path}")
            metrics = frame.get("metrics", {})
            scales  = [metrics.get(s) for s in _VIF_SCALES] if isinstance(metrics, dict) else [None] * 4
            if any(scale is None for scale in scales):
                raise ValueError(
                    f"VIF scale data missing from frame {frame.get('frameNum')} in {path}. "
                    f"Ensure libvmaf was run with feature=name=vif."
                )
            values.append(sum(cast(list[float], scales)) / len(_VIF_SCALES))
        return values


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
    MetricType.VMAF: _VmafMetricInfo(
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
        log_extension     = ".json",
        std_cutoff_max    = None,
    ),
    MetricType.SSIM: _SsimMetricInfo(
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
        log_extension     = ".log",
        std_cutoff_max    = None,
    ),
    MetricType.PSNR: _PsnrMetricInfo(
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
        log_extension     = ".log",
        std_cutoff_max    = 100.0,
    ),
    MetricType.VIF: _VifMetricInfo(
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
        log_extension     = ".json",
        std_cutoff_max    = None,
    ),
}


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
