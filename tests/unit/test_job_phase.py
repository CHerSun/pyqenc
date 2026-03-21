"""Unit tests for JobPhase source mismatch detection and force_wipe propagation.

Covers:
- scan(): returns REUSED when job.yaml exists, DRY_RUN when absent
- run() dry-run: returns DRY_RUN when job.yaml absent, REUSED when present
- run() execute: creates job.yaml on first run (COMPLETED)
- Source mismatch without --force: returns FAILED, force_wipe=False
- Source mismatch with --force: returns COMPLETED, force_wipe=True, job.yaml overwritten, other phases' files untouched
- No mismatch: returns COMPLETED/REUSED, force_wipe=False
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    PipelineConfig,
    QualityTarget,
    Strategy,
    VideoMetadata,
)
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.state import JobState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUALITY_TARGETS = [QualityTarget(metric="vmaf", statistic="min", value=93.0)]
_STRATEGY        = Strategy.from_name("slow+h265-aq")


def _make_source(tmp_path: Path, size: int = 1024) -> Path:
    """Create a fake source video file."""
    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * size)
    return src


def _make_config(
    tmp_path: Path,
    source: Path,
    force: bool = False,
    crop_params: CropParams | None = None,
) -> PipelineConfig:
    return PipelineConfig(
        source_video    = source,
        work_dir        = tmp_path / "work",
        quality_targets = _QUALITY_TARGETS,
        strategies      = [_STRATEGY],
        optimize        = False,
        all_strategies  = True,
        cleanup         = CleanupLevel.NONE,
        chunking_mode   = ChunkingMode.LOSSLESS,
        force           = force,
        crop_params     = crop_params,
    )


def _make_phase(
    tmp_path: Path,
    source: Path,
    force: bool = False,
    crop_params: CropParams | None = None,
) -> JobPhase:
    config = _make_config(tmp_path, source, force=force, crop_params=crop_params)
    return JobPhase(config)


def _persist_job(work_dir: Path, source: Path, file_size: int | None = None) -> None:
    """Write a job.yaml with given source metadata."""
    work_dir.mkdir(parents=True, exist_ok=True)
    vm = VideoMetadata(path=source)
    if file_size is not None:
        vm._file_size_bytes = file_size
    else:
        vm._file_size_bytes = source.stat().st_size
    JobState(source=vm).save(work_dir / "job.yaml")


def _write_phase_param(work_dir: Path, filename: str) -> Path:
    """Write a dummy phase parameter YAML file."""
    path = work_dir / filename
    path.write_text(yaml.dump({"dummy": True}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------

class TestJobPhaseScan:
    def test_scan_absent_returns_dry_run(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        result = phase.scan()
        assert result.outcome == PhaseOutcome.DRY_RUN
        assert result.is_complete is False
        assert result.force_wipe is False
        assert result.job is None

    def test_scan_existing_returns_reused(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src)

        result = phase.scan()
        assert result.outcome == PhaseOutcome.REUSED
        assert result.is_complete is True
        assert result.force_wipe is False
        assert result.job is not None

    def test_scan_caches_result(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        r1 = phase.scan()
        r2 = phase.scan()
        assert r1 is r2  # same cached object


# ---------------------------------------------------------------------------
# run() dry-run mode
# ---------------------------------------------------------------------------

class TestJobPhaseRunDryRun:
    def test_dry_run_absent_returns_dry_run(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=True)
        assert result.outcome == PhaseOutcome.DRY_RUN
        assert result.is_complete is False

    def test_dry_run_existing_returns_reused(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src)
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=True)
        assert result.outcome == PhaseOutcome.REUSED
        assert result.is_complete is True

    def test_dry_run_mismatch_logs_warning_no_fail(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)  # wrong size
        phase = _make_phase(tmp_path, src)

        with caplog.at_level(logging.WARNING):
            result = phase.run(dry_run=True)

        # Dry-run: mismatch is a warning, not a failure
        assert result.outcome != PhaseOutcome.FAILED
        assert any("mismatch" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# run() execute mode — no mismatch
# ---------------------------------------------------------------------------

class TestJobPhaseRunExecuteNoMismatch:
    def test_first_run_creates_job_yaml(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=False)
        assert result.is_complete is True
        assert (tmp_path / "work" / "job.yaml").exists()

    def test_first_run_force_wipe_false(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=False)
        assert result.force_wipe is False

    def test_second_run_reuses(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        phase.run(dry_run=False)

        # Reset cached result and run again
        phase.result = None
        result = phase.run(dry_run=False)
        assert result.is_complete is True
        assert result.force_wipe is False

    def test_manual_crop_stored_in_result(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        crop = CropParams(top=28, bottom=28, left=0, right=0)
        phase = _make_phase(tmp_path, src, crop_params=crop)
        result = phase.run(dry_run=False)
        assert result.crop == crop


# ---------------------------------------------------------------------------
# Source mismatch — execute without --force
# ---------------------------------------------------------------------------

class TestJobPhaseSourceMismatchNoForce:
    def test_mismatch_returns_failed(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)  # wrong size
        phase = _make_phase(tmp_path, src, force=False)
        result = phase.run(dry_run=False)
        assert result.outcome == PhaseOutcome.FAILED
        assert result.is_complete is False

    def test_mismatch_force_wipe_false_on_failure(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)
        phase = _make_phase(tmp_path, src, force=False)
        result = phase.run(dry_run=False)
        assert result.force_wipe is False

    def test_mismatch_logs_critical(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)
        phase = _make_phase(tmp_path, src, force=False)

        with caplog.at_level(logging.CRITICAL):
            phase.run(dry_run=False)

        assert any(r.levelno == logging.CRITICAL for r in caplog.records)

    def test_mismatch_does_not_delete_phase_params(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)
        chunking_yaml = _write_phase_param(work_dir, "chunking.yaml")

        phase = _make_phase(tmp_path, src, force=False)
        phase.run(dry_run=False)

        assert chunking_yaml.exists()


# ---------------------------------------------------------------------------
# Source mismatch — execute with --force (force_wipe propagation)
# ---------------------------------------------------------------------------

class TestJobPhaseSourceMismatchWithForce:
    def test_mismatch_with_force_returns_completed(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)
        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)
        assert result.is_complete is True

    def test_mismatch_with_force_sets_force_wipe_true(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)
        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)
        assert result.force_wipe is True

    def test_mismatch_with_force_does_not_delete_phase_param_yamls(self, tmp_path: Path) -> None:
        """JobPhase must NOT delete other phases' files — that is each phase's own responsibility.

        Downstream phases read ``force_wipe=True`` from ``JobPhase.result`` and
        wipe their own artifacts in their own ``_recover()`` methods.
        """
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)

        extraction_yaml   = _write_phase_param(work_dir, "extraction.yaml")
        chunking_yaml     = _write_phase_param(work_dir, "chunking.yaml")
        optimization_yaml = _write_phase_param(work_dir, "optimization.yaml")
        encoding_yaml     = _write_phase_param(work_dir, "encoding.yaml")

        phase = _make_phase(tmp_path, src, force=True)
        phase.run(dry_run=False)

        # JobPhase must leave these intact — downstream phases own their own cleanup
        assert extraction_yaml.exists()
        assert chunking_yaml.exists()
        assert optimization_yaml.exists()
        assert encoding_yaml.exists()

    def test_mismatch_with_force_overwrites_job_yaml_with_new_source(self, tmp_path: Path) -> None:
        """On --force + mismatch, job.yaml must be overwritten with current source metadata."""
        src = _make_source(tmp_path, size=1024)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)  # stale size

        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)

        # job.yaml must exist and carry the real current file size
        assert (work_dir / "job.yaml").exists()
        assert result.job is not None
        assert result.job.source._file_size_bytes == src.stat().st_size

    def test_mismatch_with_force_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, file_size=9999)
        phase = _make_phase(tmp_path, src, force=True)

        with caplog.at_level(logging.WARNING):
            phase.run(dry_run=False)

        assert any(
            "force" in r.message.lower() or "wip" in r.message.lower()
            for r in caplog.records
        )

    def test_no_mismatch_force_wipe_false(self, tmp_path: Path) -> None:
        """When source matches, force_wipe must be False even with --force flag."""
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src)  # correct size
        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)
        assert result.force_wipe is False
