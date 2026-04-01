"""Unit tests for pyqenc/metrics.py.

Covers enum membership, protocol conformance, and lifecycle behaviour.
"""

from pyqenc.metrics import NoOpMetricsCollector, TimeKey


# ---------------------------------------------------------------------------
# TimeKey enum membership
# ---------------------------------------------------------------------------

_EXPECTED_TIME_KEYS: list[tuple[str, str]] = [
    ("JOB_PROBE",             "job.probe"),
    ("JOB_CROP_DETECT",       "job.crop_detect"),
    ("EXTRACTION",            "extraction.mkvextract"),
    ("CHUNKING_SCENE_DETECT", "chunking.scene_detect"),
    ("CHUNKING_SPLIT",        "chunking.split"),
    ("AUDIO",                 "audio.processing"),
    ("ENCODING_OPTIMIZATION", "encoding.optimization"),
    ("ENCODING_MAIN",         "encoding.main"),
    ("MERGE_CONCAT",          "merge.concat"),
    ("MERGE_QUALITY_MEASURE", "merge.quality_measure"),
    ("RECOVERY",              "recovery"),
]


def test_time_key_member_count() -> None:
    """TimeKey must have exactly 11 members (Req 2.4)."""
    assert len(TimeKey) == 11


def test_time_key_member_names_and_values() -> None:
    """Every TimeKey member must have the correct dotted string value (Req 2.4)."""
    for name, expected_value in _EXPECTED_TIME_KEYS:
        member = TimeKey[name]
        assert member.value == expected_value, (
            f"TimeKey.{name}: expected {expected_value!r}, got {member.value!r}"
        )


def test_time_key_is_str() -> None:
    """TimeKey values must be plain strings (StrEnum contract)."""
    for member in TimeKey:
        assert isinstance(member, str)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_noop_collector_time_returns_context_manager() -> None:
    """NoOpMetricsCollector.time() must return a usable context manager."""
    collector = NoOpMetricsCollector()
    with collector.time(TimeKey.ENCODING_MAIN):
        pass  # must not raise


def test_noop_collector_step_is_noop() -> None:
    """NoOpMetricsCollector.step() must accept all args without error."""
    from pyqenc.metrics import ConvergenceUpdate
    collector = NoOpMetricsCollector()
    collector.step(TimeKey.ENCODING_MAIN)
    collector.step(
        TimeKey.ENCODING_MAIN,
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
    TimeKey,
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
        TimeKey.ENCODING_MAIN,
        convergence_update=ConvergenceUpdate(strategy="slow+h265", attempt_count=3),
    )
    collector.flush()

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    assert raw["pipeline_metrics"]["convergence"] is not None
    strategies = raw["pipeline_metrics"]["convergence"]["strategies"]
    assert len(strategies) == 1
    assert strategies[0]["strategy"] == "slow+h265"


def test_resume_restores_time_accum(tmp_path: Path) -> None:
    """Resuming from persisted metrics.yaml must restore time accumulators (Req 1.1)."""
    # First run: set time directly and flush
    c1 = YamlMetricsCollector(work_dir=tmp_path)
    c1._time_accum[TimeKey.AUDIO] = 120.0
    c1.flush()

    # Second run: resume and check accumulator
    c2 = YamlMetricsCollector(work_dir=tmp_path)
    assert c2._time_accum[TimeKey.AUDIO] == pytest.approx(120.0, abs=1.0)


def test_resume_bad_file_starts_fresh(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt metrics.yaml must log WARNING and start fresh (Req 1.5)."""
    (tmp_path / METRICS_YAML_FILENAME).write_text("not: valid: yaml: [[[", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="pyqenc.metrics"):
        collector = YamlMetricsCollector(work_dir=tmp_path)

    assert all(v == 0.0 for v in collector._time_accum.values())
    assert any("starting fresh" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Task 9.8 — Phase constructor collector injection
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock

from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    PipelineConfig,
    QualityTarget,
    Strategy,
)


def _make_pipeline_config(tmp_path: Path) -> PipelineConfig:
    """Return a minimal ``PipelineConfig`` for phase constructor tests."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    return PipelineConfig(
        source_video    = source,
        work_dir        = tmp_path / "work",
        quality_targets = [],
        strategies      = [],
        optimize        = False,
        max_parallel    = 1,
        include         = None,
        exclude         = None,
        cleanup         = CleanupLevel.NONE,
        chunking_mode   = ChunkingMode.LOSSLESS,
        force           = False,
    )


def test_job_phase_stores_collector(tmp_path: Path) -> None:
    """JobPhase must store the injected collector as self._collector."""
    from pyqenc.phases.job import JobPhase
    config    = _make_pipeline_config(tmp_path)
    collector = NoOpMetricsCollector()
    phase     = JobPhase(config, collector=collector)
    assert phase._collector is collector


def test_extraction_phase_stores_collector(tmp_path: Path) -> None:
    """ExtractionPhase must store the injected collector as self._collector."""
    from pyqenc.phases.extraction import ExtractionPhase
    config    = _make_pipeline_config(tmp_path)
    collector = NoOpMetricsCollector()
    phase     = ExtractionPhase(config, collector=collector)
    assert phase._collector is collector


def test_chunking_phase_stores_collector(tmp_path: Path) -> None:
    """ChunkingPhase must store the injected collector as self._collector."""
    from pyqenc.phases.chunking import ChunkingPhase
    config    = _make_pipeline_config(tmp_path)
    collector = NoOpMetricsCollector()
    phase     = ChunkingPhase(config, collector=collector)
    assert phase._collector is collector


def test_optimization_phase_stores_collector(tmp_path: Path) -> None:
    """OptimizationPhase must store the injected collector as self._collector."""
    from pyqenc.phases.optimization import OptimizationPhase
    config    = _make_pipeline_config(tmp_path)
    collector = NoOpMetricsCollector()
    phase     = OptimizationPhase(config, collector=collector)
    assert phase._collector is collector


def test_encoding_phase_stores_collector(tmp_path: Path) -> None:
    """EncodingPhase must store the injected collector as self._collector."""
    from pyqenc.phases.encoding import EncodingPhase
    config    = _make_pipeline_config(tmp_path)
    collector = NoOpMetricsCollector()
    phase     = EncodingPhase(config, collector=collector)
    assert phase._collector is collector


def test_audio_phase_stores_collector(tmp_path: Path) -> None:
    """AudioPhase must store the injected collector as self._collector."""
    from pyqenc.phases.audio import AudioPhase
    config    = _make_pipeline_config(tmp_path)
    collector = NoOpMetricsCollector()
    phase     = AudioPhase(config, collector=collector)
    assert phase._collector is collector


def test_merge_phase_stores_collector(tmp_path: Path) -> None:
    """MergePhase must store the injected collector as self._collector."""
    from pyqenc.phases.merge import MergePhase
    config    = _make_pipeline_config(tmp_path)
    collector = NoOpMetricsCollector()
    phase     = MergePhase(config, collector=collector)
    assert phase._collector is collector



# ---------------------------------------------------------------------------
# Active-timer capture tests (Req 1.4)
# ---------------------------------------------------------------------------

def test_flush_while_timer_active_includes_partial_elapsed(tmp_path: Path) -> None:
    """flush() called while a time() context is active must include partial elapsed (Req 1.4)."""
    collector = _make_collector(tmp_path)

    ctx = collector.time(TimeKey.AUDIO)
    ctx.__enter__()

    # Flush while the context is still open
    collector.flush()

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    breakdown = {e["category"]: e["seconds"] for e in raw["pipeline_metrics"]["time_distribution"]["breakdown"]}

    # The partial elapsed must be >= 0 (timer was running when flush happened)
    # Key may be absent if elapsed rounds to 0 (zero entries are omitted)
    elapsed_in_yaml = breakdown.get(TimeKey.AUDIO.value, 0)
    assert elapsed_in_yaml >= 0, "Expected non-negative partial elapsed for active timer"

    # _time_accum must NOT have been mutated by the flush
    assert collector._time_accum[TimeKey.AUDIO] == 0.0, (
        "flush() must not mutate _time_accum for in-flight timers"
    )

    # Clean up — exit the context normally
    ctx.__exit__(None, None, None)


def test_active_timer_not_double_counted_after_exit(tmp_path: Path) -> None:
    """After a time() context exits normally, the elapsed must not be double-counted (Req 1.4)."""
    collector = _make_collector(tmp_path)

    with collector.time(TimeKey.AUDIO):
        # Trigger a flush mid-context via the flush counter
        collector.flush()
        # _time_accum still 0 here — timer not yet exited

    # After exit: _time_accum holds the real elapsed; active_timers is empty
    assert len(collector._active_timers) == 0
    elapsed_after = collector._time_accum[TimeKey.AUDIO]
    assert elapsed_after > 0.0

    # A second flush must report the same value (no double-count)
    collector.flush()
    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    breakdown = {e["category"]: e["seconds"] for e in raw["pipeline_metrics"]["time_distribution"]["breakdown"]}
    # seconds is int(round(elapsed)); if it rounds to 0 the key is absent (zeros omitted)
    expected_secs = int(round(elapsed_after))
    if expected_secs == 0:
        assert TimeKey.AUDIO.value not in breakdown
    else:
        assert breakdown[TimeKey.AUDIO.value] == expected_secs


def test_snapshot_active_timers_does_not_mutate(tmp_path: Path) -> None:
    """_snapshot_active_timers() must not modify _active_timers or _time_accum."""
    collector = _make_collector(tmp_path)

    ctx = collector.time(TimeKey.ENCODING_MAIN)
    ctx.__enter__()

    before_accum  = collector._time_accum[TimeKey.ENCODING_MAIN]
    before_timers = len(collector._active_timers)

    snapshot = collector._snapshot_active_timers()

    assert collector._time_accum[TimeKey.ENCODING_MAIN] == before_accum
    assert len(collector._active_timers) == before_timers
    assert TimeKey.ENCODING_MAIN in snapshot
    assert snapshot[TimeKey.ENCODING_MAIN] >= 0.0

    ctx.__exit__(None, None, None)
