"""Property-based tests for pyqenc/metrics.py.

Each test is tagged with the feature and property it validates.
Run with: uv run python -m pytest tests/test_metrics_properties.py
"""

# Feature: pipeline-metrics-report, Property 6: YAML serialization round-trip

from __future__ import annotations

import re

import yaml
from hypothesis import HealthCheck, given, settings
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


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
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


# ---------------------------------------------------------------------------
# Property 4: Space measurement accuracy
# ---------------------------------------------------------------------------

# Feature: pipeline-metrics-report, Property 4: Space measurement accuracy

import math
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from pyqenc.metrics import SpaceKey, _measure_space


def _make_config(source_video: Path) -> MagicMock:
    """Return a minimal PipelineConfig-like mock with source_video set."""
    cfg = MagicMock()
    cfg.source_video = source_video
    return cfg


def _write_file(path: Path, size: int) -> None:
    """Create *path* with exactly *size* bytes of content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


@st.composite
def _st_file_sizes(draw: st.DrawFn) -> list[int]:
    """Draw a list of 1–8 positive file sizes (bytes)."""
    return draw(st.lists(st.integers(min_value=1, max_value=64 * 1024), min_size=1, max_size=8))


@settings(max_examples=100)
@given(
    source_size=st.integers(min_value=0, max_value=1024 * 1024),
    mkv_sizes=_st_file_sizes(),
    mka_sizes=_st_file_sizes(),
    other_sizes=_st_file_sizes(),
    chunk_sizes=_st_file_sizes(),
    flac_sizes=_st_file_sizes(),
    audio_final_sizes=_st_file_sizes(),
    enc_ws_sizes=_st_file_sizes(),
    enc_out_sizes=_st_file_sizes(),
    final_sizes=_st_file_sizes(),
)
def test_space_measurement_accuracy(
    source_size: int,
    mkv_sizes: list[int],
    mka_sizes: list[int],
    other_sizes: list[int],
    chunk_sizes: list[int],
    flac_sizes: list[int],
    audio_final_sizes: list[int],
    enc_ws_sizes: list[int],
    enc_out_sizes: list[int],
    final_sizes: list[int],
) -> None:
    """Property 4: Space measurement accuracy.

    For any work directory with known file sizes, _measure_space() must return
    exact byte counts per SpaceKey category.  Total bytes must equal the sum of
    all category bytes.

    Validates: Requirements 3.1, 3.3, 3.4
    """
    with tempfile.TemporaryDirectory() as _tmp:
        tmp_path = Path(_tmp)

        # --- source video (outside work_dir) ---
        source = tmp_path / "source.mkv"
        _write_file(source, source_size)

        work = tmp_path / "work"
        work.mkdir()

        # --- extracted/ ---
        for i, sz in enumerate(mkv_sizes):
            _write_file(work / "extracted" / f"video_{i}.mkv", sz)
        for i, sz in enumerate(mka_sizes):
            _write_file(work / "extracted" / f"audio_{i}.mka", sz)
        for i, sz in enumerate(other_sizes):
            _write_file(work / "extracted" / f"sub_{i}.srt", sz)

        # --- chunks/ (recursive) ---
        for i, sz in enumerate(chunk_sizes):
            _write_file(work / "chunks" / f"sub_{i % 3}" / f"chunk_{i}.mkv", sz)

        # --- audio/ (flat) ---
        for i, sz in enumerate(flac_sizes):
            _write_file(work / "audio" / f"intermediate_{i}.flac", sz)
        for i, sz in enumerate(audio_final_sizes):
            _write_file(work / "audio" / f"final_{i}.aac", sz)

        # --- encoding/ (recursive) ---
        for i, sz in enumerate(enc_ws_sizes):
            _write_file(work / "encoding" / f"strat_{i % 2}" / f"attempt_{i}.mkv", sz)

        # --- encoded/ (recursive) ---
        for i, sz in enumerate(enc_out_sizes):
            _write_file(work / "encoded" / f"strat_{i % 2}" / f"chunk_{i}.mkv", sz)

        # --- final/ (recursive) ---
        for i, sz in enumerate(final_sizes):
            _write_file(work / "final" / f"output_{i}.mkv", sz)

        config = _make_config(source)
        result = _measure_space(work, config)

        # Verify exact byte counts per category
        assert result[SpaceKey.SOURCE]             == source_size
        assert result[SpaceKey.EXTRACTED_VIDEO]    == sum(mkv_sizes)
        assert result[SpaceKey.EXTRACTED_AUDIO]    == sum(mka_sizes)
        assert result[SpaceKey.EXTRACTED_OTHER]    == sum(other_sizes)
        assert result[SpaceKey.CHUNKS]             == sum(chunk_sizes)
        assert result[SpaceKey.AUDIO_INTERMEDIATE] == sum(flac_sizes)
        assert result[SpaceKey.AUDIO_FINAL]        == sum(audio_final_sizes)
        assert result[SpaceKey.ENCODING_WORKSPACE] == sum(enc_ws_sizes)
        assert result[SpaceKey.ENCODING_OUTPUTS]   == sum(enc_out_sizes)
        assert result[SpaceKey.FINAL]              == sum(final_sizes)

        # All SpaceKey values must be present
        assert set(result.keys()) == set(SpaceKey)


@settings(max_examples=50)
@given(present=st.frozensets(st.sampled_from(list(SpaceKey)), min_size=0))
def test_space_measurement_missing_dirs_return_zero(
    present: frozenset[SpaceKey],
) -> None:
    """Missing directories and files must contribute 0 bytes (Req 3.4)."""
    with tempfile.TemporaryDirectory() as _tmp:
        tmp_path = Path(_tmp)
        source = tmp_path / "source.mkv"
        if SpaceKey.SOURCE in present:
            _write_file(source, 100)

        work = tmp_path / "work"
        work.mkdir()

        config = _make_config(source)
        result = _measure_space(work, config)

        assert set(result.keys()) == set(SpaceKey)
        if SpaceKey.SOURCE not in present:
            assert result[SpaceKey.SOURCE] == 0


# ---------------------------------------------------------------------------
# Property 5: Convergence stats math
# ---------------------------------------------------------------------------

# Feature: pipeline-metrics-report, Property 5: Convergence stats math

from pyqenc.metrics import (
    ConvergenceUpdate,
    NoOpMetricsCollector,
    TimeKey,
    _ConvergenceAccumulator,
    _compute_convergence,
    _update_accumulator,
)


@st.composite
def _st_attempt_sequence(draw: st.DrawFn) -> list[int]:
    """Draw a non-empty list of attempt counts (integers ≥ 1)."""
    return draw(st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=100))


def _population_stddev(values: list[int]) -> float:
    """Compute population standard deviation (0.0 for single element)."""
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

    For any sequence of attempt counts fed incrementally via _update_accumulator,
    the computed ConvergenceStats must match min/max/sum/mean/population_stddev/len
    of the input sequence.

    Validates: Requirements 4.2, 4.1a
    """
    strategy = "slow+h265-aq"
    acc = _ConvergenceAccumulator()
    for c in counts:
        _update_accumulator(acc, c)

    result = _compute_convergence({strategy: acc})
    assert result is not None
    assert len(result) == 1

    stats = result[0]
    assert stats.strategy == strategy
    assert stats.chunks   == len(counts)
    assert stats.attempts.total  == sum(counts)
    assert stats.attempts.min    == min(counts)
    assert stats.attempts.max    == max(counts)
    # stats.attempts.mean/stddev are stored as round(..., 1), so the maximum
    # deviation from the exact value is one rounding step (0.1).
    exact_mean   = sum(counts) / len(counts)
    exact_stddev = _population_stddev(counts)
    assert abs(stats.attempts.mean   - exact_mean)   < 0.1
    assert abs(stats.attempts.stddev - exact_stddev) < 0.1


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
    assert len(result) == 2
    assert result[0].strategy == "a-strategy"
    assert result[1].strategy == "z-strategy"


def test_convergence_stats_empty_returns_none() -> None:
    """_compute_convergence must return None when all accumulators are empty (Req 4.4)."""
    accumulators = {
        "strat-a": _ConvergenceAccumulator(),
        "strat-b": _ConvergenceAccumulator(),
    }
    assert _compute_convergence(accumulators) is None
    assert _compute_convergence({}) is None


@settings(max_examples=100)
@given(counts=_st_attempt_sequence())
def test_convergence_stats_resume_from_yaml(counts: list[int]) -> None:
    """Property 5 (resume): restoring Welford state from persisted mean/stddev
    and continuing accumulation must produce identical results to a fresh run.

    Validates: Requirements 4.2, 4.1a
    """
    import yaml as _yaml

    from pyqenc.metrics import (
        AttemptStats,
        ConvergenceSection,
        ConvergenceStats,
        PipelineMetrics,
        SpaceDistribution,
        TimeDistribution,
    )

    strategy = "slow+h265-aq"

    # --- fresh run: feed all counts ---
    acc_fresh = _ConvergenceAccumulator()
    for c in counts:
        _update_accumulator(acc_fresh, c)
    result_fresh = _compute_convergence({strategy: acc_fresh})
    assert result_fresh is not None
    stats_fresh = result_fresh[0]

    # --- simulate persist + resume ---
    # Persisted YAML stores mean and stddev; resume restores welford_mean and
    # welford_M2 = stddev² * n  (as documented in the design).
    persisted_mean   = acc_fresh.welford_mean
    persisted_stddev = 0.0 if acc_fresh.n == 1 else math.sqrt(acc_fresh.welford_M2 / acc_fresh.n)

    acc_resumed = _ConvergenceAccumulator(
        n=acc_fresh.n,
        total=acc_fresh.total,
        min=acc_fresh.min,
        max=acc_fresh.max,
        welford_mean=persisted_mean,
        welford_M2=persisted_stddev ** 2 * acc_fresh.n,
    )

    result_resumed = _compute_convergence({strategy: acc_resumed})
    assert result_resumed is not None
    stats_resumed = result_resumed[0]

    # All fields must match the fresh run
    assert stats_resumed.chunks          == stats_fresh.chunks
    assert stats_resumed.attempts.total  == stats_fresh.attempts.total
    assert stats_resumed.attempts.min    == stats_fresh.attempts.min
    assert stats_resumed.attempts.max    == stats_fresh.attempts.max
    assert abs(stats_resumed.attempts.mean   - stats_fresh.attempts.mean)   < 1e-6
    assert abs(stats_resumed.attempts.stddev - stats_fresh.attempts.stddev) < 1e-6


# ---------------------------------------------------------------------------
# Property 1: Time accumulation round-trip
# ---------------------------------------------------------------------------

# Feature: pipeline-metrics-report, Property 1: Time accumulation round-trip

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from pyqenc.metrics import TimeKey, YamlMetricsCollector


def _make_yaml_collector(tmp_path: Path) -> YamlMetricsCollector:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x" * 100)
    cfg = MagicMock()
    cfg.source_video = source
    return YamlMetricsCollector(work_dir=tmp_path, config=cfg)


@settings(max_examples=200, deadline=None)
@given(
    key=st.sampled_from(list(TimeKey)),
    durations=st.lists(st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False), min_size=1, max_size=50),
)
def test_time_accumulation_round_trip(key: TimeKey, durations: list[float]) -> None:
    """Property 1: Time accumulation round-trip.

    For any TimeKey and any sequence of elapsed durations recorded via time()
    context managers, the accumulated seconds for that key must equal the sum
    of all recorded durations.

    Validates: Requirements 2.1, 2.2, 2.2a
    """
    # Feature: pipeline-metrics-report, Property 1: Time accumulation round-trip
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        # Inject durations directly into the accumulator (time() measures real
        # wall-clock which is non-deterministic; we test the accumulation math)
        for d in durations:
            collector._time_accum[key] += d

        expected = sum(durations)
        tol = max(1e-9, abs(expected) * 1e-9)
        assert abs(collector._time_accum[key] - expected) <= tol, (
            f"Expected {expected}, got {collector._time_accum[key]}"
        )


# ---------------------------------------------------------------------------
# Property 2: Time distribution math
# ---------------------------------------------------------------------------

# Feature: pipeline-metrics-report, Property 2: Time distribution math

from pyqenc.metrics import PipelineMetrics, TimeDistribution


@settings(max_examples=200, deadline=None)
@given(
    time_map=st.fixed_dictionaries({
        key: st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        for key in TimeKey
    }),
)
def test_time_distribution_math(time_map: dict[TimeKey, float]) -> None:
    """Property 2: Time distribution math.

    For any mapping of TimeKey → float (non-negative), total_seconds must equal
    the rounded sum of all accumulated seconds, and each key's percent must
    equal (seconds / total_seconds) * 100 (or 0.0 when total is 0).
    Zero-second keys must still appear in the breakdown.

    Validates: Requirements 2.3, 2.5
    """
    # Feature: pipeline-metrics-report, Property 2: Time distribution math
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for key, elapsed in time_map.items():
            if elapsed > 0:
                collector._time_accum[key] = elapsed

        collector.flush(partial=True)

        raw = yaml.safe_load((Path(_tmp) / "metrics.yaml").read_text(encoding="utf-8"))
        td  = raw["pipeline_metrics"]["time_distribution"]

        total_secs = td["total_seconds"]
        breakdown  = td["breakdown"]

        # All TimeKey values must appear
        categories = {e["category"] for e in breakdown}
        assert categories == {k.value for k in TimeKey}

        # total_seconds must equal int(round(sum of all accumulated floats))
        expected_total = int(round(sum(collector._time_accum.values())))
        assert total_secs == expected_total

        # Each percent must match seconds / total * 100 (within rounding tolerance)
        for entry in breakdown:
            secs = entry["seconds"]
            pct_str = entry["percent"].rstrip("%")
            pct = float(pct_str)
            if total_secs > 0:
                expected_pct = secs / total_secs * 100
            else:
                expected_pct = 0.0
            assert abs(pct - expected_pct) < 0.15, (
                f"category={entry['category']}: expected {expected_pct:.1f}%, got {pct:.1f}%"
            )


# ---------------------------------------------------------------------------
# Property 3: Breakdown sorted descending
# ---------------------------------------------------------------------------

# Feature: pipeline-metrics-report, Property 3: Breakdown sorted descending


@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
@given(
    time_map=st.fixed_dictionaries({
        key: st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        for key in TimeKey
    }),
)
def test_breakdown_sorted_descending(time_map: dict[TimeKey, float]) -> None:
    """Property 3: Breakdown sorted descending.

    For any set of time recordings flushed via YamlMetricsCollector,
    time_distribution.breakdown must be sorted descending by seconds, and
    space_distribution.breakdown must be sorted descending by bytes.

    Validates: Requirements 2.6, 3.5
    """
    # Feature: pipeline-metrics-report, Property 3: Breakdown sorted descending
    with tempfile.TemporaryDirectory() as _tmp:
        collector = _make_yaml_collector(Path(_tmp))
        for key, elapsed in time_map.items():
            if elapsed > 0:
                collector._time_accum[key] = elapsed
        collector.flush(partial=True)

        raw = yaml.safe_load((Path(_tmp) / "metrics.yaml").read_text(encoding="utf-8"))
        pm  = raw["pipeline_metrics"]

        # time breakdown: sorted descending by seconds
        time_secs = [e["seconds"] for e in pm["time_distribution"]["breakdown"]]
        assert time_secs == sorted(time_secs, reverse=True), (
            f"time breakdown not sorted descending: {time_secs}"
        )

        # space breakdown: sorted descending by GB value (ASCII digits only from _format_gb)
        space_gb = [float(e["size"].removesuffix(" GB")) for e in pm["space_distribution"]["breakdown"]]
        assert space_gb == sorted(space_gb, reverse=True), (
            f"space breakdown not sorted descending: {space_gb}"
        )
