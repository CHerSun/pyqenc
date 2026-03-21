"""Unit tests for EncodingPhase quality target re-evaluation.

Covers requirement 5.7:
- COMPLETE pairs whose metrics no longer meet updated quality targets are
  downgraded to ARTIFACT_ONLY by ``_recover_pair`` (called from
  ``EncodingPhase._recover()``).
- COMPLETE pairs that still meet updated targets remain COMPLETE.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pyqenc.models import QualityTarget
from pyqenc.phases.encoding import _enc_recover_pair as _recover_pair
from pyqenc.state import ArtifactState, EncodingResultSidecar
from pyqenc.utils.yaml_utils import write_yaml_atomic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHUNK_ID  = "00꞉00꞉00․000-00꞉01꞉30․000"
_STRATEGY  = "slow+h265-aq"
_SAFE_STRAT = "slow_h265-aq"
_RESOLUTION = "1920x800"
_CRF        = 18.0

_TARGET_VMAF_MIN_93 = QualityTarget(metric="vmaf", statistic="min", value=93.0)
_TARGET_VMAF_MIN_96 = QualityTarget(metric="vmaf", statistic="min", value=96.0)


def _make_attempt_file(encoding_dir: Path, crf: float = _CRF) -> Path:
    """Create a fake attempt .mkv file in encoding/<strategy>/."""
    encoding_dir.mkdir(parents=True, exist_ok=True)
    attempt_name = f"{_CHUNK_ID}.{_RESOLUTION}.crf{crf:4.1f}.mkv"
    attempt_path = encoding_dir / attempt_name
    attempt_path.write_bytes(b"\x00" * 1024)
    return attempt_path


def _write_attempt_sidecar(attempt_path: Path, metrics: dict[str, float], crf: float = _CRF) -> None:
    """Write a per-attempt metrics sidecar alongside an attempt file."""
    sidecar = attempt_path.with_suffix(".yaml")
    data = {"crf": crf, "targets_met": True, "metrics": metrics}
    write_yaml_atomic(sidecar, data)


def _make_result_sidecar(encoded_dir: Path, winning_attempt: str, metrics: dict[str, float]) -> Path:
    """Create a result sidecar in encoded/<strategy>/ marking the pair COMPLETE."""
    encoded_dir.mkdir(parents=True, exist_ok=True)
    # Create the winning attempt file (hard-link target)
    winning_path = encoded_dir / winning_attempt
    winning_path.write_bytes(b"\x00" * 1024)

    sidecar_path = encoded_dir / f"{_CHUNK_ID}.{_RESOLUTION}.yaml"
    data = EncodingResultSidecar(
        winning_attempt = winning_attempt,
        crf             = _CRF,
        quality_targets = ["vmaf-min:93.0"],
        metrics         = metrics,
    ).to_yaml_dict()
    write_yaml_atomic(sidecar_path, data)
    return sidecar_path


# ---------------------------------------------------------------------------
# Tests: COMPLETE → ARTIFACT_ONLY downgrade when targets tighten
# ---------------------------------------------------------------------------

class TestQualityTargetReEvaluation:
    """Tests for COMPLETE → ARTIFACT_ONLY downgrade when quality targets change."""

    def test_complete_pair_stays_complete_when_targets_still_met(self, tmp_path: Path) -> None:
        """A COMPLETE pair remains COMPLETE when its metrics still satisfy the targets."""
        encoding_dir = tmp_path / "encoding" / _SAFE_STRAT
        encoded_dir  = tmp_path / "encoded"  / _SAFE_STRAT

        attempt_file = _make_attempt_file(encoding_dir)
        _write_attempt_sidecar(attempt_file, {"vmaf_min": 94.5})
        winning_name = attempt_file.name
        _make_result_sidecar(encoded_dir, winning_name, {"vmaf_min": 94.5})

        # Target: vmaf_min >= 93.0 — metrics (94.5) still pass
        recovery = _recover_pair(
            chunk_id        = _CHUNK_ID,
            strategy        = _STRATEGY,
            encoding_dir    = encoding_dir,
            encoded_dir     = encoded_dir,
            quality_targets = [_TARGET_VMAF_MIN_93],
        )

        assert recovery.state == ArtifactState.COMPLETE

    def test_complete_pair_downgraded_when_targets_tightened(self, tmp_path: Path) -> None:
        """A COMPLETE pair is downgraded to ARTIFACT_ONLY when tightened targets are not met."""
        encoding_dir = tmp_path / "encoding" / _SAFE_STRAT
        encoded_dir  = tmp_path / "encoded"  / _SAFE_STRAT

        attempt_file = _make_attempt_file(encoding_dir)
        _write_attempt_sidecar(attempt_file, {"vmaf_min": 94.5})
        winning_name = attempt_file.name
        # Result sidecar stores vmaf_min=94.5 (met old target of 93.0)
        _make_result_sidecar(encoded_dir, winning_name, {"vmaf_min": 94.5})

        # New target: vmaf_min >= 96.0 — metrics (94.5) no longer pass
        recovery = _recover_pair(
            chunk_id        = _CHUNK_ID,
            strategy        = _STRATEGY,
            encoding_dir    = encoding_dir,
            encoded_dir     = encoded_dir,
            quality_targets = [_TARGET_VMAF_MIN_96],
        )

        assert recovery.state == ArtifactState.ARTIFACT_ONLY

    def test_downgraded_pair_has_crf_history_from_attempts(self, tmp_path: Path) -> None:
        """After downgrade, the CRF history from attempt files is preserved for resumption."""
        encoding_dir = tmp_path / "encoding" / _SAFE_STRAT
        encoded_dir  = tmp_path / "encoded"  / _SAFE_STRAT

        attempt_file = _make_attempt_file(encoding_dir, crf=18.0)
        _write_attempt_sidecar(attempt_file, {"vmaf_min": 94.5}, crf=18.0)
        winning_name = attempt_file.name
        _make_result_sidecar(encoded_dir, winning_name, {"vmaf_min": 94.5})

        recovery = _recover_pair(
            chunk_id        = _CHUNK_ID,
            strategy        = _STRATEGY,
            encoding_dir    = encoding_dir,
            encoded_dir     = encoded_dir,
            quality_targets = [_TARGET_VMAF_MIN_96],
        )

        assert recovery.state == ArtifactState.ARTIFACT_ONLY
        # CRF history should contain the existing attempt
        assert len(recovery.history.attempts) >= 1

    def test_absent_pair_stays_absent(self, tmp_path: Path) -> None:
        """A pair with no files at all is classified ABSENT."""
        encoding_dir = tmp_path / "encoding" / _SAFE_STRAT
        encoded_dir  = tmp_path / "encoded"  / _SAFE_STRAT

        recovery = _recover_pair(
            chunk_id        = _CHUNK_ID,
            strategy        = _STRATEGY,
            encoding_dir    = encoding_dir,
            encoded_dir     = encoded_dir,
            quality_targets = [_TARGET_VMAF_MIN_93],
        )

        assert recovery.state == ArtifactState.ABSENT

    def test_artifact_only_pair_stays_artifact_only(self, tmp_path: Path) -> None:
        """A pair with attempt files but no result sidecar is ARTIFACT_ONLY."""
        encoding_dir = tmp_path / "encoding" / _SAFE_STRAT
        encoded_dir  = tmp_path / "encoded"  / _SAFE_STRAT

        attempt_file = _make_attempt_file(encoding_dir)
        _write_attempt_sidecar(attempt_file, {"vmaf_min": 94.5})
        # No result sidecar in encoded/

        recovery = _recover_pair(
            chunk_id        = _CHUNK_ID,
            strategy        = _STRATEGY,
            encoding_dir    = encoding_dir,
            encoded_dir     = encoded_dir,
            quality_targets = [_TARGET_VMAF_MIN_93],
        )

        assert recovery.state == ArtifactState.ARTIFACT_ONLY

    def test_result_sidecar_with_missing_winning_file_treated_as_absent(
        self, tmp_path: Path
    ) -> None:
        """If the result sidecar references a missing winning file and no attempt files
        exist, the pair is ABSENT (sidecar is deleted, no attempts to resume from)."""
        encoding_dir = tmp_path / "encoding" / _SAFE_STRAT
        encoded_dir  = tmp_path / "encoded"  / _SAFE_STRAT
        encoded_dir.mkdir(parents=True, exist_ok=True)

        # Write result sidecar referencing a non-existent winning file
        sidecar_path = encoded_dir / f"{_CHUNK_ID}.{_RESOLUTION}.yaml"
        data = EncodingResultSidecar(
            winning_attempt = f"{_CHUNK_ID}.{_RESOLUTION}.crf{_CRF:4.1f}.mkv",
            crf             = _CRF,
            quality_targets = ["vmaf-min:93.0"],
            metrics         = {"vmaf_min": 94.5},
        ).to_yaml_dict()
        write_yaml_atomic(sidecar_path, data)
        # winning file does NOT exist, no attempt files either

        recovery = _recover_pair(
            chunk_id        = _CHUNK_ID,
            strategy        = _STRATEGY,
            encoding_dir    = encoding_dir,
            encoded_dir     = encoded_dir,
            quality_targets = [_TARGET_VMAF_MIN_93],
        )

        # Sidecar is deleted and no attempt files → ABSENT
        assert recovery.state == ArtifactState.ABSENT
        assert not sidecar_path.exists()
