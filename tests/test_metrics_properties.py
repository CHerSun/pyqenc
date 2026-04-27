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
from hypothesis import assume

from pyqenc.metrics import (
    AttemptStats,
    ConvergenceStats,
    DottedEntry,
    DottedGroup,
    MetricKey,
    PipelineMetrics,
    TimeDistribution,
    TopLevelEntry,
    YamlMetricsCollector,
    _ConvergenceAccumulator,
    _compute_convergence,
    _last_dot_prefix,
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

_st_top_level_key = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)

_st_suffix = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="+_-"),
    min_size=1,
    max_size=20,
)


@st.composite
def _st_top_level_entry(draw: st.DrawFn) -> TopLevelEntry:
    return TopLevelEntry(
        key=draw(_st_top_level_key),
        seconds=draw(st.integers(min_value=0, max_value=10_000_000)),
        duration=draw(_st_duration),
        percent=draw(_st_percent),
    )


@st.composite
def _st_dotted_entry(draw: st.DrawFn, prefix: str) -> DottedEntry:
    suffix = draw(_st_suffix)
    return DottedEntry(
        key=f"{prefix}.{suffix}",
        seconds=draw(st.integers(min_value=0, max_value=10_000_000)),
        duration=draw(_st_duration),
        percent=draw(_st_percent),
    )


@st.composite
def _st_dotted_group(draw: st.DrawFn) -> tuple[str, DottedGroup]:
    prefix   = draw(_st_top_level_key)
    entries  = draw(st.lists(_st_dotted_entry(prefix), min_size=1, max_size=5))
    return prefix, DottedGroup(
        prefix_seconds=draw(st.integers(min_value=0, max_value=10_000_000)),
        prefix_duration=draw(_st_duration),
        breakdown=entries,
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
    top_level_entries = draw(st.lists(_st_top_level_entry(), min_size=0, max_size=10))
    dotted_pairs      = draw(st.lists(_st_dotted_group(), min_size=0, max_size=5))
    # Deduplicate prefixes (keep last occurrence)
    dotted: dict[str, DottedGroup] = {}
    for prefix, group in dotted_pairs:
        dotted[prefix] = group
    return TimeDistribution(
        total_seconds=draw(st.integers(min_value=0, max_value=10_000_000)),
        total_duration=draw(_st_duration),
        top_level=top_level_entries,
        dotted=dotted,
    )


@st.composite
def _st_pipeline_metrics(draw: st.DrawFn) -> PipelineMetrics:
    convergence = draw(st.one_of(
        st.none(),
        st.lists(_st_convergence_stats(), min_size=1, max_size=10),
    ))
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


@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@given(metrics=_st_pipeline_metrics())
def test_yaml_round_trip(metrics: PipelineMetrics) -> None:
    """Property 6: YAML serialization round-trip.

    Validates: Requirements 5.1, 5.2, 5.3, 5.4
    # Feature: pipeline-metrics-report, Property 6: YAML serialization round-trip
    """
    restored = _deserialize(_serialize(metrics))

    td_orig = metrics.time_distribution
    td_rest = restored.time_distribution
    assert td_rest.total_seconds  == td_orig.total_seconds
    assert td_rest.total_duration == td_orig.total_duration

    # top_level entries
    assert len(td_rest.top_level) == len(td_orig.top_level)
    for orig_e, rest_e in zip(td_orig.top_level, td_rest.top_level):
        assert rest_e.key      == orig_e.key
        assert rest_e.seconds  == orig_e.seconds
        assert rest_e.duration == orig_e.duration
        assert rest_e.percent  == orig_e.percent

    # dotted groups
    assert set(td_rest.dotted.keys()) == set(td_orig.dotted.keys())
    for prefix in td_orig.dotted:
        orig_g = td_orig.dotted[prefix]
        rest_g = td_rest.dotted[prefix]
        assert rest_g.prefix_seconds  == orig_g.prefix_seconds
        assert rest_g.prefix_duration == orig_g.prefix_duration
        assert len(rest_g.breakdown)  == len(orig_g.breakdown)
        for orig_e, rest_e in zip(orig_g.breakdown, rest_g.breakdown):
            assert rest_e.key      == orig_e.key
            assert rest_e.seconds  == orig_e.seconds
            assert rest_e.duration == orig_e.duration
            assert rest_e.percent  == orig_e.percent

    assert (restored.convergence is None) == (metrics.convergence is None)
    if metrics.convergence is not None and restored.convergence is not None:
        assert len(restored.convergence) == len(metrics.convergence)
        for orig_s, rest_s in zip(metrics.convergence, restored.convergence):
            assert rest_s.strategy == orig_s.strategy
            assert rest_s.chunks   == orig_s.chunks
            assert rest_s.attempts.total  == orig_s.attempts.total
            assert rest_s.attempts.min    == orig_s.attempts.min
            assert rest_s.attempts.max    == orig_s.attempts.max
            assert abs(rest_s.attempts.avg    - orig_s.attempts.avg)    < 1e-9
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
    key=st.sampled_from(list(MetricKey)),
    durations=st.lists(
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1, max_size=50,
    ),
)
def test_time_accumulation_round_trip(key: MetricKey, durations: list[float]) -> None:
    """Property 1: Time accumulation round-trip.

    Validates: Requirements 2.1, 2.2, 2.2a
    # Feature: pipeline-metrics-report, Property 1: Time accumulation round-trip
    """
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for d in durations:
            collector._store[key.value] = collector._store.get(key.value, 0.0) + d

        expected = sum(durations)
        tol = max(1e-9, abs(expected) * 1e-9)
        assert abs(collector._store[key.value] - expected) <= tol


# ---------------------------------------------------------------------------
# Property 2: Time distribution math
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    time_map=st.fixed_dictionaries({
        key: st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        for key in MetricKey
    }),
)
def test_time_distribution_math(time_map: dict[MetricKey, float]) -> None:
    """Property 2: Time distribution math.

    Validates: Requirements 2.3, 2.5
    # Feature: pipeline-metrics-report, Property 2: Time distribution math
    """
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for key, elapsed in time_map.items():
            if elapsed > 0:
                collector._store[key.value] = elapsed

        collector.flush()

        raw = yaml.safe_load((Path(_tmp) / "metrics.yaml").read_text(encoding="utf-8"))
        td  = raw["pipeline_metrics"]["time_distribution"]

        total_secs = td["total_seconds"]
        top_level  = td["top_level"]

        # Only non-zero top-level keys appear in top_level list (zeros are omitted)
        present_keys   = {e["key"] for e in top_level}
        expected_nonzero = {k.value for k in MetricKey if int(round(collector._store.get(k.value, 0.0))) > 0}
        assert present_keys == expected_nonzero
        assert total_secs == int(round(sum(collector._store.values())))

        for entry in top_level:
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
        for key in MetricKey
    }),
)
def test_breakdown_sorted_descending(time_map: dict[MetricKey, float]) -> None:
    """Property 3: Top-level list sorted descending.

    Validates: Requirements 2.6
    # Feature: pipeline-metrics-report, Property 3: Breakdown sorted descending
    """
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for key, elapsed in time_map.items():
            if elapsed > 0:
                collector._store[key.value] = elapsed
        collector.flush()

        raw      = yaml.safe_load((Path(_tmp) / "metrics.yaml").read_text(encoding="utf-8"))
        secs_lst = [e["seconds"] for e in raw["pipeline_metrics"]["time_distribution"]["top_level"]]
        assert secs_lst == sorted(secs_lst, reverse=True)



# ---------------------------------------------------------------------------
# Property 1 (metrics-two-tier): Prefix extraction is last-dot
# ---------------------------------------------------------------------------

# Feature: metrics-two-tier, Property 1: Prefix extraction is last-dot

_st_segment = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)


@st.composite
def _st_dotted_key(draw: st.DrawFn) -> str:
    """Generate a dotted key string with 1–3 dots and random alphanumeric segments."""
    num_dots = draw(st.integers(min_value=1, max_value=3))
    segments = draw(st.lists(_st_segment, min_size=num_dots + 1, max_size=num_dots + 1))
    return ".".join(segments)


@settings(max_examples=100)
@given(key=_st_dotted_key())
def test_last_dot_prefix_is_last_dot(key: str) -> None:
    """Property 1 (metrics-two-tier): Prefix extraction is last-dot.

    Validates: Requirements 1.4
    # Feature: metrics-two-tier, Property 1: Prefix extraction is last-dot
    """
    assert _last_dot_prefix(key) == key.rsplit(".", 1)[0]


# ---------------------------------------------------------------------------
# Property 2 (metrics-two-tier): Time accumulation is additive
# ---------------------------------------------------------------------------

# Feature: metrics-two-tier, Property 2: Time accumulation is additive

_st_any_key = st.one_of(
    # top-level key (no dot)
    _st_top_level_key,
    # dotted key (one dot)
    st.builds(lambda prefix, suffix: f"{prefix}.{suffix}", _st_top_level_key, _st_suffix),
)


@settings(max_examples=200, deadline=None)
@given(
    key=_st_any_key,
    durations=st.lists(
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=50,
    ),
)
def test_time_accumulation_is_additive(key: str, durations: list[float]) -> None:
    """Property 2 (metrics-two-tier): Time accumulation is additive.

    Validates: Requirements 2.2, 3.4
    # Feature: metrics-two-tier, Property 2: Time accumulation is additive
    """
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for d in durations:
            collector._store[key] = collector._store.get(key, 0.0) + d

        expected = sum(durations)
        tol = max(1e-9, abs(expected) * 1e-9)
        assert abs(collector._store[key] - expected) <= tol


# ---------------------------------------------------------------------------
# Property 3 (metrics-two-tier): Top-level percentages sum to 100%
# ---------------------------------------------------------------------------

# Feature: metrics-two-tier, Property 3: Top-level percentages sum to 100%


@st.composite
def _st_top_level_store(draw: st.DrawFn) -> dict[str, float]:
    """Generate a dict of top-level keys (no dots) with at least one non-zero value."""
    keys = draw(st.lists(_st_top_level_key, min_size=1, max_size=8, unique=True))
    values = draw(st.lists(
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=len(keys),
        max_size=len(keys),
    ))
    store = dict(zip(keys, values))
    # Ensure at least one non-zero value
    assume(any(v > 0.5 for v in store.values()))
    return store


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(store=_st_top_level_store())
def test_top_level_percentages_sum_to_100(store: dict[str, float]) -> None:
    """Property 3 (metrics-two-tier): Top-level percentages sum to 100%.

    Validates: Requirements 4.1, 4.4
    # Feature: metrics-two-tier, Property 3: Top-level percentages sum to 100%
    """
    with tempfile.TemporaryDirectory() as _tmp:
        tmp_path = Path(_tmp)
        collector = _make_yaml_collector(tmp_path)
        collector._store.update(store)
        collector.flush()

        raw = yaml.safe_load((tmp_path / "metrics.yaml").read_text(encoding="utf-8"))
        top_level = raw["pipeline_metrics"]["time_distribution"].get("top_level") or []

        total = sum(float(e["percent"].rstrip("%")) for e in top_level)
        # Each entry is rounded to 1 decimal place (±0.05%), so with up to 8
        # entries the worst-case accumulated rounding error is 8 × 0.05 = 0.4%.
        assert abs(total - 100.0) < 0.5, (
            f"Top-level percentages sum to {total:.2f}%, expected 100.0%"
        )


# ---------------------------------------------------------------------------
# Property 4 (metrics-two-tier): Dotted percentages sum to 100% per prefix
# ---------------------------------------------------------------------------

# Feature: metrics-two-tier, Property 4: Dotted percentages sum to 100% per prefix


@st.composite
def _st_dotted_store(draw: st.DrawFn) -> dict[str, float]:
    """Generate a dict of dotted keys grouped by prefix, at least one non-zero per group."""
    num_prefixes = draw(st.integers(min_value=1, max_value=4))
    store: dict[str, float] = {}
    for _ in range(num_prefixes):
        prefix   = draw(_st_top_level_key)
        suffixes = draw(st.lists(_st_suffix, min_size=2, max_size=5, unique=True))
        values   = draw(st.lists(
            st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
            min_size=len(suffixes),
            max_size=len(suffixes),
        ))
        group = {f"{prefix}.{s}": v for s, v in zip(suffixes, values)}
        # Ensure at least one non-zero value in this group
        assume(any(v > 0.5 for v in group.values()))
        store.update(group)
    return store


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(store=_st_dotted_store())
def test_dotted_percentages_sum_to_100_per_prefix(
    store: dict[str, float],
) -> None:
    """Property 4 (metrics-two-tier): Dotted percentages sum to 100% per prefix.

    Validates: Requirements 4.2, 4.5
    # Feature: metrics-two-tier, Property 4: Dotted percentages sum to 100% per prefix
    """
    with tempfile.TemporaryDirectory() as _tmp:
        tmp_path = Path(_tmp)
        collector = _make_yaml_collector(tmp_path)
        collector._store.update(store)
        collector.flush()

        raw    = yaml.safe_load((tmp_path / "metrics.yaml").read_text(encoding="utf-8"))
        dotted = raw["pipeline_metrics"]["time_distribution"].get("dotted") or {}

        for prefix, group in dotted.items():
            breakdown = group.get("breakdown") or []
            total = sum(float(e["percent"].rstrip("%")) for e in breakdown)
            # Each entry is rounded to 1 decimal place (±0.05%), so with up to 5
            # entries the worst-case accumulated rounding error is 5 × 0.05 = 0.25%.
            assert abs(total - 100.0) < 0.5, (
                f"Dotted prefix {prefix!r} percentages sum to {total:.2f}%, expected 100.0%"
            )


# ---------------------------------------------------------------------------
# Property 8 (metrics-two-tier): Top-level list sorted descending with no zeros
# ---------------------------------------------------------------------------

# Feature: metrics-two-tier, Property 8: Top-level list sorted descending with no zeros


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(store=_st_top_level_store())
def test_top_level_sorted_descending_no_zeros(
    store: dict[str, float],
) -> None:
    """Property 8 (metrics-two-tier): Top-level list sorted descending with no zeros.

    Validates: Requirements 5.2
    # Feature: metrics-two-tier, Property 8: Top-level list sorted descending with no zeros
    """
    with tempfile.TemporaryDirectory() as _tmp:
        tmp_path = Path(_tmp)
        collector = _make_yaml_collector(tmp_path)
        collector._store.update(store)
        collector.flush()

        raw       = yaml.safe_load((tmp_path / "metrics.yaml").read_text(encoding="utf-8"))
        top_level = raw["pipeline_metrics"]["time_distribution"].get("top_level") or []
        secs_list = [e["seconds"] for e in top_level]

        assert all(s > 0 for s in secs_list), "top_level must contain no zero-second entries"
        assert secs_list == sorted(secs_list, reverse=True), (
            "top_level must be sorted in descending order of seconds"
        )


# ---------------------------------------------------------------------------
# Property 9 (metrics-two-tier): Dotted breakdown sorted descending with no zeros
# ---------------------------------------------------------------------------

# Feature: metrics-two-tier, Property 9: Dotted breakdown sorted descending with no zeros


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(store=_st_dotted_store())
def test_dotted_breakdown_sorted_descending_no_zeros(
    store: dict[str, float],
) -> None:
    """Property 9 (metrics-two-tier): Dotted breakdown sorted descending with no zeros.

    Validates: Requirements 5.3
    # Feature: metrics-two-tier, Property 9: Dotted breakdown sorted descending with no zeros
    """
    with tempfile.TemporaryDirectory() as _tmp:
        tmp_path = Path(_tmp)
        collector = _make_yaml_collector(tmp_path)
        collector._store.update(store)
        collector.flush()

        raw    = yaml.safe_load((tmp_path / "metrics.yaml").read_text(encoding="utf-8"))
        dotted = raw["pipeline_metrics"]["time_distribution"].get("dotted") or {}

        for prefix, group in dotted.items():
            breakdown = group.get("breakdown") or []
            secs_list = [e["seconds"] for e in breakdown]
            assert all(s > 0 for s in secs_list), (
                f"Dotted prefix {prefix!r} breakdown must contain no zero-second entries"
            )
            assert secs_list == sorted(secs_list, reverse=True), (
                f"Dotted prefix {prefix!r} breakdown must be sorted in descending order of seconds"
            )


# ---------------------------------------------------------------------------
# Property 6 (metrics-two-tier): Resume restores accumulated store
# ---------------------------------------------------------------------------

# Feature: metrics-two-tier, Property 6: Resume restores accumulated store


@st.composite
def _st_mixed_store(draw: st.DrawFn) -> dict[str, float]:
    """Generate a MetricsStore with a mix of top-level and dotted keys.

    All values are >= 1.0 so they survive the integer-rounding zero-omission
    filter in _compute_top_level_entries / _compute_dotted_groups (YAML only
    persists entries where int(round(value)) >= 1).
    """
    top_level_keys = draw(st.lists(_st_top_level_key, min_size=0, max_size=4, unique=True))
    dotted_keys: list[str] = []
    num_prefixes = draw(st.integers(min_value=0, max_value=3))
    for _ in range(num_prefixes):
        prefix   = draw(_st_top_level_key)
        suffixes = draw(st.lists(_st_suffix, min_size=1, max_size=3, unique=True))
        for s in suffixes:
            dotted_keys.append(f"{prefix}.{s}")

    all_keys = top_level_keys + dotted_keys
    assume(len(all_keys) >= 1)

    # Use min_value=1.0 so int(round(v)) >= 1 — values survive the zero-omission filter
    values = draw(st.lists(
        st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=len(all_keys),
        max_size=len(all_keys),
    ))
    return dict(zip(all_keys, values))


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(store=_st_mixed_store())
def test_resume_restores_accumulated_store(store: dict[str, float]) -> None:
    """Property 6 (metrics-two-tier): Resume restores accumulated store.

    Validates: Requirements 5.6
    # Feature: metrics-two-tier, Property 6: Resume restores accumulated store
    """
    with tempfile.TemporaryDirectory() as _tmp:
        tmp_path = Path(_tmp)

        # Inject store into first collector and flush to disk
        collector = _make_yaml_collector(tmp_path)
        collector._store.update(store)
        collector.flush()

        # Construct a second collector pointing at the same work_dir — triggers _try_resume
        resumed = _make_yaml_collector(tmp_path)

        # Each key must be restored within integer-rounding tolerance (YAML persists int seconds)
        for key, original_value in store.items():
            assert key in resumed._store, f"Key {key!r} missing from resumed store"
            assert abs(resumed._store[key] - int(round(original_value))) <= 1, (
                f"Key {key!r}: resumed={resumed._store[key]}, "
                f"expected≈{int(round(original_value))} (original={original_value})"
            )
