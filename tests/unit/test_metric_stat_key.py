"""Unit tests for the metric-statistic identity type (unified-quality-summaries, Req 7).

``MetricStatKey`` is the single naming authority for metric-statistic keys:
canonical persistence form, display form, tolerant parse, and the pydantic
dict-key wiring that keeps typed keys in memory and canonical strings on
disk. Each test names the contract it guards.
"""
# CHerSun 2026

from dataclasses import FrozenInstanceError

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from pyqenc.models import QualityTarget
from pyqenc.quality import (
    MetricStatKey,
    MetricStatMap,
    MetricType,
    StatisticType,
)


class _Probe(BaseModel):
    """Minimal model exercising the dict-key alias on a pydantic field."""

    metrics: MetricStatMap = {}


ALL_KEYS: list[MetricStatKey] = [
    MetricStatKey(metric=metric, statistic=statistic)
    for metric in MetricType
    for statistic in StatisticType
]


class TestIdentity:
    """The canonical and display forms (Req 7.1–7.2)."""

    @pytest.mark.parametrize("key", ALL_KEYS, ids=lambda k: k.sidecar_key())
    def test_sidecar_key_is_the_underscore_form(self, key: MetricStatKey) -> None:
        """The persistence form is exactly ``{metric}_{statistic}``, lowercase tokens."""
        assert key.sidecar_key() == f"{key.metric.value}_{key.statistic.value}"

    @pytest.mark.parametrize("key", ALL_KEYS, ids=lambda k: k.sidecar_key())
    def test_display_uses_plot_vocabulary(self, key: MetricStatKey) -> None:
        """Display = upper-case metric + the plot label (percentiles as percents,
        min/max/std as words) — e.g. ``VMAF 50%``, ``PSNR Min``."""
        expected_stat = {
            StatisticType.MIN: "Min", StatisticType.P05: "5%", StatisticType.P10: "10%",
            StatisticType.P25: "25%", StatisticType.MEDIAN: "50%", StatisticType.P75: "75%",
            StatisticType.P90: "90%", StatisticType.P95: "95%", StatisticType.MAX: "Max",
            StatisticType.STD: "Std",
        }[key.statistic]
        assert key.display() == f"{key.metric.info.name} {expected_stat}"

    def test_keys_are_hashable_and_frozen(self) -> None:
        """The type works directly as a dict key and rejects mutation."""
        key = MetricStatKey(metric=MetricType.VMAF, statistic=StatisticType.MEDIAN)
        lookup = {key: 96.0}
        assert lookup[MetricStatKey(metric=MetricType.VMAF, statistic=StatisticType.MEDIAN)] == 96.0
        assert hash(key) == hash(MetricStatKey(metric=MetricType.VMAF, statistic=StatisticType.MEDIAN))
        with pytest.raises(FrozenInstanceError):
            key.statistic = StatisticType.P10  # ty: ignore[invalid-assignment] — rejection is the test's subject


class TestParse:
    """Tolerant input parsing (Req 7.1, Req 7.2)."""

    @pytest.mark.parametrize("key", ALL_KEYS, ids=lambda k: k.sidecar_key())
    def test_parse_round_trips_the_canonical_form(self, key: MetricStatKey) -> None:
        """``parse(sidecar_key(k)) == k`` for the whole closed cross-product."""
        assert MetricStatKey.parse(key.sidecar_key()) == key

    @given(
        metric=st.sampled_from(MetricType),
        statistic=st.sampled_from(StatisticType),
        separator=st.sampled_from(["-", "_"]),
        upper=st.booleans(),
    )
    def test_parse_ignores_separator_and_case(self, metric: MetricType, statistic: StatisticType, separator: str, upper: bool) -> None:
        """Dash/underscore and case never change the parsed identity."""
        token = f"{metric.value}{separator}{statistic.value}"
        if upper:
            token = token.upper()
        assert MetricStatKey.parse(token) == MetricStatKey(metric=metric, statistic=statistic)

    def test_parse_accepts_the_med_alias(self) -> None:
        """``med`` is the CLI alias for ``median``."""
        assert MetricStatKey.parse("vmaf-med") == MetricStatKey(
            metric=MetricType.VMAF, statistic=StatisticType.MEDIAN,
        )

    @pytest.mark.parametrize(
        "bad",
        ["vmaf", "", "  ", "vmaf-", "-median", "foo-median", "vmaf-p99", "vmaf-median-extra"],
    )
    def test_parse_fails_loudly(self, bad: str) -> None:
        """No separator, unknown metric, or unknown statistic raise — never a
        silent round-trip of a mistyped key."""
        with pytest.raises(ValueError, match="Invalid (metric-statistic key|metric|statistic)"):
            MetricStatKey.parse(bad)


class TestMetricStatMap:
    """The pydantic dict-key alias: typed in memory, canonical on disk (Req 7.1)."""

    def test_validation_yields_typed_keys(self) -> None:
        """Canonical string keys validate into ``MetricStatKey`` map keys."""
        probe = _Probe.model_validate({"metrics": {"vmaf_median": 96.0, "psnr_p10": 43.0}})
        assert set(probe.metrics) == {
            MetricStatKey(metric=MetricType.VMAF, statistic=StatisticType.MEDIAN),
            MetricStatKey(metric=MetricType.PSNR, statistic=StatisticType.P10),
        }
        assert probe.metrics[MetricStatKey(metric=MetricType.VMAF, statistic=StatisticType.MEDIAN)] == 96.0

    def test_dump_yields_canonical_strings(self) -> None:
        """``model_dump()`` re-keys to the canonical sidecar form."""
        probe = _Probe.model_validate({"metrics": {"vmaf-median": 96.0}})
        assert probe.model_dump() == {"metrics": {"vmaf_median": 96.0}}

    def test_yaml_round_trip(self) -> None:
        """The on-disk form is canonical keys; a load→dump→load cycle is stable."""
        probe = _Probe.model_validate({"metrics": {"vmaf_median": 96.0, "ssim_p10": 96.5}})
        as_text = yaml.safe_dump(probe.model_dump())
        assert "vmaf_median" in as_text
        reloaded = _Probe.model_validate(yaml.safe_load(as_text))
        assert reloaded == probe

    def test_non_mapping_rejected(self) -> None:
        """A non-mapping payload fails pydantic validation, not silently."""
        with pytest.raises(ValidationError):
            _Probe.model_validate({"metrics": 5})


class TestQualityTargetKey:
    """The typed view over ``QualityTarget``'s string fields."""

    def test_key_matches_the_target_identity(self) -> None:
        target = QualityTarget.parse("vmaf-med:96.0")
        assert target.key() == MetricStatKey(
            metric=MetricType.VMAF, statistic=StatisticType.MEDIAN,
        )

    def test_key_fails_loudly_on_bad_fields(self) -> None:
        """Fields are validated at parse, not construction — ``key()`` is where
        a bad value fails instead of leaking a mistyped key."""
        target = QualityTarget(metric="vmaf", statistic="p99", value=90.0)
        with pytest.raises(ValueError, match="p99"):
            target.key()
