"""Unit tests for recovery state classification and yaml_utils.

Covers:
- 10.1  ArtifactState classification in recover_attempts (ABSENT / ARTIFACT_ONLY / COMPLETE)
         with and without sidecars
- 10.2  write_yaml_atomic: .tmp cleanup on failure; _resolve_tmp_paths ValueError when
         output path not in cmd
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pyqenc.models import QualityTarget, VideoMetadata
from pyqenc.phases.encoding import _recover_encoding_attempts as recover_attempts
from pyqenc.state import ArtifactState
from pyqenc.utils.yaml_utils import write_yaml_atomic

_CHUNK_ID  = "00꞉00꞉00․000-00꞉00꞉13․330"
_STRATEGY  = "slow+h265-aq"
_SAFE_STRAT = "slow_h265-aq"
_RESOLUTION = "1920x800"
_QUALITY_TARGETS: list[QualityTarget] = [
    QualityTarget(metric="vmaf", statistic="min", value=93.0),
]


def _encoding_dir(work_dir: Path) -> Path:
    """CRF search workspace: encoding/<safe_strategy>/"""
    return work_dir / "encoding" / _SAFE_STRAT


def _encoded_dir(work_dir: Path) -> Path:
    """Finalized artifact output: encoded/<safe_strategy>/"""
    return work_dir / "encoded" / _SAFE_STRAT


def _attempt_path(encoding_dir: Path, crf: float = 20.0) -> Path:
    return encoding_dir / f"{_CHUNK_ID}.{_RESOLUTION}.crf{crf}.mkv"


def _attempt_sidecar(encoding_dir: Path, crf: float = 20.0) -> Path:
    return encoding_dir / f"{_CHUNK_ID}.{_RESOLUTION}.crf{crf}.yaml"


def _result_sidecar(encoded_dir: Path) -> Path:
    return encoded_dir / f"{_CHUNK_ID}.{_RESOLUTION}.yaml"


def _write_attempt(encoding_dir: Path, crf: float = 20.0, with_sidecar: bool = True) -> Path:
    """Write a fake attempt .mkv (and optionally its metrics sidecar) to encoding/."""
    encoding_dir.mkdir(parents=True, exist_ok=True)
    attempt = _attempt_path(encoding_dir, crf)
    attempt.write_bytes(b"\x00" * 64)
    if with_sidecar:
        sidecar_data = {
            "crf": crf,
            "targets_met": True,
            "metrics": {"vmaf_min": 95.0},
        }
        _attempt_sidecar(encoding_dir, crf).write_text(
            yaml.dump(sidecar_data), encoding="utf-8"
        )
    return attempt


def _write_result_sidecar(encoded_dir: Path, winning_attempt_path: Path, crf: float = 20.0) -> None:
    """Write a valid encoding result sidecar to encoded/ referencing the winning attempt."""
    encoded_dir.mkdir(parents=True, exist_ok=True)
    # Hard-link (or copy) the winning attempt into encoded/ so the sidecar reference resolves
    dst = encoded_dir / winning_attempt_path.name
    if not dst.exists():
        import shutil
        shutil.copy2(winning_attempt_path, dst)
    data = {
        "winning_attempt": winning_attempt_path.name,
        "crf": crf,
        "quality_targets": ["vmaf-min:93"],
        "metrics": {"vmaf_min": 95.0},
    }
    _result_sidecar(encoded_dir).write_text(yaml.dump(data), encoding="utf-8")


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
    """ARTIFACT_ONLY: attempt .mkv exists in encoding/ but no result sidecar in encoded/."""

    def test_artifact_only_without_attempt_sidecar(self, tmp_path: Path) -> None:
        enc_dir = _encoding_dir(tmp_path)
        _write_attempt(enc_dir, crf=20.0, with_sidecar=False)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ARTIFACT_ONLY

    def test_artifact_only_with_attempt_sidecar_no_result_sidecar(self, tmp_path: Path) -> None:
        enc_dir = _encoding_dir(tmp_path)
        _write_attempt(enc_dir, crf=20.0, with_sidecar=True)
        # No result sidecar written

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ARTIFACT_ONLY

    def test_artifact_only_pair_is_in_pending(self, tmp_path: Path) -> None:
        enc_dir = _encoding_dir(tmp_path)
        _write_attempt(enc_dir, crf=20.0, with_sidecar=True)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        assert (_CHUNK_ID, _STRATEGY) in result.pending

    def test_artifact_only_history_reconstructed_from_sidecar(self, tmp_path: Path) -> None:
        enc_dir = _encoding_dir(tmp_path)
        _write_attempt(enc_dir, crf=20.0, with_sidecar=True)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert len(pair.attempts) == 1
        assert pair.attempts[0].crf == 20.0


class TestRecoverAttemptsComplete:
    """COMPLETE: winning attempt hard-linked to encoded/ + result sidecar present."""

    def test_complete_with_result_sidecar(self, tmp_path: Path) -> None:
        enc_dir  = _encoding_dir(tmp_path)
        out_dir  = _encoded_dir(tmp_path)
        attempt  = _write_attempt(enc_dir, crf=20.0, with_sidecar=True)
        _write_result_sidecar(out_dir, attempt, crf=20.0)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.COMPLETE

    def test_complete_pair_not_in_pending(self, tmp_path: Path) -> None:
        enc_dir  = _encoding_dir(tmp_path)
        out_dir  = _encoded_dir(tmp_path)
        attempt  = _write_attempt(enc_dir, crf=20.0, with_sidecar=True)
        _write_result_sidecar(out_dir, attempt, crf=20.0)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        assert (_CHUNK_ID, _STRATEGY) not in result.pending

    def test_complete_without_attempt_sidecar_still_complete(self, tmp_path: Path) -> None:
        """Result sidecar in encoded/ alone is sufficient for COMPLETE."""
        enc_dir  = _encoding_dir(tmp_path)
        out_dir  = _encoded_dir(tmp_path)
        attempt  = _write_attempt(enc_dir, crf=20.0, with_sidecar=False)
        _write_result_sidecar(out_dir, attempt, crf=20.0)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], _QUALITY_TARGETS)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.COMPLETE

    def test_complete_downgrades_to_artifact_only_when_targets_change(self, tmp_path: Path) -> None:
        """If quality targets tighten, a previously COMPLETE pair becomes ARTIFACT_ONLY."""
        enc_dir  = _encoding_dir(tmp_path)
        out_dir  = _encoded_dir(tmp_path)
        attempt  = _write_attempt(enc_dir, crf=20.0, with_sidecar=True)
        _write_result_sidecar(out_dir, attempt, crf=20.0)

        stricter = [QualityTarget(metric="vmaf", statistic="min", value=99.0)]
        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY], stricter)
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ARTIFACT_ONLY


class TestRecoverAttemptsMultiplePairs:
    """Mixed states across multiple (chunk_id, strategy) pairs."""

    def test_mixed_states_counted_correctly(self, tmp_path: Path) -> None:
        chunk_a = "00꞉00꞉00․000-00꞉00꞉10․000"
        chunk_b = "00꞉00꞉10․000-00꞉00꞉20․000"
        enc_dir = _encoding_dir(tmp_path)
        out_dir = _encoded_dir(tmp_path)

        # chunk_a: COMPLETE — attempt in encoding/, result sidecar + winning link in encoded/
        enc_dir.mkdir(parents=True, exist_ok=True)
        attempt_a = enc_dir / f"{chunk_a}.{_RESOLUTION}.crf20.0.mkv"
        attempt_a.write_bytes(b"\x00" * 64)
        (enc_dir / f"{chunk_a}.{_RESOLUTION}.crf20.0.yaml").write_text(
            yaml.dump({"crf": 20.0, "targets_met": True, "metrics": {"vmaf_min": 95.0}}),
            encoding="utf-8",
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(attempt_a, out_dir / attempt_a.name)
        (out_dir / f"{chunk_a}.{_RESOLUTION}.yaml").write_text(
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
