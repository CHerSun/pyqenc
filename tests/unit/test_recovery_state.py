"""Unit tests for recovery state classification, yaml_utils, and JobStateManager.validate.

Covers:
- 10.1  ArtifactState classification in recover_attempts (ABSENT / ARTIFACT_ONLY / COMPLETE)
         with and without sidecars
- 10.2  write_yaml_atomic: .tmp cleanup on failure; _resolve_tmp_paths ValueError when
         output path not in cmd
- 10.3  JobStateManager.validate: dry-run warning, execute critical stop, --force wipe-and-continue
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from pyqenc.models import QualityTarget, VideoMetadata
from pyqenc.phases.recovery import recover_attempts
from pyqenc.state import ArtifactState, JobState, JobStateManager
from pyqenc.utils.yaml_utils import write_yaml_atomic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHUNK_ID  = "00꞉00꞉00․000-00꞉00꞉13․330"
_STRATEGY  = "slow+h265-aq"
_SAFE_STRAT = "slow_h265-aq"
_RESOLUTION = "1920x800"
_QUALITY_TARGETS: list[QualityTarget] = [
    QualityTarget(metric="vmaf", statistic="min", value=93.0),
]


def _strategy_dir(work_dir: Path) -> Path:
    return work_dir / "encoded" / _SAFE_STRAT


def _attempt_path(strategy_dir: Path, crf: float = 20.0) -> Path:
    return strategy_dir / f"{_CHUNK_ID}.{_RESOLUTION}.crf{crf}.mkv"


def _attempt_sidecar(strategy_dir: Path, crf: float = 20.0) -> Path:
    return strategy_dir / f"{_CHUNK_ID}.{_RESOLUTION}.crf{crf}.yaml"


def _result_sidecar(strategy_dir: Path) -> Path:
    return strategy_dir / f"{_CHUNK_ID}.{_RESOLUTION}.yaml"


def _write_attempt(strategy_dir: Path, crf: float = 20.0, with_sidecar: bool = True) -> Path:
    """Write a fake attempt .mkv and optionally its metrics sidecar."""
    strategy_dir.mkdir(parents=True, exist_ok=True)
    attempt = _attempt_path(strategy_dir, crf)
    attempt.write_bytes(b"\x00" * 64)
    if with_sidecar:
        sidecar_data = {
            "crf": crf,
            "targets_met": True,
            "metrics": {"vmaf_min": 95.0},
        }
        _attempt_sidecar(strategy_dir, crf).write_text(
            yaml.dump(sidecar_data), encoding="utf-8"
        )
    return attempt


def _write_result_sidecar(strategy_dir: Path, crf: float = 20.0) -> None:
    """Write a valid encoding result sidecar."""
    attempt_name = _attempt_path(strategy_dir, crf).name
    data = {
        "winning_attempt": attempt_name,
        "crf": crf,
        "quality_targets": ["vmaf-min:93"],
        "metrics": {"vmaf_min": 95.0},
    }
    _result_sidecar(strategy_dir).write_text(yaml.dump(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# 10.1  ArtifactState classification in recover_attempts
# ---------------------------------------------------------------------------

class TestRecoverAttemptsAbsent:
    """ABSENT: no attempt files exist for the pair."""

    def test_absent_when_no_encoded_dir(self, tmp_path: Path) -> None:
        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT

    def test_absent_pair_is_in_pending(self, tmp_path: Path) -> None:
        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        assert (_CHUNK_ID, _STRATEGY) in result.pending

    def test_absent_history_is_empty(self, tmp_path: Path) -> None:
        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert len(pair.attempts) == 0


class TestRecoverAttemptsArtifactOnly:
    """ARTIFACT_ONLY: attempt .mkv exists but no encoding result sidecar."""

    def test_artifact_only_without_attempt_sidecar(self, tmp_path: Path) -> None:
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=False)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ARTIFACT_ONLY

    def test_artifact_only_with_attempt_sidecar_no_result_sidecar(self, tmp_path: Path) -> None:
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=True)
        # No result sidecar written

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ARTIFACT_ONLY

    def test_artifact_only_pair_is_in_pending(self, tmp_path: Path) -> None:
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=True)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        assert (_CHUNK_ID, _STRATEGY) in result.pending

    def test_artifact_only_history_reconstructed_from_sidecar(self, tmp_path: Path) -> None:
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=True)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        # CRFHistory should have the attempt recorded
        assert len(pair.attempts) == 1
        assert pair.attempts[0].crf == 20.0


class TestRecoverAttemptsComplete:
    """COMPLETE: attempt .mkv + encoding result sidecar both present and valid."""

    def test_complete_with_result_sidecar(self, tmp_path: Path) -> None:
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=True)
        _write_result_sidecar(strat_dir, crf=20.0)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.COMPLETE

    def test_complete_pair_not_in_pending(self, tmp_path: Path) -> None:
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=True)
        _write_result_sidecar(strat_dir, crf=20.0)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        assert (_CHUNK_ID, _STRATEGY) not in result.pending

    def test_complete_without_attempt_sidecar_still_complete(self, tmp_path: Path) -> None:
        """Result sidecar alone is sufficient for COMPLETE — attempt sidecar is optional."""
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=False)
        _write_result_sidecar(strat_dir, crf=20.0)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.COMPLETE

    def test_complete_downgrades_to_artifact_only_when_targets_change(self, tmp_path: Path) -> None:
        """If quality targets tighten, a previously COMPLETE pair becomes ARTIFACT_ONLY."""
        strat_dir = _strategy_dir(tmp_path)
        _write_attempt(strat_dir, crf=20.0, with_sidecar=True)
        _write_result_sidecar(strat_dir, crf=20.0)

        # Raise the target above what the sidecar recorded (vmaf_min=95)
        stricter = [QualityTarget(metric="vmaf", statistic="min", value=99.0)]
        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], stricter)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ARTIFACT_ONLY


class TestRecoverAttemptsMultiplePairs:
    """Mixed states across multiple (chunk_id, strategy) pairs."""

    def test_mixed_states_counted_correctly(self, tmp_path: Path) -> None:
        chunk_a = "00꞉00꞉00․000-00꞉00꞉10․000"
        chunk_b = "00꞉00꞉10․000-00꞉00꞉20․000"
        strat_dir = _strategy_dir(tmp_path)

        # chunk_a: COMPLETE
        attempt_a = strat_dir / f"{chunk_a}.{_RESOLUTION}.crf20.0.mkv"
        strat_dir.mkdir(parents=True, exist_ok=True)
        attempt_a.write_bytes(b"\x00" * 64)
        (strat_dir / f"{chunk_a}.{_RESOLUTION}.crf20.0.yaml").write_text(
            yaml.dump({"crf": 20.0, "targets_met": True, "metrics": {"vmaf_min": 95.0}}),
            encoding="utf-8",
        )
        (strat_dir / f"{chunk_a}.{_RESOLUTION}.yaml").write_text(
            yaml.dump({
                "winning_attempt": attempt_a.name,
                "crf": 20.0,
                "quality_targets": ["vmaf-min:93"],
                "metrics": {"vmaf_min": 95.0},
            }),
            encoding="utf-8",
        )

        # chunk_b: ABSENT (nothing written)

        result = recover_attempts(
            tmp_path, [chunk_a, chunk_b], [_STRATEGY], _QUALITY_TARGETS
        )
        assert result.pairs[(chunk_a, _STRATEGY)].state == ArtifactState.COMPLETE
        assert result.pairs[(chunk_b, _STRATEGY)].state == ArtifactState.ABSENT
        assert (chunk_b, _STRATEGY) in result.pending
        assert (chunk_a, _STRATEGY) not in result.pending


# ---------------------------------------------------------------------------
# 10.2  write_yaml_atomic and ffmpeg runner output_file validation
# ---------------------------------------------------------------------------

class TestWriteYamlAtomic:
    def test_writes_valid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "out.yaml"
        write_yaml_atomic(path, {"key": "value", "num": 42})
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert loaded == {"key": "value", "num": 42}

    def test_tmp_file_cleaned_up_on_failure(self, tmp_path: Path) -> None:
        """If an exception occurs during write, the .tmp file must not remain."""
        from unittest.mock import patch

        path = tmp_path / "out.yaml"
        tmp_path_expected = tmp_path / "out.tmp"

        with patch("pyqenc.utils.yaml_utils.yaml.dump", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                write_yaml_atomic(path, {"x": 1})

        assert not tmp_path_expected.exists(), ".tmp file must be deleted on failure"

    def test_final_file_written_not_tmp(self, tmp_path: Path) -> None:
        path = tmp_path / "params.yaml"
        write_yaml_atomic(path, {"a": 1})
        assert path.exists()
        assert not (tmp_path / "params.tmp").exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "out.yaml"
        write_yaml_atomic(path, {"x": 1})
        assert path.exists()


class TestResolveTmpPathsOutputValidation:
    """_resolve_tmp_paths raises ValueError when output path is not in cmd."""

    def test_raises_when_output_not_in_cmd(self) -> None:
        from pyqenc.utils.ffmpeg_runner import _resolve_tmp_paths

        out = Path("/tmp/output.mkv")
        cmd: list = ["ffmpeg", "-i", "input.mkv", "/tmp/other.mkv"]
        with pytest.raises(ValueError, match="not found in ffmpeg cmd"):
            _resolve_tmp_paths(cmd, out)

    def test_no_error_when_output_in_cmd(self) -> None:
        from pyqenc.utils.ffmpeg_runner import _resolve_tmp_paths

        out = Path("/tmp/output.mkv")
        cmd: list = ["ffmpeg", "-i", "input.mkv", str(out)]
        modified_cmd, mapping = _resolve_tmp_paths(cmd, out)
        assert len(mapping) == 1
        assert out in mapping.values()


# ---------------------------------------------------------------------------
# 10.3  JobStateManager.validate — all three modes
# ---------------------------------------------------------------------------

def _make_source(tmp_path: Path, size: int = 1000) -> Path:
    src = tmp_path / "source.mkv"
    src.write_bytes(b"\x00" * size)
    return src


def _make_manager(tmp_path: Path, source: Path, force: bool = False) -> JobStateManager:
    return JobStateManager(work_dir=tmp_path / "work", source_video=source, force=force)


def _persist_job(manager: JobStateManager, source: Path, file_size: int = 1000) -> None:
    """Write a job.yaml with a pre-populated source VideoMetadata."""
    vm = VideoMetadata(path=source)
    vm._file_size_bytes = file_size
    manager.work_dir.mkdir(parents=True, exist_ok=True)
    manager.save_job(JobState(source=vm))


class TestJobStateManagerValidateDryRun:
    """Dry-run mode: mismatch logs a warning but returns True (no action taken)."""

    def test_no_job_yaml_returns_true(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src)
        assert mgr.validate(dry_run=True) is True

    def test_matching_source_returns_true(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src)
        _persist_job(mgr, src, file_size=src.stat().st_size)
        assert mgr.validate(dry_run=True) is True

    def test_mismatch_returns_true_in_dry_run(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src)
        # Persist with wrong file size
        _persist_job(mgr, src, file_size=9999)
        assert mgr.validate(dry_run=True) is True

    def test_mismatch_logs_warning_in_dry_run(self, tmp_path: Path, caplog) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src)
        _persist_job(mgr, src, file_size=9999)

        with caplog.at_level(logging.WARNING, logger="pyqenc.state"):
            mgr.validate(dry_run=True)

        assert any("mismatch" in r.message.lower() for r in caplog.records)


class TestJobStateManagerValidateExecuteNoForce:
    """Execute mode without --force: mismatch logs critical and returns False."""

    def test_mismatch_returns_false(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src, force=False)
        _persist_job(mgr, src, file_size=9999)
        assert mgr.validate(dry_run=False) is False

    def test_mismatch_logs_critical(self, tmp_path: Path, caplog) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src, force=False)
        _persist_job(mgr, src, file_size=9999)

        with caplog.at_level(logging.CRITICAL, logger="pyqenc.state"):
            mgr.validate(dry_run=False)

        assert any(r.levelno == logging.CRITICAL for r in caplog.records)

    def test_no_artifacts_deleted_without_force(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src, force=False)
        _persist_job(mgr, src, file_size=9999)

        chunks_dir = mgr.work_dir / "chunks"
        chunks_dir.mkdir(parents=True)
        sentinel = chunks_dir / "chunk.mkv"
        sentinel.write_bytes(b"\x00" * 64)

        mgr.validate(dry_run=False)
        assert sentinel.exists()


class TestJobStateManagerValidateForce:
    """Execute mode with --force: mismatch wipes artifacts and returns True."""

    def test_mismatch_with_force_returns_true(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src, force=True)
        _persist_job(mgr, src, file_size=9999)
        assert mgr.validate(dry_run=False) is True

    def test_force_deletes_artifact_dirs(self, tmp_path: Path) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src, force=True)
        _persist_job(mgr, src, file_size=9999)

        chunks_dir = mgr.work_dir / "chunks"
        chunks_dir.mkdir(parents=True)
        (chunks_dir / "chunk.mkv").write_bytes(b"\x00" * 64)

        mgr.validate(dry_run=False)
        assert not chunks_dir.exists()

    def test_force_logs_warning(self, tmp_path: Path, caplog) -> None:
        src = _make_source(tmp_path)
        mgr = _make_manager(tmp_path, src, force=True)
        _persist_job(mgr, src, file_size=9999)

        with caplog.at_level(logging.WARNING, logger="pyqenc.state"):
            mgr.validate(dry_run=False)

        assert any("force" in r.message.lower() or "wip" in r.message.lower()
                   or "wiping" in r.message.lower() for r in caplog.records)
