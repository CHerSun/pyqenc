"""Unit tests for pyqenc/metrics.py.

Covers enum membership, protocol conformance, and lifecycle behaviour.
"""

from pyqenc.metrics import NoOpMetricsCollector, SpaceKey, TimeKey


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
# SpaceKey enum membership
# ---------------------------------------------------------------------------

_EXPECTED_SPACE_KEYS: list[tuple[str, str]] = [
    ("SOURCE",             "source"),
    ("EXTRACTED_VIDEO",    "extracted.video"),
    ("EXTRACTED_AUDIO",    "extracted.audio"),
    ("EXTRACTED_OTHER",    "extracted.other"),
    ("CHUNKS",             "chunks"),
    ("AUDIO_INTERMEDIATE", "audio.intermediate"),
    ("AUDIO_FINAL",        "audio.final"),
    ("ENCODING_WORKSPACE", "encoding.workspace"),
    ("ENCODING_OUTPUTS",   "encoding.outputs"),
    ("FINAL",              "final"),
]


def test_space_key_member_count() -> None:
    """SpaceKey must have exactly 10 members (Req 3.2)."""
    assert len(SpaceKey) == 10


def test_space_key_member_names_and_values() -> None:
    """Every SpaceKey member must have the correct dotted string value (Req 3.2)."""
    for name, expected_value in _EXPECTED_SPACE_KEYS:
        member = SpaceKey[name]
        assert member.value == expected_value, (
            f"SpaceKey.{name}: expected {expected_value!r}, got {member.value!r}"
        )


def test_space_key_is_str() -> None:
    """SpaceKey values must be plain strings (StrEnum contract)."""
    for member in SpaceKey:
        assert isinstance(member, str)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_noop_collector_time_returns_context_manager() -> None:
    """NoOpMetricsCollector.time() must return a usable context manager."""
    collector = NoOpMetricsCollector()
    with collector.time(TimeKey.ENCODING_MAIN):
        pass  # must not raise


def test_noop_collector_record_step_is_noop() -> None:
    """NoOpMetricsCollector.record_step() must accept all args without error."""
    from pyqenc.metrics import ConvergenceUpdate
    collector = NoOpMetricsCollector()
    collector.record_step(TimeKey.ENCODING_MAIN, 1.5)
    collector.record_step(
        TimeKey.ENCODING_MAIN,
        2.0,
        convergence_update=ConvergenceUpdate(strategy="slow+h265", attempt_count=3),
    )


def test_noop_collector_flush_is_noop() -> None:
    """NoOpMetricsCollector.flush() must accept partial flag without error."""
    collector = NoOpMetricsCollector()
    collector.flush(partial=True)
    collector.flush(partial=False)


# ---------------------------------------------------------------------------
# YamlMetricsCollector lifecycle tests
# ---------------------------------------------------------------------------

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from pyqenc.metrics import (
    METRICS_YAML_FILENAME,
    ConvergenceUpdate,
    TimeKey,
    YamlMetricsCollector,
)


def _make_config(source_video: Path) -> MagicMock:
    """Return a minimal PipelineConfig-like mock."""
    cfg = MagicMock()
    cfg.source_video = source_video
    return cfg


def _make_collector(tmp_path: Path, *, force_wipe: bool = False) -> YamlMetricsCollector:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x" * 100)
    return YamlMetricsCollector(
        work_dir=tmp_path,
        config=_make_config(source),
        force_wipe=force_wipe,
    )


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
    collector.flush(partial=True)
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
            collector.flush(partial=True)  # must not raise

    assert any("failed to write" in r.message for r in caplog.records), (
        "Expected a WARNING about write failure"
    )


def test_flush_partial_false_sets_partial_false(tmp_path: Path) -> None:
    """flush(partial=False) must write partial: false in YAML (Req 5.4)."""
    collector = _make_collector(tmp_path)
    collector.flush(partial=False)

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    assert raw["pipeline_metrics"]["partial"] is False


def test_flush_partial_true_sets_partial_true(tmp_path: Path) -> None:
    """flush(partial=True) must write partial: true in YAML (Req 5.4)."""
    collector = _make_collector(tmp_path)
    collector.flush(partial=True)

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    assert raw["pipeline_metrics"]["partial"] is True


def test_empty_convergence_produces_null_in_yaml(tmp_path: Path) -> None:
    """No convergence data must produce convergence: null in YAML (Req 4.4)."""
    collector = _make_collector(tmp_path)
    collector.flush(partial=True)

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    assert raw["pipeline_metrics"]["convergence"] is None


def test_convergence_present_after_record_step(tmp_path: Path) -> None:
    """Convergence section must appear after a record_step with convergence_update."""
    collector = _make_collector(tmp_path)
    collector.record_step(
        TimeKey.ENCODING_MAIN,
        5.0,
        convergence_update=ConvergenceUpdate(strategy="slow+h265", attempt_count=3),
    )
    collector.flush(partial=True)

    raw = yaml.safe_load((tmp_path / METRICS_YAML_FILENAME).read_text(encoding="utf-8"))
    assert raw["pipeline_metrics"]["convergence"] is not None
    strategies = raw["pipeline_metrics"]["convergence"]["strategies"]
    assert len(strategies) == 1
    assert strategies[0]["strategy"] == "slow+h265"


def test_resume_restores_time_accum(tmp_path: Path) -> None:
    """Resuming from persisted metrics.yaml must restore time accumulators (Req 1.1)."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x" * 100)
    config = _make_config(source)

    # First run: record some time and flush
    c1 = YamlMetricsCollector(work_dir=tmp_path, config=config)
    c1.record_step(TimeKey.AUDIO, 120.0)
    c1.flush(partial=True)

    # Second run: resume and check accumulator
    c2 = YamlMetricsCollector(work_dir=tmp_path, config=config)
    assert c2._time_accum[TimeKey.AUDIO] == pytest.approx(120.0, abs=1.0)


def test_resume_bad_file_starts_fresh(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt metrics.yaml must log WARNING and start fresh (Req 1.5)."""
    (tmp_path / METRICS_YAML_FILENAME).write_text("not: valid: yaml: [[[", encoding="utf-8")
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")

    with caplog.at_level(logging.WARNING, logger="pyqenc.metrics"):
        collector = YamlMetricsCollector(
            work_dir=tmp_path,
            config=_make_config(source),
        )

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


def test_phase_collector_defaults_to_noop_when_omitted(tmp_path: Path) -> None:
    """When no collector is passed, each phase must default to NoOpMetricsCollector."""
    from pyqenc.phases.audio import AudioPhase
    from pyqenc.phases.chunking import ChunkingPhase
    from pyqenc.phases.encoding import EncodingPhase
    from pyqenc.phases.extraction import ExtractionPhase
    from pyqenc.phases.job import JobPhase
    from pyqenc.phases.merge import MergePhase
    from pyqenc.phases.optimization import OptimizationPhase

    config = _make_pipeline_config(tmp_path)
    for cls in [JobPhase, ExtractionPhase, ChunkingPhase, OptimizationPhase,
                EncodingPhase, AudioPhase, MergePhase]:
        phase = cls(config)
        assert isinstance(phase._collector, NoOpMetricsCollector), (
            f"{cls.__name__}._collector should be NoOpMetricsCollector when omitted"
        )
