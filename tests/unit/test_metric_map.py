"""Unit tests for the measured/targeted map roles (metric-stat-identity spec,
Req 2).

``MetricMap`` is the frozen ``Mapping`` value class; ``MeasuredMetrics`` and
``TargetedMetrics`` are its zero-behavior roles; ``narrowed_to`` / ``covers`` /
``worst_of`` are its owned operations. Each test names the contract it guards.
"""
# CHerSun 2026

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pyqenc.quality_metrics import (
    FIXED_COMPARISON_PAIRS,
    MeasuredMetrics,
    MetricMap,
    MetricStatKey,
    MetricType,
    StatisticType,
    TargetedMetrics,
)

_PAIR_STRATEGY = st.builds(
    MetricStatKey,
    metric=st.sampled_from(MetricType),
    statistic=st.sampled_from(StatisticType),
)
_MAPS_STRATEGY = st.dictionaries(_PAIR_STRATEGY, st.floats(min_value=0.0, max_value=100.0))


class TestNarrowing:
    """The full→targeted narrowing primitive (Req 3.1)."""

    def test_happy_path_narrows_a_representative_full_set(self) -> None:
        """Narrowing keeps exactly the wanted pairs' values and returns the
        targeted role."""
        full = MeasuredMetrics({
            MetricStatKey.parse("vmaf_min"):    90.0,
            MetricStatKey.parse("vmaf_p10"):    91.0,
            MetricStatKey.parse("vmaf_median"): 95.5,
            MetricStatKey.parse("ssim_min"):    0.97,
        })
        narrowed = full.narrowed_to(FIXED_COMPARISON_PAIRS)
        assert isinstance(narrowed, TargetedMetrics)
        assert set(narrowed) == {
            MetricStatKey.parse("vmaf_p10"),
            MetricStatKey.parse("vmaf_median"),
        }
        assert narrowed[MetricStatKey.parse("vmaf_median")] == 95.5

    @given(data=_MAPS_STRATEGY)
    def test_narrowed_keys_are_a_subset_of_the_full_keys(self, data: dict[MetricStatKey, float]) -> None:
        """A narrowed map never carries a pair its source lacked."""
        assume_pairs = list(data)
        full = MeasuredMetrics(data)
        narrowed = full.narrowed_to(assume_pairs)
        assert set(narrowed) <= set(full)

    @given(data=_MAPS_STRATEGY)
    def test_narrowing_to_every_own_key_returns_the_same_content(self, data: dict[MetricStatKey, float]) -> None:
        """Narrowing is the identity when the pair set covers the map."""
        full = MeasuredMetrics(data)
        narrowed = full.narrowed_to(list(full))
        assert dict(narrowed) == dict(full)

    @given(data=_MAPS_STRATEGY)
    def test_wanted_pairs_absent_from_the_map_are_omitted(self, data: dict[MetricStatKey, float]) -> None:
        """A wanted pair the map lacks is omitted — narrowing never invents
        values."""
        full = MeasuredMetrics(data)
        wanted = [*full, MetricStatKey(metric=MetricType.VIF, statistic=StatisticType.STD)]
        narrowed = full.narrowed_to(wanted)
        assert set(narrowed) == set(full)

    def test_empty_pair_set_narrows_to_the_empty_map(self) -> None:
        full = MeasuredMetrics({MetricStatKey.parse("vmaf_min"): 90.0})
        assert len(full.narrowed_to([])) == 0


class TestCovers:
    """The presence check (Req 7.2's pick-up subset test)."""

    @given(data=_MAPS_STRATEGY)
    def test_covers_agrees_with_subset_checks(self, data: dict[MetricStatKey, float]) -> None:
        """``covers(pairs)`` is exactly ``all(pair in map)``."""
        metrics = MeasuredMetrics(data)
        pairs = list(data)[:3]
        assert metrics.covers(pairs) == all(pair in metrics for pair in pairs)

    def test_covers_empty_is_vacuously_true(self) -> None:
        assert MeasuredMetrics().covers([]) is True

    def test_covers_missing_pair_is_false(self) -> None:
        metrics = MeasuredMetrics({MetricStatKey.parse("vmaf_min"): 90.0})
        assert metrics.covers([MetricStatKey.parse("vmaf-p10")]) is False


class TestWorstOf:
    """The per-key min aggregation (Req 7's optimization worst-chunk fold)."""

    @given(
        first=_MAPS_STRATEGY,
        second=_MAPS_STRATEGY,
    )
    def test_worst_of_is_the_per_key_minimum(
        self,
        first: dict[MetricStatKey, float],
        second: dict[MetricStatKey, float],
    ) -> None:
        """Across maps, every key resolves to the minimum value seen for it."""
        maps = [TargetedMetrics(first), TargetedMetrics(second)]
        worst = MetricMap.worst_of(maps)
        assert isinstance(worst, TargetedMetrics)
        for key in set(first) | set(second):
            values = [m[key] for m in maps if key in m]
            assert worst[key] == min(values)

    def test_worst_of_empty_input_is_the_empty_map(self) -> None:
        assert len(MetricMap.worst_of([])) == 0


class TestMapValueSemantics:
    """Immutability and role-distinct equality (Req 2.1)."""

    def test_mutation_is_rejected(self) -> None:
        """The map exposes no mutation surface — assignment raises."""
        metrics = MeasuredMetrics({MetricStatKey.parse("vmaf_min"): 90.0})
        with pytest.raises(TypeError):
            metrics[MetricStatKey.parse("vmaf_min")] = 1.0  # ty: ignore[invalid-assignment] — rejection is the test's subject

    def test_same_role_compares_by_content(self) -> None:
        left = TargetedMetrics({MetricStatKey.parse("vmaf_p10"): 91.0})
        right = TargetedMetrics({MetricStatKey.parse("vmaf-p10"): 91.0})
        assert left == right

    def test_cross_role_never_equal_even_content_wise(self) -> None:
        """A full set is never equal to a targeted subset — the roles stay
        distinct to the checker."""
        pairs = {MetricStatKey.parse("vmaf_p10"): 91.0}
        assert MeasuredMetrics(pairs) != TargetedMetrics(pairs)  # type: ignore[comparison-overlap]

    def test_map_never_equals_a_plain_dict(self) -> None:
        pairs = {MetricStatKey.parse("vmaf_p10"): 91.0}
        assert TargetedMetrics(pairs) != pairs  # type: ignore[comparison-overlap]
