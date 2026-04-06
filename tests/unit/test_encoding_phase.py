"""Unit tests for encoding phase recovery.

Covers the fast presence-based recovery in ``_recover_encoding_attempts``:
- A pair is COMPLETE when both a winning .mkv and its .yaml sidecar exist in
  ``encoded/<strategy>/``.
- A pair is ABSENT when neither is present.
- ``winning_file`` is populated on COMPLETE pairs.
- The index is built from a single ``iterdir()`` per strategy — no per-pair globs.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pyqenc.phases.encoding import _recover_encoding_attempts
from pyqenc.state import ArtifactState
from pyqenc.utils.yaml_utils import write_yaml_atomic


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_CHUNK_ID   = "00꞉00꞉00․000-00꞉01꞉30․000"
_STRATEGY   = "slow+h265-aq"
_SAFE_STRAT = "slow_h265-aq"
_RESOLUTION = "1920x800"
_CRF        = Decimal("18.0")


def _make_complete_pair(encoded_dir: Path, chunk_id: str = _CHUNK_ID, crf: Decimal = _CRF) -> Path:
    """Write a winning .mkv and its result sidecar into encoded_dir.

    Layout mirrors the real encoded/ directory:
      <chunk_id>.<res>.crf<N>.mkv   — winning attempt
      <chunk_id>.<res>.yaml          — result sidecar (no crf in name)
    """
    encoded_dir.mkdir(parents=True, exist_ok=True)
    mkv     = encoded_dir / f"{chunk_id}.{_RESOLUTION}.crf{crf}.mkv"
    sidecar = encoded_dir / f"{chunk_id}.{_RESOLUTION}.yaml"
    mkv.write_bytes(b"\x00" * 512)
    write_yaml_atomic(sidecar, {"crf": str(crf), "targets_met": True, "metrics": {"vmaf_min": 94.5}})
    return mkv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecoverEncodingAttempts:

    def test_complete_pair_detected(self, tmp_path: Path) -> None:
        """A pair with mkv + yaml in encoded/ is classified COMPLETE."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        winning     = _make_complete_pair(encoded_dir)

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY],
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state        == ArtifactState.COMPLETE
        assert pair.winning_file == winning
        assert recovery.pending  == []

    def test_absent_pair_when_no_encoded_dir(self, tmp_path: Path) -> None:
        """A pair with no encoded/ directory is ABSENT."""
        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY],
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT
        assert (_CHUNK_ID, _STRATEGY) in recovery.pending

    def test_absent_pair_when_mkv_missing_sidecar(self, tmp_path: Path) -> None:
        """A .mkv without a .yaml sidecar is not COMPLETE — pair is ABSENT."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        encoded_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{_CHUNK_ID}.{_RESOLUTION}.crf{_CRF:4.1f}"
        (encoded_dir / f"{stem}.mkv").write_bytes(b"\x00" * 512)
        # no .yaml written

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY],
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.state == ArtifactState.ABSENT

    def test_multiple_chunks_mixed_states(self, tmp_path: Path) -> None:
        """Multiple chunks: some COMPLETE, some ABSENT."""
        chunk_a     = "00꞉00꞉00․000-00꞉01꞉00․000"
        chunk_b     = "00꞉01꞉00․000-00꞉02꞉00․000"
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        _make_complete_pair(encoded_dir, chunk_id=chunk_a)
        # chunk_b has no files

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [chunk_a, chunk_b],
            strategies = [_STRATEGY],
        )

        assert recovery.pairs[(chunk_a, _STRATEGY)].state == ArtifactState.COMPLETE
        assert recovery.pairs[(chunk_b, _STRATEGY)].state == ArtifactState.ABSENT
        assert (chunk_b, _STRATEGY) in recovery.pending
        assert (chunk_a, _STRATEGY) not in recovery.pending

    def test_winning_file_path_is_correct(self, tmp_path: Path) -> None:
        """winning_file points to the actual .mkv, not a placeholder."""
        encoded_dir = tmp_path / "encoded" / _SAFE_STRAT
        winning     = _make_complete_pair(encoded_dir)

        recovery = _recover_encoding_attempts(
            work_dir  = tmp_path,
            chunk_ids = [_CHUNK_ID],
            strategies = [_STRATEGY],
        )

        pair = recovery.pairs[(_CHUNK_ID, _STRATEGY)]
        assert pair.winning_file is not None
        assert pair.winning_file.exists()
        assert pair.winning_file == winning
