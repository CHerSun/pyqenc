"""Unit tests for JobPhase source identity, permission, and job.yaml currency.

Covers:
- run() dry-run: returns PENDING when job.yaml absent, REUSED when present
- run() execute: creates job.yaml on first run (COMPLETED)
- job.yaml persists the one human-facing record (path + sampled fingerprint)
- Content-identity mismatch without --force: FAILED, message names --force
- Content-identity mismatch with --force (permission): COMPLETED, job.yaml
  rewritten for the new source — no propagated wipe order exists (Req 35a)
- Path-only change with matching content: locator update (rewrite, no fatal,
  no force) — Req 34
- The raw --force flag rides the result unchanged (permission, never a wipe
  command)
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from pyqenc.app_config import load_app_config
from pyqenc.models import (
    CleanupLevel,
    PhaseOutcome,
    QualityTarget,
)
from pyqenc.phases.job import JobPhase
from pyqenc.state import ArtifactState
from pyqenc.stream_model import File, JobSidecar, JobSourceRecord
from pyqenc.utils.yaml_utils import write_yaml_atomic

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUALITY_TARGETS = [QualityTarget(metric="vmaf", statistic="min", value=93.0)]
_APP_CONFIG      = load_app_config(default_only=True)


def _make_source(tmp_path: Path, size: int = 1024) -> Path:
    """Create a fake source video file."""
    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * size)
    return src


def _make_phase(
    tmp_path: Path,
    source: Path,
    force: bool = False,
) -> JobPhase:
    config = _APP_CONFIG.model_copy(deep=True)
    return JobPhase(
        config, {},
        source      = source,
        work_dir    = tmp_path / "work",
        force       = force,
        cleanup     = CleanupLevel.NONE,
        no_metrics  = True,
        collector   = MagicMock(),
    )


def _persist_job(
    work_dir: Path,
    source: Path,
    *,
    fingerprint_of: Path | None = None,
    as_path: Path | None = None,
) -> None:
    """Write a job.yaml record.

    By default the record matches *source* (its real sampled fingerprint and
    path). ``fingerprint_of`` records a DIFFERENT file's identity (the
    mismatch fixture); ``as_path`` records a different locator (the
    locator-update fixture).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    content = fingerprint_of if fingerprint_of is not None else source
    sidecar = JobSidecar(source=JobSourceRecord(
        path        = as_path if as_path is not None else source,
        fingerprint = File.sampled_fingerprint(content),
    ))
    write_yaml_atomic(work_dir / "job.yaml", sidecar.model_dump(exclude_none=True))


# ---------------------------------------------------------------------------
# run() dry-run mode
# ---------------------------------------------------------------------------

class TestJobPhaseRunDryRun:
    def test_dry_run_absent_probes_but_writes_no_file(self, tmp_path: Path) -> None:
        """Dry-run on a fresh work-dir establishes the File (read-only) without writing job.yaml.

        Bug guarded: if JobPhase returned PENDING (or otherwise not-complete) in
        dry-run, the whole dry-run pipeline would cascade to "pending at: Job"
        and never preview any downstream phase — Job is run SETUP, not pipeline
        work, so a dry-run must proceed past it. Only the job.yaml WRITE is
        skipped.
        """
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=True)
        assert result.is_complete is True
        assert result.file is not None
        assert not (work_dir / "job.yaml").exists()

    def test_dry_run_existing_returns_reused(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src)
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=True)
        assert result.outcome == PhaseOutcome.REUSED
        assert result.is_complete is True

    def test_dry_run_mismatch_fails_actionably(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dry-run on a mismatched source fails with the actionable error.

        Contract: recovery raises a fatal ``RecoveryError`` on a content
        mismatch without ``--force`` regardless of dry-run — previewing
        "success" against stale state would be misleading. The error names
        the mismatch and points at ``--force`` truthfully.
        """
        src = _make_source(tmp_path)
        other = tmp_path / "other.bin"
        other.write_bytes(b"\xff" * 1024)  # same size, different content
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)
        phase = _make_phase(tmp_path, src)

        with caplog.at_level(logging.CRITICAL):
            result = phase.run(dry_run=True)

        assert result.outcome == PhaseOutcome.FAILED
        assert result.message
        assert "identity mismatch" in result.message.lower()
        assert "--force" in result.message
        assert any(
            r.levelno == logging.CRITICAL and "mismatch" in r.message.lower()
            for r in caplog.records
        )


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

    def test_job_yaml_persists_locator_and_fingerprint(self, tmp_path: Path) -> None:
        """Bug prevented: job.yaml regrowing cached fast metadata — the
        record is the locator + the sampled content identity (the size lives
        inside the fingerprint as its belt) and nothing else (Req 31)."""
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        phase.run(dry_run=False)

        data = yaml.safe_load((tmp_path / "work" / "job.yaml").read_text(encoding="utf-8"))
        assert set(data) == {"source"}
        assert set(data["source"]) == {"path", "fingerprint"}
        assert data["source"]["path"] == str(src)
        assert data["source"]["fingerprint"]["size"] == src.stat().st_size
        assert data["source"]["fingerprint"]["token"]

    def test_result_carries_eager_file(self, tmp_path: Path) -> None:
        """JobPhaseResult exposes the run's single File — path + size +
        fingerprint from the filesystem, established eagerly."""
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=False)
        assert result.file is not None
        assert result.file.state == ArtifactState.COMPLETE
        assert result.file.payload.path == src
        assert result.file.payload.file_size_bytes == src.stat().st_size
        assert result.file.payload.fingerprint is not None
        assert result.file.payload.fingerprint.size == src.stat().st_size

    def test_reused_result_carries_file_too(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src)
        phase = _make_phase(tmp_path, src)
        result = phase.run(dry_run=False)
        assert result.outcome == PhaseOutcome.REUSED
        assert result.file is not None
        assert result.file.state == ArtifactState.COMPLETE
        assert result.file.payload.path == src

    def test_first_run_force_flag_rides_result(self, tmp_path: Path) -> None:
        """The raw --force permission rides the result verbatim (Req 35a) —
        it never becomes a derived wipe order."""
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)
        assert result.force is True

    def test_second_run_reuses(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        phase = _make_phase(tmp_path, src)
        phase.run(dry_run=False)

        # Reset cached result and run again
        phase.result = None
        result = phase.run(dry_run=False)
        assert result.is_complete is True
        assert result.outcome == PhaseOutcome.REUSED


# ---------------------------------------------------------------------------
# Locator update — path changed, content identical (Req 34)
# ---------------------------------------------------------------------------

class TestJobPhaseLocatorUpdate:
    def test_path_only_change_rewrites_locator_without_force(self, tmp_path: Path) -> None:
        """A moved source with unchanged content is a locator update: job.yaml
        is rewritten, no fatal, no --force, no invalidation.

        Bug guarded (J-3): the old behavior demanded --force and wiped the
        workdir for a zero-content change.
        """
        src = _make_source(tmp_path)
        moved = tmp_path / "moved.mkv"
        moved.write_bytes(src.read_bytes())  # same content, different path
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, as_path=moved)
        phase = _make_phase(tmp_path, src, force=False)

        result = phase.run(dry_run=False)

        assert result.is_complete is True
        assert result.outcome == PhaseOutcome.COMPLETED  # the rewrite ran
        data = yaml.safe_load((work_dir / "job.yaml").read_text(encoding="utf-8"))
        assert data["source"]["path"] == str(src)


# ---------------------------------------------------------------------------
# Content-identity mismatch — execute without --force
# ---------------------------------------------------------------------------

class TestJobPhaseIdentityMismatchNoForce:
    @staticmethod
    def _mismatched(tmp_path: Path) -> tuple[Path, Path]:
        src = _make_source(tmp_path)
        other = tmp_path / "other.bin"
        other.write_bytes(b"\xff" * 1024)  # same size, different bytes
        return src, other

    def test_mismatch_returns_failed(self, tmp_path: Path) -> None:
        src, other = self._mismatched(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)
        phase = _make_phase(tmp_path, src, force=False)
        result = phase.run(dry_run=False)
        assert result.outcome == PhaseOutcome.FAILED
        assert result.is_complete is False

    def test_mismatch_flag_stays_raw_false_on_failure(self, tmp_path: Path) -> None:
        src, other = self._mismatched(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)
        phase = _make_phase(tmp_path, src, force=False)
        result = phase.run(dry_run=False)
        assert result.force is False

    def test_mismatch_logs_critical(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        src, other = self._mismatched(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)
        phase = _make_phase(tmp_path, src, force=False)

        with caplog.at_level(logging.CRITICAL):
            phase.run(dry_run=False)

        assert any(r.levelno == logging.CRITICAL for r in caplog.records)


# ---------------------------------------------------------------------------
# Content-identity mismatch — execute with --force (permission)
# ---------------------------------------------------------------------------

class TestJobPhaseIdentityMismatchWithForce:
    @staticmethod
    def _mismatched(tmp_path: Path, size: int = 1024) -> tuple[Path, Path]:
        src = _make_source(tmp_path, size=size)
        other = tmp_path / "other.bin"
        other.write_bytes(b"\xff" * size)
        return src, other

    def test_mismatch_with_permission_returns_completed(self, tmp_path: Path) -> None:
        src, other = self._mismatched(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)
        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)
        assert result.is_complete is True

    def test_permission_flag_rides_result(self, tmp_path: Path) -> None:
        """The raw flag rides the result even as the phase consumes the
        permission — downstream phases re-read it for their own fatal-band
        conditions (each phase detects its own mismatch from its own key)."""
        src, other = self._mismatched(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)
        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)
        assert result.force is True

    def test_mismatch_with_permission_overwrites_job_yaml(self, tmp_path: Path) -> None:
        """With permission, job.yaml is rewritten with the new source's
        identity — downstream phases then see their own keys mismatch (no
        propagated wipe order; idempotent across crashes, Req 33)."""
        src, other = self._mismatched(tmp_path, size=1024)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)

        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)

        assert (work_dir / "job.yaml").exists()
        assert result.file is not None
        assert result.file.state == ArtifactState.COMPLETE
        data = yaml.safe_load((work_dir / "job.yaml").read_text(encoding="utf-8"))
        assert data["source"]["fingerprint"]["token"] == File.sampled_fingerprint(src).token

    def test_mismatch_with_permission_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        src, other = self._mismatched(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src, fingerprint_of=other)
        phase = _make_phase(tmp_path, src, force=True)

        with caplog.at_level(logging.WARNING):
            phase.run(dry_run=False)

        assert any(
            "mismatch" in r.message.lower() and "force" in r.message.lower()
            for r in caplog.records
        )

    def test_no_mismatch_flag_rides_without_firing_anything(self, tmp_path: Path) -> None:
        """--force with a matching identity changes nothing — permission
        alone never causes a wipe (J-2 fix)."""
        src = _make_source(tmp_path)
        work_dir = tmp_path / "work"
        _persist_job(work_dir, src)
        phase = _make_phase(tmp_path, src, force=True)
        result = phase.run(dry_run=False)
        assert result.outcome == PhaseOutcome.REUSED
        assert result.force is True
