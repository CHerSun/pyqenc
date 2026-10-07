"""Unit tests for recovery state classification and yaml_utils.

Covers:
- 10.1  ArtifactState classification in _recover_encoding_attempts — the
        static-name consumption over one listing per strategy dir: COMPLETE
        iff both ``<chunk_id>.mkv`` and ``<chunk_id>.yaml`` are present;
        PARTIAL when only the winner file exists (the conclusion record is
        missing); ABSENT when neither is.
- 10.2  write_yaml_atomic: .tmp cleanup on failure; _resolve_tmp_paths
        ValueError when output path not in cmd.
"""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from pyqenc.app_config import load_app_config
from pyqenc.phases.encoding import _recover_encoding_attempts as recover_attempts
from pyqenc.state import ArtifactState
from pyqenc.stream_model import EncodedChunk
from pyqenc.utils.yaml_utils import write_yaml_atomic

_STRATEGY_OBJ = next(
    s for s in load_app_config(default_only=True).resolve_encoding().strategies
    if s.preset == "slow" and s.profile == "h265-aq"
)
_CHUNK_ID   = "00꞉00꞉00․000-00꞉00꞉13․330"
_STRATEGY   = _STRATEGY_OBJ.display_name()
_SAFE_STRAT = _STRATEGY_OBJ.safe_name()
_CRF        = Decimal("20.5")


def _encoded_dir(work_dir: Path) -> Path:
    return work_dir / "encoded" / _SAFE_STRAT


def _make_complete_pair(
    encoded_dir: Path,
    chunk_id:    str     = _CHUNK_ID,
    crf:         Decimal = _CRF,
) -> Path:
    """Write a winner .mkv + result sidecar into encoded_dir.

    Layout mirrors the real encoded/ directory (static names):
      <chunk_id>.mkv   — the promoted winner
      <chunk_id>.yaml  — the winner result sidecar (stem swap)
    """
    encoded_dir.mkdir(parents=True, exist_ok=True)
    mkv     = encoded_dir / EncodedChunk.format_winner_file_name(chunk_id)
    sidecar = encoded_dir / EncodedChunk.format_winner_sidecar_name(chunk_id)
    mkv.write_bytes(b"\x00" * 64)
    sidecar.write_text(
        yaml.dump({"crf": str(crf), "targets_met": True, "metrics": {"vmaf_min": 95.0}}),
        encoding="utf-8",
    )
    return mkv


# ---------------------------------------------------------------------------
# 10.1  ArtifactState classification
# ---------------------------------------------------------------------------

class TestRecoverAttemptsAbsent:
    """ABSENT: no winner mkv+yaml in encoded/."""

    def test_absent_when_no_encoded_dir(self, tmp_path: Path) -> None:
        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT

    def test_absent_pair_is_in_pending(self, tmp_path: Path) -> None:
        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        assert (_CHUNK_ID, _STRATEGY) in result.pending

    def test_partial_when_mkv_present_but_no_sidecar(self, tmp_path: Path) -> None:
        """A winner .mkv without its .yaml sidecar is PARTIAL — the product
        exists but the conclusion record (which makes presence trustworthy)
        is missing."""
        out_dir = _encoded_dir(tmp_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / EncodedChunk.format_winner_file_name(_CHUNK_ID)).write_bytes(b"\x00" * 64)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.PARTIAL
        assert (_CHUNK_ID, _STRATEGY) in result.pending

    def test_absent_when_only_encoding_dir_has_attempts(self, tmp_path: Path) -> None:
        """Attempt files in encoding/ (no winner in encoded/) → ABSENT —
        attempts are the substrate, never classified."""
        enc_dir = tmp_path / "encoding" / _SAFE_STRAT
        enc_dir.mkdir(parents=True, exist_ok=True)
        (enc_dir / EncodedChunk.format_attempt_file_name(_CHUNK_ID, _CRF)).write_bytes(b"\x00" * 64)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT


class TestRecoverAttemptsComplete:
    """COMPLETE: winner .mkv + .yaml sidecar present in encoded/."""

    def test_complete_with_mkv_and_sidecar(self, tmp_path: Path) -> None:
        out_dir = _encoded_dir(tmp_path)
        winning = _make_complete_pair(out_dir)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state        == ArtifactState.COMPLETE
        assert pair.winning_file == winning

    def test_complete_pair_not_in_pending(self, tmp_path: Path) -> None:
        out_dir = _encoded_dir(tmp_path)
        _make_complete_pair(out_dir)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        assert (_CHUNK_ID, _STRATEGY) not in result.pending

    def test_winning_file_exists(self, tmp_path: Path) -> None:
        out_dir = _encoded_dir(tmp_path)
        _make_complete_pair(out_dir)

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        pair = result.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.winning_file is not None
        assert pair.winning_file.exists()

    def test_legacy_q_bearing_winner_name_is_not_complete(self, tmp_path: Path) -> None:
        """Pre-alpha break (Req 1): a q-bearing winner file from an old
        workdir matches NO static expected name — the pair re-derives."""
        out_dir = _encoded_dir(tmp_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / EncodedChunk.format_attempt_file_name(_CHUNK_ID, _CRF)).write_bytes(b"\x00" * 64)
        (out_dir / EncodedChunk.format_attempt_sidecar_name(_CHUNK_ID, _CRF)).write_text(
            yaml.dump({"crf": str(_CRF)}), encoding="utf-8",
        )

        result = recover_attempts(tmp_path, [_CHUNK_ID], [_STRATEGY_OBJ])
        assert result.pairs[(_CHUNK_ID, _STRATEGY)].state == ArtifactState.ABSENT


class TestRecoverAttemptsMultiplePairs:
    """Mixed states across multiple (chunk_id, strategy) pairs."""

    def test_mixed_states_counted_correctly(self, tmp_path: Path) -> None:
        chunk_a = "00꞉00꞉00․000-00꞉00꞉10․000"
        chunk_b = "00꞉00꞉10․000-00꞉00꞉20․000"
        out_dir = _encoded_dir(tmp_path)
        _make_complete_pair(out_dir, chunk_id=chunk_a)
        # chunk_b: nothing written

        result = recover_attempts(
            tmp_path, [chunk_a, chunk_b], [_STRATEGY_OBJ]
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

        with (
            patch("pyqenc.utils.yaml_utils.yaml.dump", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
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
