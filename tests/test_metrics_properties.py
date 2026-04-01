"""Property-based tests for pyqenc/metrics.py.

Each test is tagged with the feature and property it validates.
Run with: uv run python -m pytest tests/test_metrics_properties.py
"""

# Feature: pipeline-metrics-report, Property 6: YAML serialization round-trip

from __future__ import annotations

import math
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.metrics import (
    AttemptStats,
    ConvergenceSection,
    ConvergenceStats,
    PipelineMetrics,
    TimeDistribution,
    TimeEntry,
    TimeKey,
    YamlMetricsCollector,
    _ConvergenceAccumulator,
    _compute_convergence,
    _update_accumulator,
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_st_datetime = st.from_regex(
    r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]) "
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d",
    fullmatch=True,
)

_st_percent = st.from_regex(r"\d{1,3}\.\d%", fullmatch=True)

_st_duration = st.one_of(
    st.from_regex(r"[0-9]{2}:[0-5][0-9]:[0-5][0-9]", fullmatch=True),
    st.from_regex(r"[1-9][0-9]*d [0-9]{2}:[0-5][0-9]:[0-5][0-9]", fullmatch=True),
)

_st_category = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="._"),
    min_size=1,
    max_size=40,
)

_st_strategy_name = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="+_-"),
    min_size=1,
    max_size=40,
)


@st.composite
def _st_time_entry(draw: st.DrawFn) -> TimeEntry:
    return TimeEntry(
        category=draw(_st_category),
        seconds=draw(st.integers(min_value=0, max_value=10_000_000)),
        duration=draw(_st_duration),
        percent=draw(_st_percent),
    )


@st.composite
def _st_attempt_stats(draw: st.DrawFn) -> AttemptStats:
    mn = draw(st.integers(min_value=1, max_value=100))
    mx = draw(st.integers(min_value=mn, max_value=200))
    return AttemptStats(
        total=draw(st.integers(min_value=mn, max_value=10_000)),
        min=mn,
        avg=round(draw(st.floats(min_value=mn, max_value=mx, allow_nan=False)), 1),
        max=mx,
        stddev=round(draw(st.floats(min_value=0.0, max_value=50.0, allow_nan=False)), 1),
    )


@st.composite
def _st_convergence_stats(draw: st.DrawFn) -> ConvergenceStats:
    return ConvergenceStats(
        strategy=draw(_st_strategy_name),
        chunks=draw(st.integers(min_value=1, max_value=1000)),
        attempts=draw(_st_attempt_stats()),
    )


@st.composite
def _st_time_distribution(draw: st.DrawFn) -> TimeDistribution:
    entries = draw(st.lists(_st_time_entry(), min_size=0, max_size=15))
    return TimeDistribution(
        seconds=draw(st.integers(min_value=0, max_value=10_000_000)),
        duration=draw(_st_duration),
        breakdown=entries,
    )


@st.composite
def _st_convergence_section(draw: st.DrawFn) -> ConvergenceSection:
    strategies = draw(st.lists(_st_convergence_stats(), min_size=1, max_size=10))
    return ConvergenceSection(strategies=strategies)


@st.composite
def _st_pipeline_metrics(draw: st.DrawFn) -> PipelineMetrics:
    convergence = draw(st.one_of(st.none(), _st_convergence_section()))
    return PipelineMetrics(
        time_distribution=draw(_st_time_distribution()),
        convergence=convergence,
    )


def _make_yaml_collector(tmp_path: Path) -> YamlMetricsCollector:
    return YamlMetricsCollector(work_dir=tmp_path)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize(metrics: PipelineMetrics) -> str:
    data = {"pipeline_metrics": metrics.model_dump()}
    return yaml.dump(data, default_flow_style=False, allow_unicode=True)


def _deserialize(text: str) -> PipelineMetrics:
    raw = yaml.safe_load(text)
    return PipelineMetrics.model_validate(raw["pipeline_metrics"])


# ---------------------------------------------------------------------------
# Property 6: YAML serialization round-trip
# ---------------------------------------------------------------------------


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(metrics=_st_pipeline_metrics())
def test_yaml_round_trip(metrics: PipelineMetrics) -> None:
    """Property 6: YAML serialization round-trip.

    Validates: Requirements 5.1, 5.2, 5.3, 5.4
    # Feature: pipeline-metrics-report, Property 6: YAML serialization round-trip
    """
    restored = _deserialize(_serialize(metrics))

    td_orig = metrics.time_distribution
    td_rest = restored.time_distribution
    assert td_rest.seconds  == td_orig.seconds
    assert td_rest.duration == td_orig.duration
    assert len(td_rest.breakdown) == len(td_orig.breakdown)
    for orig_e, rest_e in zip(td_orig.breakdown, td_rest.breakdown):
        assert rest_e.category == orig_e.category
        assert rest_e.seconds  == orig_e.seconds
        assert rest_e.duration == orig_e.duration
        assert rest_e.percent  == orig_e.percent

    assert (restored.convergence is None) == (metrics.convergence is None)
    if metrics.convergence is not None and restored.convergence is not None:
        cv_orig = metrics.convergence
        cv_rest = restored.convergence
        assert len(cv_rest.strategies) == len(cv_orig.strategies)
        for orig_s, rest_s in zip(cv_orig.strategies, cv_rest.strategies):
            assert rest_s.strategy == orig_s.strategy
            assert rest_s.chunks   == orig_s.chunks
            assert rest_s.attempts.total  == orig_s.attempts.total
            assert rest_s.attempts.min    == orig_s.attempts.min
            assert rest_s.attempts.max    == orig_s.attempts.max
            assert abs(rest_s.attempts.avg   - orig_s.attempts.avg)   < 1e-9
            assert abs(rest_s.attempts.stddev - orig_s.attempts.stddev) < 1e-9


# ---------------------------------------------------------------------------
# Property 5: Convergence stats math
# ---------------------------------------------------------------------------


@st.composite
def _st_attempt_sequence(draw: st.DrawFn) -> list[int]:
    return draw(st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=100))


def _population_stddev(values: list[int]) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


@settings(max_examples=200)
@given(counts=_st_attempt_sequence())
def test_convergence_stats_math(counts: list[int]) -> None:
    """Property 5: Convergence stats math.

    Validates: Requirements 4.2, 4.1a
    # Feature: pipeline-metrics-report, Property 5: Convergence stats math
    """
    strategy = "slow+h265-aq"
    acc = _ConvergenceAccumulator()
    for c in counts:
        _update_accumulator(acc, c)

    result = _compute_convergence({strategy: acc})
    assert result is not None
    stats = result[0]
    assert stats.strategy        == strategy
    assert stats.chunks          == len(counts)
    assert stats.attempts.total  == sum(counts)
    assert stats.attempts.min    == min(counts)
    assert stats.attempts.max    == max(counts)
    assert abs(stats.attempts.avg   - sum(counts) / len(counts)) < 0.1
    assert abs(stats.attempts.stddev - _population_stddev(counts)) < 0.1


@settings(max_examples=100)
@given(
    strategy_a=_st_attempt_sequence(),
    strategy_b=_st_attempt_sequence(),
)
def test_convergence_stats_multi_strategy_sorted(
    strategy_a: list[int],
    strategy_b: list[int],
) -> None:
    """Results must be sorted by strategy name (Req 4.2)."""
    accumulators: dict[str, _ConvergenceAccumulator] = {
        "z-strategy": _ConvergenceAccumulator(),
        "a-strategy": _ConvergenceAccumulator(),
    }
    for c in strategy_a:
        _update_accumulator(accumulators["z-strategy"], c)
    for c in strategy_b:
        _update_accumulator(accumulators["a-strategy"], c)

    result = _compute_convergence(accumulators)
    assert result is not None
    assert result[0].strategy == "a-strategy"
    assert result[1].strategy == "z-strategy"


def test_convergence_stats_empty_returns_none() -> None:
    """_compute_convergence must return None when all accumulators are empty (Req 4.4)."""
    assert _compute_convergence({"s": _ConvergenceAccumulator()}) is None
    assert _compute_convergence({}) is None


@settings(max_examples=100)
@given(counts=_st_attempt_sequence())
def test_convergence_stats_resume_from_yaml(counts: list[int]) -> None:
    """Property 5 (resume): Welford state restored from persisted mean/stddev
    must produce identical results to a fresh run.

    Validates: Requirements 4.2, 4.1a
    """
    strategy = "slow+h265-aq"

    acc_fresh = _ConvergenceAccumulator()
    for c in counts:
        _update_accumulator(acc_fresh, c)
    result_fresh = _compute_convergence({strategy: acc_fresh})
    assert result_fresh is not None
    stats_fresh = result_fresh[0]

    persisted_stddev = 0.0 if acc_fresh.n == 1 else math.sqrt(acc_fresh.welford_M2 / acc_fresh.n)
    acc_resumed = _ConvergenceAccumulator(
        n=acc_fresh.n,
        total=acc_fresh.total,
        min=acc_fresh.min,
        max=acc_fresh.max,
        welford_mean=acc_fresh.welford_mean,
        welford_M2=persisted_stddev ** 2 * acc_fresh.n,
    )

    result_resumed = _compute_convergence({strategy: acc_resumed})
    assert result_resumed is not None
    stats_resumed = result_resumed[0]

    assert stats_resumed.chunks          == stats_fresh.chunks
    assert stats_resumed.attempts.total  == stats_fresh.attempts.total
    assert stats_resumed.attempts.min    == stats_fresh.attempts.min
    assert stats_resumed.attempts.max    == stats_fresh.attempts.max
    assert abs(stats_resumed.attempts.avg   - stats_fresh.attempts.avg)   < 1e-6
    assert abs(stats_resumed.attempts.stddev - stats_fresh.attempts.stddev) < 1e-6


# ---------------------------------------------------------------------------
# Property 1: Time accumulation round-trip
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    key=st.sampled_from(list(TimeKey)),
    durations=st.lists(
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1, max_size=50,
    ),
)
def test_time_accumulation_round_trip(key: TimeKey, durations: list[float]) -> None:
    """Property 1: Time accumulation round-trip.

    Validates: Requirements 2.1, 2.2, 2.2a
    # Feature: pipeline-metrics-report, Property 1: Time accumulation round-trip
    """
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for d in durations:
            collector._time_accum[key] += d

        expected = sum(durations)
        tol = max(1e-9, abs(expected) * 1e-9)
        assert abs(collector._time_accum[key] - expected) <= tol


# ---------------------------------------------------------------------------
# Property 2: Time distribution math
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    time_map=st.fixed_dictionaries({
        key: st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        for key in TimeKey
    }),
)
def test_time_distribution_math(time_map: dict[TimeKey, float]) -> None:
    """Property 2: Time distribution math.

    Validates: Requirements 2.3, 2.5
    # Feature: pipeline-metrics-report, Property 2: Time distribution math
    """
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for key, elapsed in time_map.items():
            if elapsed > 0:
                collector._time_accum[key] = elapsed

        collector.flush()

        raw = yaml.safe_load((Path(_tmp) / "metrics.yaml").read_text(encoding="utf-8"))
        td  = raw["pipeline_metrics"]["time_distribution"]

        total_secs = td["seconds"]
        breakdown  = td["breakdown"]

        # Only non-zero categories appear in breakdown (zeros are omitted)
        present_categories = {e["category"] for e in breakdown}
        expected_nonzero   = {k.value for k in TimeKey if int(round(collector._time_accum[k])) > 0}
        assert present_categories == expected_nonzero
        assert total_secs == int(round(sum(collector._time_accum.values())))

        for entry in breakdown:
            secs    = entry["seconds"]
            pct     = float(entry["percent"].rstrip("%"))
            exp_pct = secs / total_secs * 100 if total_secs > 0 else 0.0
            assert abs(pct - exp_pct) < 0.15


# ---------------------------------------------------------------------------
# Property 3: Breakdown sorted descending
# ---------------------------------------------------------------------------


@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@given(
    time_map=st.fixed_dictionaries({
        key: st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        for key in TimeKey
    }),
)
def test_breakdown_sorted_descending(time_map: dict[TimeKey, float]) -> None:
    """Property 3: Breakdown sorted descending.

    Validates: Requirements 2.6
    # Feature: pipeline-metrics-report, Property 3: Breakdown sorted descending
    """
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for key, elapsed in time_map.items():
            if elapsed > 0:
                collector._time_accum[key] = elapsed
        collector.flush()

        raw      = yaml.safe_load((Path(_tmp) / "metrics.yaml").read_text(encoding="utf-8"))
        secs_lst = [e["seconds"] for e in raw["pipeline_metrics"]["time_distribution"]["breakdown"]]
        assert secs_lst == sorted(secs_lst, reverse=True)

