"""Unit tests for ProbeState load/save round-trip."""
# CHerSun 2026

import pytest
from pathlib import Path

from pyqenc.models import CropParams
from pyqenc.state import ProbeState


class TestProbeStateRoundTrip:
    """Tests for ProbeState serialisation, persistence, and recovery."""

    # ------------------------------------------------------------------
    # load() — absent file
    # ------------------------------------------------------------------

    def test_load_returns_none_when_file_absent(self, tmp_path: Path):
        """ProbeState.load() must return None when the file does not exist.

        Bug: a missing file could raise an exception instead of returning None,
        breaking the recovery protocol that uses None to mean 'not yet probed'.
        """
        result = ProbeState.load(tmp_path / "probe.yaml")
        assert result is None

    # ------------------------------------------------------------------
    # save() writes valid YAML via .tmp-then-rename
    # ------------------------------------------------------------------

    def test_save_creates_file(self, tmp_path: Path):
        """ProbeState.save() must create the YAML file at the target path.

        Bug: if the atomic write failed silently, the file would be absent
        on the next run and the probe would re-run unnecessarily.
        """
        state = ProbeState(frame_count=1440, crop=None)
        path  = tmp_path / "probe.yaml"
        state.save(path)
        assert path.exists()

    def test_save_no_tmp_file_left_behind(self, tmp_path: Path):
        """After save(), no .tmp file must remain on disk.

        Bug: if atomicity was not implemented, a .tmp file could be left behind
        after a crash, corrupting recovery on the next run.
        """
        state = ProbeState(frame_count=1440, crop=None)
        path  = tmp_path / "probe.yaml"
        state.save(path)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        """ProbeState.save() must create missing parent directories.

        Bug: saving to a non-existent sub-directory would raise FileNotFoundError.
        """
        nested = tmp_path / "job_dir" / "probe.yaml"
        ProbeState(frame_count=100, crop=None).save(nested)
        assert nested.exists()

    # ------------------------------------------------------------------
    # Round-trip: save → load restores all fields
    # ------------------------------------------------------------------

    def test_round_trip_no_crop(self, tmp_path: Path):
        """Save and reload a ProbeState with crop=None; all fields must be preserved.

        Bug: crop=None could be serialised as a dict key that breaks deserialization,
        or frame_count could be lost.
        """
        original = ProbeState(frame_count=1440, crop=None)
        path = tmp_path / "probe.yaml"
        original.save(path)

        restored = ProbeState.load(path)
        assert restored is not None
        assert restored.frame_count == 1440
        assert restored.crop is None

    def test_round_trip_with_crop(self, tmp_path: Path):
        """Save and reload a ProbeState with an active CropParams; all fields must match.

        Bug: crop could be serialised incorrectly, losing top/bottom/left/right values.
        """
        crop     = CropParams(top=140, bottom=140, left=0, right=0)
        original = ProbeState(frame_count=72000, crop=crop)
        path     = tmp_path / "probe.yaml"
        original.save(path)

        restored = ProbeState.load(path)
        assert restored is not None
        assert restored.frame_count  == 72000
        assert restored.crop         is not None
        assert restored.crop.top     == 140
        assert restored.crop.bottom  == 140
        assert restored.crop.left    == 0
        assert restored.crop.right   == 0

    def test_round_trip_all_sides_crop(self, tmp_path: Path):
        """Round-trip with all four crop sides non-zero must preserve every value.

        Bug: left/right values could be silently dropped if serialisation only
        wrote top/bottom.
        """
        crop     = CropParams(top=10, bottom=20, left=30, right=40)
        original = ProbeState(frame_count=500, crop=crop)
        path     = tmp_path / "probe.yaml"
        original.save(path)

        restored = ProbeState.load(path)
        assert restored is not None
        assert restored.crop is not None
        assert restored.crop.top    == 10
        assert restored.crop.bottom == 20
        assert restored.crop.left   == 30
        assert restored.crop.right  == 40

    def test_round_trip_frame_count_zero_sentinel(self, tmp_path: Path):
        """Round-trip with frame_count=0 (unknown sentinel) must not be lost.

        Bug: frame_count=0 could be treated as falsy and omitted from the YAML,
        returning a different default on restore.
        """
        original = ProbeState(frame_count=0, crop=None)
        path     = tmp_path / "probe.yaml"
        original.save(path)

        restored = ProbeState.load(path)
        assert restored is not None
        assert restored.frame_count == 0

    # ------------------------------------------------------------------
    # load() — graceful handling of corrupt/invalid files
    # ------------------------------------------------------------------

    def test_load_returns_none_for_invalid_yaml(self, tmp_path: Path):
        """ProbeState.load() must return None (not raise) for an invalid file.

        Bug: a corrupt probe.yaml could abort the entire pipeline run with an
        unhandled exception instead of triggering a fresh probe.
        """
        path = tmp_path / "probe.yaml"
        path.write_text("not: valid: yaml: [unclosed", encoding="utf-8")

        result = ProbeState.load(path)
        assert result is None
