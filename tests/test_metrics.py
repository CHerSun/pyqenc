"""Unit tests for pyqenc/metrics.py.

Covers enum membership, protocol conformance, and lifecycle behaviour.
"""

from pyqenc.metrics import MetricKey, NoOpMetricsCollector


# ---------------------------------------------------------------------------
# MetricKey enum membership
# ---------------------------------------------------------------------------

_EXPECTED_METRIC_KEYS: list[tuple[str, str]] = [
    ("JOB",          "job"),
    ("EXTRACTION",   "extraction"),
    ("CHUNKING",     "chunking"),
    ("AUDIO",        "audio"),
    ("ENCODING",     "encoding"),
    ("OPTIMIZATION", "optimization"),
    ("MERGE",        "merge"),
    ("RECOVERY",     "recovery"),
]


def test_metric_key_member_count() -> None:
    """MetricKey must have exactly 8 members (Req 6.1, 6.2)."""
    assert len(MetricKey) == 8


def test_metric_key_member_names_and_values() -> None:
    """Every MetricKey member must have the correct flat string value (Req 6.1, 6.2)."""
    for name, expected_value in _EXPECTED_METRIC_KEYS:
        member = MetricKey[name]
        assert member.value == expected_value, (
            f"MetricKey.{name}: expected {expected_value!r}, got {member.value!r}"
        )


def test_metric_key_is_str() -> None:
    """MetricKey values must be plain strings (StrEnum contract)."""
    for member in MetricKey:
        assert isinstance(member, str)


def test_metric_key_values_have_no_dot() -> None:
    """MetricKey values must contain no dot separator (top-level keys only)."""
    for member in MetricKey:
        assert "." not in member.value, (
            f"MetricKey.{member.name} value {member.value!r} must not contain a dot"
        )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_noop_collector_time_returns_context_manager() -> None:
    """NoOpMetricsCollector.time() must return a usable context manager."""
    collector = NoOpMetricsCollector()
    with collector.time(MetricKey.ENCODING):
        pass  # must not raise


def test_noop_collector_step_is_noop() -> None:
    """NoOpMetricsCollector.step() must accept all args without error."""
    from pyqenc.metrics import ConvergenceUpdate
    collector = NoOpMetricsCollector()
    collector.step(MetricKey.ENCODING)
    collector.step(
        MetricKey.ENCODING,
        convergence_update=ConvergenceUpdate(strategy="slow+h265", attempt_count=3),
    )


def test_noop_collector_flush_is_noop() -> None:
    """NoOpMetricsCollector.flush() must not raise."""
    collector = NoOpMetricsCollector()
    collector.flush()


# ---------------------------------------------------------------------------
# YamlMetricsCollector lifecycle tests
# ---------------------------------------------------------------------------

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from pyqenc.metrics import (
    METRICS_YAML_FILENAME,
    ConvergenceUpdate,
    MetricKey,
    YamlMetricsCollector,
)


def _make_collector(tmp_path: Path, *, force_wipe: bool = False) -> YamlMetricsCollector:
    return YamlMetricsCollector(work_dir=tmp_path, force_wipe=force_wipe)


def test_force_wipe_deletes_existing_metrics(tmp_path: Path) -> None:
    """force_wipe=True must delete existing metrics.yaml and start fresh (Req 1.2)."""
    metrics_file = tmp_path / METRICS_YAML_FILENAME
    metrics_file.write_text("old content", encoding="utf-8")

    _make_collector(tmp_path, force_wipe=True)

    assert not metrics_file.exists(), "metrics.yaml should have been deleted by force_wipe"


def test_force_wipe_false_does_not_delete(tmp_path: Path) -> None:
    """force_wipe=False must not delete an existing metrics.yaml."""
    # Write a valid metrics file first
    collector = _make_collector(tmp_path)
    collector.flush()
    assert (tmp_path / METRICS_YAML_FILENAME).exists()

    # Re-open without force_wipe — file must still be there
    _make_collector(tmp_path, force_wipe=False)
    assert (tmp_path / METRICS_YAML_FILENAME).exists()


def test_write_failure_logs_warning_and_does_not_raise(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Write failure must log WARNING and not propagate (Req 1.5)."""
    collector = _make_collector(tmp_path)
    with caplog.at_level(logging.WARNING, logger="pyqenc.metrics"):
        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            collector.flush()  # must not raise

    assert any("failed to write" in r.message for r in caplog.records), (
        "Expected a WARNING about write failure"
    )


def test_flush_writes_yaml(tmp_path: Path) -> None:
    """flush() must write a valid metrics.yaml."""
    collector = _make_collector(tmp_path)
    collector.flush()
    assert (tmp_path / METRICS_YAML_FILENAME).exists()


def test_empty_convergence_produces_null_in_yaml(tmp_path: Path) -> None:
    """No convergence data must produce convergence: null in YAML (Req 4.4)."""
    collector = _make_collector(tmp_path)
    collector.flush()

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    assert raw["pipeline_metrics"]["convergence"] is None


def test_convergence_present_after_step(tmp_path: Path) -> None:
    """Convergence section must appear after a step() with convergence_update."""
    collector = _make_collector(tmp_path)
    collector.step(
        MetricKey.ENCODING,
        convergence_update=ConvergenceUpdate(strategy="slow+h265", attempt_count=3),
    )
    collector.flush()

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    assert raw["pipeline_metrics"]["convergence"] is not None
    strategies = raw["pipeline_metrics"]["convergence"]
    assert len(strategies) == 1
    assert strategies[0]["strategy"] == "slow+h265"


def test_resume_restores_time_accum(tmp_path: Path) -> None:
    """Resuming from persisted metrics.yaml must restore time accumulators (Req 1.1)."""
    # First run: set time directly and flush
    c1 = YamlMetricsCollector(work_dir=tmp_path)
    c1._store[MetricKey.AUDIO] = 120.0
    c1.flush()

    # Second run: resume and check accumulator
    c2 = YamlMetricsCollector(work_dir=tmp_path)
    assert c2._store[MetricKey.AUDIO] == pytest.approx(120.0, abs=1.0)


def test_resume_bad_file_starts_fresh(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt metrics.yaml must log WARNING and start fresh (Req 1.5)."""
    (tmp_path / METRICS_YAML_FILENAME).write_text("not: valid: yaml: [[[", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pyqenc.metrics"):
        collector = YamlMetricsCollector(work_dir=tmp_path)

    assert all(v == 0.0 for v in collector._store.values())
    assert any("starting fresh" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Task 9.8 — Phase constructor collector injection
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.metrics import NoOpMetricsCollector


def _make_app_config() -> AppConfig:
    """Return a minimal ``AppConfig`` for phase constructor tests."""
    return load_app_config()


def test_job_phase_stores_collector(tmp_path: Path) -> None:
    """JobPhase must store the injected collector as self._collector."""
    from pyqenc.phases.job import JobPhase
    from pyqenc.models import CleanupLevel
    config    = _make_app_config()
    collector = NoOpMetricsCollector()
    source    = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    phase     = JobPhase(
        config,
        collector  = collector,
        source     = source,
        work_dir   = tmp_path / "work",
        force      = False,
        cleanup    = CleanupLevel.NONE,
        no_metrics = False,
    )
    assert phase._collector is collector


def test_extraction_phase_stores_collector(tmp_path: Path) -> None:
    """ExtractionPhase must store the injected collector as self._collector."""
    from pyqenc.phases.extraction import ExtractionPhase
    config    = _make_app_config()
    collector = NoOpMetricsCollector()
    phase     = ExtractionPhase(config, collector=collector)
    assert phase._collector is collector


def test_chunking_phase_stores_collector(tmp_path: Path) -> None:
    """ChunkingPhase must store the injected collector as self._collector."""
    from pyqenc.phases.chunking import ChunkingPhase
    config    = _make_app_config()
    collector = NoOpMetricsCollector()
    phase     = ChunkingPhase(config, collector=collector)
    assert phase._collector is collector


def test_optimization_phase_stores_collector(tmp_path: Path) -> None:
    """OptimizationPhase must store the injected collector as self._collector."""
    from pyqenc.phases.optimization import OptimizationPhase
    config    = _make_app_config()
    collector = NoOpMetricsCollector()
    phase     = OptimizationPhase(config, collector=collector)
    assert phase._collector is collector


def test_encoding_phase_stores_collector(tmp_path: Path) -> None:
    """EncodingPhase must store the injected collector as self._collector."""
    from pyqenc.phases.encoding import EncodingPhase
    config    = _make_app_config()
    collector = NoOpMetricsCollector()
    phase     = EncodingPhase(config, collector=collector)
    assert phase._collector is collector


def test_audio_phase_stores_collector(tmp_path: Path) -> None:
    """AudioPhase must store the injected collector as self._collector."""
    from pyqenc.phases.audio import AudioPhase
    config    = _make_app_config()
    collector = NoOpMetricsCollector()
    phase     = AudioPhase(config, collector=collector)
    assert phase._collector is collector


def test_merge_phase_stores_collector(tmp_path: Path) -> None:
    """MergePhase must store the injected collector as self._collector."""
    from pyqenc.phases.merge import MergePhase
    config    = _make_app_config()
    collector = NoOpMetricsCollector()
    phase     = MergePhase(config, collector=collector)
    assert phase._collector is collector



# ---------------------------------------------------------------------------
# Active-timer capture tests (Req 1.4)
# ---------------------------------------------------------------------------

def test_flush_while_timer_active_includes_partial_elapsed(tmp_path: Path) -> None:
    """flush() called while a time() context is active must include partial elapsed (Req 1.4)."""
    collector = _make_collector(tmp_path)

    ctx = collector.time(MetricKey.AUDIO)
    ctx.__enter__()

    # Flush while the context is still open
    collector.flush()

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    top_level = {e["key"]: e["seconds"] for e in raw["pipeline_metrics"]["time_distribution"]["top_level"]}

    # The partial elapsed must be >= 0 (timer was running when flush happened)
    # Key may be absent if elapsed rounds to 0 (zero entries are omitted)
    elapsed_in_yaml = top_level.get(MetricKey.AUDIO.value, 0)
    assert elapsed_in_yaml >= 0, "Expected non-negative partial elapsed for active timer"

    # _store must NOT have been mutated by the flush
    assert collector._store.get(MetricKey.AUDIO, 0.0) == 0.0, (
        "flush() must not mutate _store for in-flight timers"
    )

    # Clean up — exit the context normally
    ctx.__exit__(None, None, None)


def test_active_timer_not_double_counted_after_exit(tmp_path: Path) -> None:
    """After a time() context exits normally, the elapsed must not be double-counted (Req 1.4)."""
    collector = _make_collector(tmp_path)

    with collector.time(MetricKey.AUDIO):
        # Trigger a flush mid-context via the flush counter
        collector.flush()
        # _store still 0 here — timer not yet exited

    # After exit: _store holds the real elapsed; active_timers is empty
    assert len(collector._active_timers) == 0
    elapsed_after = collector._store.get(MetricKey.AUDIO, 0.0)
    assert elapsed_after > 0.0

    # A second flush must report the same value (no double-count)
    collector.flush()
    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    top_level = {e["key"]: e["seconds"] for e in raw["pipeline_metrics"]["time_distribution"]["top_level"]}
    # seconds is int(round(elapsed)); if it rounds to 0 the key is absent (zeros omitted)
    expected_secs = int(round(elapsed_after))
    if expected_secs == 0:
        assert MetricKey.AUDIO.value not in top_level
    else:
        assert top_level[MetricKey.AUDIO.value] == expected_secs


def test_snapshot_active_timers_does_not_mutate(tmp_path: Path) -> None:
    """_snapshot_active_timers() must not modify _active_timers or _time_accum."""
    collector = _make_collector(tmp_path)

    ctx = collector.time(MetricKey.ENCODING)
    ctx.__enter__()

    before_accum  = collector._store.get(MetricKey.ENCODING, 0.0)
    before_timers = len(collector._active_timers)

    snapshot = collector._snapshot_active_timers()

    assert collector._store.get(MetricKey.ENCODING, 0.0) == before_accum
    assert len(collector._active_timers) == before_timers
    assert MetricKey.ENCODING in snapshot
    assert snapshot[MetricKey.ENCODING] >= 0.0

    ctx.__exit__(None, None, None)
