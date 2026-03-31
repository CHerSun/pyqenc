"""Property-based tests for pyqenc/metrics.py.

Each test is tagged with the feature and property it validates.
Run with: uv run python -m pytest tests/test_metrics_properties.py
"""

# Feature: pipeline-metrics-report, Property 6: YAML serialization round-trip

from __future__ import annotations

import re

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from pyqenc.metrics import (
    AttemptStats,
    ConvergenceSection,
    ConvergenceStats,
    PipelineMetrics,
    SpaceDistribution,
    SpaceEntry,
    TimeDistribution,
    TimeEntry,
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

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

_st_size_gb = st.from_regex(r"\d{1,6}\.\d{2} GB", fullmatch=True)

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
def _st_space_entry(draw: st.DrawFn) -> SpaceEntry:
    return SpaceEntry(
        category=draw(_st_category),
        size=draw(_st_size_gb),
        percent=draw(_st_percent),
    )


@st.composite
def _st_attempt_stats(draw: st.DrawFn) -> AttemptStats:
    mn  = draw(st.integers(min_value=1, max_value=100))
    mx  = draw(st.integers(min_value=mn, max_value=200))
    return AttemptStats(
        total=draw(st.integers(min_value=mn, max_value=10_000)),
        min=mn,
        mean=round(draw(st.floats(min_value=mn, max_value=mx, allow_nan=False)), 1),
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
        updated_at=draw(_st_datetime),
        total_seconds=draw(st.integers(min_value=0, max_value=10_000_000)),
        total_duration=draw(_st_duration),
        breakdown=entries,
    )


@st.composite
def _st_space_distribution(draw: st.DrawFn) -> SpaceDistribution:
    entries = draw(st.lists(_st_space_entry(), min_size=0, max_size=15))
    return SpaceDistribution(
        updated_at=draw(_st_datetime),
        total_size=draw(_st_size_gb),
        breakdown=entries,
    )


@st.composite
def _st_convergence_section(draw: st.DrawFn) -> ConvergenceSection:
    strategies = draw(st.lists(_st_convergence_stats(), min_size=1, max_size=10))
    return ConvergenceSection(
        updated_at=draw(_st_datetime),
        strategies=strategies,
    )


@st.composite
def _st_pipeline_metrics(draw: st.DrawFn) -> PipelineMetrics:
    convergence = draw(st.one_of(st.none(), _st_convergence_section()))
    return PipelineMetrics(
        run_date=draw(_st_datetime),
        partial=draw(st.booleans()),
        time_distribution=draw(_st_time_distribution()),
        space_distribution=draw(_st_space_distribution()),
        convergence=convergence,
    )


# ---------------------------------------------------------------------------
# Serialization helpers (mirrors YamlMetricsCollector flush logic)
# ---------------------------------------------------------------------------


def _serialize(metrics: PipelineMetrics) -> str:
    """Serialize a PipelineMetrics to YAML string (pipeline_metrics: wrapper)."""
    data = {"pipeline_metrics": metrics.model_dump()}
    return yaml.dump(data, default_flow_style=False, allow_unicode=True)


def _deserialize(text: str) -> PipelineMetrics:
    """Deserialize a YAML string back to PipelineMetrics."""
    raw = yaml.safe_load(text)
    return PipelineMetrics.model_validate(raw["pipeline_metrics"])


# ---------------------------------------------------------------------------
# Property 6: YAML serialization round-trip
# ---------------------------------------------------------------------------


@settings(max_examples=150)
@given(metrics=_st_pipeline_metrics())
def test_yaml_round_trip(metrics: PipelineMetrics) -> None:
    """Property 6: YAML serialization round-trip.

    For any valid PipelineMetrics instance, serializing to YAML and
    deserializing back must produce an equivalent instance — all fields
    identical (numeric fields within floating-point tolerance).

    Validates: Requirements 5.1, 5.2, 5.3, 5.4
    """
    # Feature: pipeline-metrics-report, Property 6: YAML serialization round-trip
    restored = _deserialize(_serialize(metrics))

    # Top-level scalar fields
    assert restored.run_date == metrics.run_date
    assert restored.partial  == metrics.partial

    # time_distribution
    td_orig = metrics.time_distribution
    td_rest = restored.time_distribution
    assert td_rest.updated_at    == td_orig.updated_at
    assert td_rest.total_seconds == td_orig.total_seconds
    assert td_rest.total_duration == td_orig.total_duration
    assert len(td_rest.breakdown) == len(td_orig.breakdown)
    for orig_e, rest_e in zip(td_orig.breakdown, td_rest.breakdown):
        assert rest_e.category == orig_e.category
        assert rest_e.seconds  == orig_e.seconds
        assert rest_e.duration == orig_e.duration
        assert rest_e.percent  == orig_e.percent

    # space_distribution
    sd_orig = metrics.space_distribution
    sd_rest = restored.space_distribution
    assert sd_rest.updated_at == sd_orig.updated_at
    assert sd_rest.total_size == sd_orig.total_size
    assert len(sd_rest.breakdown) == len(sd_orig.breakdown)
    for orig_e, rest_e in zip(sd_orig.breakdown, sd_rest.breakdown):
        assert rest_e.category == orig_e.category
        assert rest_e.size     == orig_e.size
        assert rest_e.percent  == orig_e.percent

    # convergence (may be None)
    assert (restored.convergence is None) == (metrics.convergence is None)
    if metrics.convergence is not None and restored.convergence is not None:
        cv_orig = metrics.convergence
        cv_rest = restored.convergence
        assert cv_rest.updated_at == cv_orig.updated_at
        assert len(cv_rest.strategies) == len(cv_orig.strategies)
        for orig_s, rest_s in zip(cv_orig.strategies, cv_rest.strategies):
            assert rest_s.strategy == orig_s.strategy
            assert rest_s.chunks   == orig_s.chunks
            assert rest_s.attempts.total  == orig_s.attempts.total
            assert rest_s.attempts.min    == orig_s.attempts.min
            assert rest_s.attempts.max    == orig_s.attempts.max
            assert abs(rest_s.attempts.mean   - orig_s.attempts.mean)   < 1e-9
            assert abs(rest_s.attempts.stddev - orig_s.attempts.stddev) < 1e-9
