"""Unit tests for the metric-statistic identity type (unified-quality-summaries
Req 7 base; metric-stat-identity spec).

``MetricStatKey`` is the single naming authority for metric-statistic keys:
the persistence and CLI key forms, the display form, tolerant parse, and the
pydantic role-alias boundary that keeps typed maps in memory and canonical
strings on disk. Each test names the contract it guards.
"""
# CHerSun 2026

from dataclasses import FrozenInstanceError

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from pyqenc.models import QualityTarget
from pyqenc.quality_metrics import (
    MeasuredMetrics,
    MeasuredMetricsYaml,
    MetricStatKey,
    MetricType,
    StatisticType,
    TargetedMetrics,
    TargetedMetricsYaml,
)


class _AttemptProbe(BaseModel):
    """Minimal model exercising the measured-role alias on a pydantic field."""

    model_config = {"arbitrary_types_allowed": True}

    metrics: MeasuredMetricsYaml = MeasuredMetrics()


class _WinnerProbe(BaseModel):
    """Minimal model exercising the targeted-role alias on a pydantic field."""

    model_config = {"arbitrary_types_allowed": True}

    metrics: TargetedMetricsYaml = TargetedMetrics()


ALL_KEYS: list[MetricStatKey] = [
    MetricStatKey(metric=metric, statistic=statistic)
    for metric in MetricType
    for statistic in StatisticType
]


class TestIdentity:
    """The identity's key forms and frozenness (Req 7.1–7.2)."""

    @given(
        metric=st.sampled_from(MetricType),
        statistic=st.sampled_from(StatisticType),
    )
    def test_key_forms_round_trip_through_parse(self, metric: MetricType, statistic: StatisticType) -> None:
        """Both spellings of the identity — the persistence form and the
        CLI form — parse back to the identity they were derived from."""
        key = MetricStatKey(metric=metric, statistic=statistic)
        assert MetricStatKey.parse(key.sidecar_key()) == key
        assert MetricStatKey.parse(key.cli_key()) == key

    def test_display_distinguishes_statistics_within_a_metric(self) -> None:
        """Display forms never collide across one metric's statistics — the
        plot labels stay unambiguous."""
        for metric in MetricType:
            displays = {
                MetricStatKey(metric=metric, statistic=statistic).display()
                for statistic in StatisticType
            }
            assert len(displays) == len(StatisticType)

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


class TestRoleAliases:
    """The pydantic role-alias boundary: typed maps in memory, canonical
    strings on disk (Req 2.3)."""

    def test_validation_yields_the_role_instances(self) -> None:
        """String-keyed payloads validate into the field's role instance —
        the full-set role for measured fields, the narrowed role for
        targeted ones."""
        attempt = _AttemptProbe.model_validate(
            {"metrics": {"vmaf_median": 96.0, "psnr_p10": 43}}
        )
        assert isinstance(attempt.metrics, MeasuredMetrics)
        assert attempt.metrics[MetricStatKey.parse("vmaf_median")] == 96.0
        assert attempt.metrics[MetricStatKey.parse("psnr-p10")] == 43.0

        winner = _WinnerProbe.model_validate({"metrics": {"vmaf-median": 96.0}})
        assert isinstance(winner.metrics, TargetedMetrics)
        assert winner.metrics[MetricStatKey.parse("vmaf_median")] == 96.0

    def test_save_load_round_trip_preserves_values_and_identities(self) -> None:
        """A save→load cycle preserves the measured values and pair
        identities (Req 6.3) — both roles serialize identically."""
        probe = _AttemptProbe.model_validate(
            {"metrics": {"vmaf_median": 96.0, "ssim_p10": 96.5}}
        )
        reloaded = _AttemptProbe.model_validate(
            yaml.safe_load(yaml.safe_dump(probe.model_dump()))
        )
        assert reloaded == probe
        assert reloaded.metrics == probe.metrics

    def test_non_mapping_rejected(self) -> None:
        """A non-mapping payload fails pydantic validation, not silently."""
        with pytest.raises(ValidationError):
            _AttemptProbe.model_validate({"metrics": 5})


class TestQualityTargetIdentity:
    """The target fold: the identity is the pair, validated at construction
    (Req 5.1–5.2, Req 12.2)."""

    def test_parse_yields_the_typed_identity(self) -> None:
        target = QualityTarget.parse("vmaf-med:96.0")
        assert target.identity == MetricStatKey(
            metric=MetricType.VMAF, statistic=StatisticType.MEDIAN,
        )
        assert target.value == 96.0

    def test_construction_rejects_unknown_statistic(self) -> None:
        """A statistic outside the closed set fails at construction — never
        at first use."""
        with pytest.raises(ValidationError):
            QualityTarget(
                identity={"metric": "vmaf", "statistic": "p99"},  # type: ignore[arg-type]
                value=90.0,
            )

    def test_construction_rejects_unknown_metric(self) -> None:
        """A metric outside the closed set fails at construction."""
        with pytest.raises(ValidationError):
            QualityTarget(
                identity={"metric": "vmaf2", "statistic": "min"},  # type: ignore[arg-type]
                value=90.0,
            )
