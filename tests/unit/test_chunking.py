"""Unit tests for two-phase chunking (detect_scenes / split_chunks)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.models import (
    ChunkMetadata,
    ChunkingMode,
    SceneBoundary,
    VideoMetadata,
)
from pyqenc.phases.chunking import (
    _chunk_name_duration,
    detect_scenes,
    split_chunks,
)
from pyqenc.phases.recovery import ChunkingRecovery
from pyqenc.state import (
    ArtifactState,
    ChunkingParams,
    JobState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunking_yaml(tmp_path: Path) -> Path:
    """Return the path to chunking.yaml in tmp_path."""
    return tmp_path / "chunking.yaml"


def _make_job(tmp_path: Path) -> JobState:
    """Create a minimal JobState for testing."""
    return JobState(source=VideoMetadata(path=tmp_path / "source.mkv"))


# ---------------------------------------------------------------------------
# _chunk_name_duration -- timestamp-range naming
# ---------------------------------------------------------------------------

class TestChunkNameDuration:
    def test_matches_chunk_name_pattern(self):
        """Output of _chunk_name_duration must match CHUNK_NAME_PATTERN."""
        from pyqenc.constants import CHUNK_NAME_PATTERN
        name = _chunk_name_duration(0.0, 13.33)
        assert CHUNK_NAME_PATTERN.match(name), f"Pattern mismatch: {name!r}"

    def test_zero_start(self):
        """Zero start timestamp produces correct zero-padded hours/minutes."""
        name = _chunk_name_duration(0.0, 13.33)
        assert name.startswith("00꞉00꞉"), f"Unexpected start: {name!r}"

    def test_range_separator_present(self):
        """The range separator '-' separates start and end timestamps."""
        name = _chunk_name_duration(0.0, 13.33)
        assert "-" in name, f"Missing range separator in: {name!r}"


# ---------------------------------------------------------------------------
# detect_scenes -- zero-scene fallback
# ---------------------------------------------------------------------------

class TestDetectScenes:
    def test_zero_scenes_produces_single_boundary(self, tmp_path):
        """When the detector returns no scenes, a single boundary at frame 0 is stored."""
        yaml_path  = _chunking_yaml(tmp_path)
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        with patch("pyqenc.phases.chunking.detect", return_value=[]):
            boundaries = detect_scenes(video_meta, yaml_path)

        assert len(boundaries) == 1
        assert boundaries[0].frame == 0
        assert boundaries[0].timestamp_seconds == 0.0

    def test_zero_scenes_persisted_in_chunking_yaml(self, tmp_path):
        """The single fallback boundary is persisted into chunking.yaml."""
        yaml_path  = _chunking_yaml(tmp_path)
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        with patch("pyqenc.phases.chunking.detect", return_value=[]):
            detect_scenes(video_meta, yaml_path)

        params = ChunkingParams.load(yaml_path)
        assert params is not None
        assert len(params.scenes) == 1

    def test_multiple_scenes_persisted(self, tmp_path):
        """Detected scene boundaries are all persisted."""
        yaml_path  = _chunking_yaml(tmp_path)
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        def _make_timecode(frame: int, seconds: float) -> MagicMock:
            tc = MagicMock()
            tc.get_frames.return_value = frame
            tc.get_seconds.return_value = seconds
            return tc

        fake_scenes = [
            (_make_timecode(0, 0.0),    _make_timecode(320, 13.33)),
            (_make_timecode(320, 13.33), _make_timecode(640, 26.67)),
        ]

        with patch("pyqenc.phases.chunking.detect", return_value=fake_scenes):
            boundaries = detect_scenes(video_meta, yaml_path)

        assert len(boundaries) == 2
        assert boundaries[0].frame == 0
        assert boundaries[1].frame == 320

        params = ChunkingParams.load(yaml_path)
        assert params is not None
        assert len(params.scenes) == 2

    def test_zero_scenes_logs_warning(self, tmp_path, caplog):
        """A warning is logged when zero scenes are detected."""
        import logging

        yaml_path  = _chunking_yaml(tmp_path)
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        with patch("pyqenc.phases.chunking.detect", return_value=[]):
            with caplog.at_level(logging.WARNING, logger="pyqenc.phases.chunking"):
                detect_scenes(video_meta, yaml_path)

        assert any("0 scenes" in r.message or "one chunk" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# split_chunks -- resumption and skipping
# ---------------------------------------------------------------------------

class TestSplitChunks:
    def _make_recovery_with_complete_chunk(
        self,
        tmp_path: Path,
        chunk_id: str,
        chunk_meta: ChunkMetadata,
    ) -> ChunkingRecovery:
        """Build a ChunkingRecovery with one COMPLETE chunk."""
        from pyqenc.phases.recovery import ChunkRecovery
        return ChunkingRecovery(
            scenes=[],
            chunks={
                chunk_id: ChunkRecovery(
                    chunk_id=chunk_id,
                    path=tmp_path / "chunks" / f"{chunk_id}.mkv",
                    state=ArtifactState.COMPLETE,
                    metadata=chunk_meta,
                )
            },
            pending=[],
        )

    def test_already_complete_chunks_are_skipped(self, tmp_path):
        """Chunks already COMPLETE in recovery are not re-split."""
        boundaries = [
            SceneBoundary(frame=0,   timestamp_seconds=0.0),
            SceneBoundary(frame=320, timestamp_seconds=13.33),
        ]
        chunk_id = "00꞉00꞉00․000-00꞉00꞉13․330"
        existing_chunk = ChunkMetadata(
            path=tmp_path / "chunks" / f"{chunk_id}.mkv",
            chunk_id=chunk_id,
            start_timestamp=0.0,
            end_timestamp=13.33,
        )
        existing_chunk._frame_count = 320

        recovery = self._make_recovery_with_complete_chunk(tmp_path, chunk_id, existing_chunk)

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 26.67
        video_meta._frame_count      = 640

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        # The second chunk would need ffmpeg; create a fake output file
        second_chunk_id = "00꞉00꞉13․330-00꞉00꞉26․670"
        second_chunk_file = output_dir / f"{second_chunk_id}.mkv"

        def _fake_run_ffmpeg(cmd, output_file=None, **kwargs):
            # Simulate ffmpeg writing the output file
            if output_file is not None:
                output_file.write_bytes(b"\x00" * 100)
            result = MagicMock()
            result.success = True
            result.returncode = 0
            result.frame_count = 320
            result.stderr_lines = []
            return result

        with patch("pyqenc.phases.chunking.run_ffmpeg", side_effect=_fake_run_ffmpeg):
            result = split_chunks(
                video_meta, output_dir, boundaries, recovery,
                chunking_mode=ChunkingMode.REMUX,
            )

        assert len(result) == 2
        assert result[0].chunk_id == chunk_id

    def test_no_boundaries_raises(self, tmp_path):
        """RuntimeError is raised when no boundaries are provided."""
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        recovery = ChunkingRecovery()

        with pytest.raises(RuntimeError, match="No scene boundaries"):
            split_chunks(video_meta, tmp_path / "chunks", [], recovery)

    def test_missing_chunk_file_is_skipped(self, tmp_path):
        """A chunk whose file is missing after ffmpeg is not added to results."""
        boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
        recovery = ChunkingRecovery()

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 13.33
        video_meta._frame_count      = 320

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        # ffmpeg "succeeds" but leaves no file (output_file not written)
        def _fake_run_ffmpeg(cmd, output_file=None, **kwargs):
            result = MagicMock()
            result.success = False  # runner returns failure when file is missing
            result.returncode = 1
            result.frame_count = None
            result.stderr_lines = []
            return result

        with patch("pyqenc.phases.chunking.run_ffmpeg", side_effect=_fake_run_ffmpeg):
            result = split_chunks(
                video_meta, output_dir, boundaries, recovery,
                chunking_mode=ChunkingMode.REMUX,
            )

        assert result == []


# ---------------------------------------------------------------------------
# FFV1 command construction
# ---------------------------------------------------------------------------

class TestFFV1CommandConstruction:
    """Verify the ffmpeg command built by split_chunks for each mode."""

    def _run_split(
        self,
        tmp_path: Path,
        chunking_mode: ChunkingMode,
        pix_fmt: str = "yuv420p10le",
    ) -> list[list[str]]:
        """Run split_chunks with mocked run_ffmpeg and return captured commands."""
        boundaries = [
            SceneBoundary(frame=0,   timestamp_seconds=0.0),
            SceneBoundary(frame=320, timestamp_seconds=13.33),
        ]
        recovery = ChunkingRecovery()

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 26.67
        video_meta._frame_count      = 640
        video_meta._pix_fmt          = pix_fmt

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        captured_cmds: list[list[str]] = []

        def _fake_run_ffmpeg(cmd, output_file=None, **kwargs):
            captured_cmds.append([str(a) for a in cmd])
            if output_file is not None:
                output_file.write_bytes(b"\x00" * 100)
            result = MagicMock()
            result.success = True
            result.returncode = 0
            result.frame_count = 320
            result.stderr_lines = []
            return result

        with patch("pyqenc.phases.chunking.run_ffmpeg", side_effect=_fake_run_ffmpeg):
            split_chunks(video_meta, output_dir, boundaries, recovery, chunking_mode=chunking_mode)

        return captured_cmds

    def test_lossless_uses_ffv1_codec(self, tmp_path):
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        assert cmds, "Expected at least one ffmpeg call"
        for cmd in cmds:
            assert "-c:v" in cmd and "ffv1" in cmd

    def test_lossless_includes_g1(self, tmp_path):
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        for cmd in cmds:
            assert "-g" in cmd
            g_idx = cmd.index("-g")
            assert cmd[g_idx + 1] == "1"

    def test_lossless_includes_level3(self, tmp_path):
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        for cmd in cmds:
            assert "-level" in cmd
            lvl_idx = cmd.index("-level")
            assert cmd[lvl_idx + 1] == "3"

    def test_lossless_passes_pix_fmt(self, tmp_path):
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS, pix_fmt="yuv420p10le")
        for cmd in cmds:
            assert "-pix_fmt" in cmd
            pf_idx = cmd.index("-pix_fmt")
            assert cmd[pf_idx + 1] == "yuv420p10le"

    def test_lossless_no_stream_copy(self, tmp_path):
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        for cmd in cmds:
            cmd_str = " ".join(cmd)
            assert "-c copy" not in cmd_str

    def test_remux_uses_stream_copy(self, tmp_path):
        cmds = self._run_split(tmp_path, ChunkingMode.REMUX)
        assert cmds, "Expected at least one ffmpeg call"
        for cmd in cmds:
            cmd_str = " ".join(cmd)
            assert "-c copy" in cmd_str

    def test_remux_no_ffv1_flags(self, tmp_path):
        cmds = self._run_split(tmp_path, ChunkingMode.REMUX)
        ffv1_flags = {"-c:v", "ffv1", "-level", "-coder", "-context", "-slices"}
        for cmd in cmds:
            for flag in ffv1_flags:
                assert flag not in cmd, f"Unexpected FFV1 flag '{flag}' in remux cmd: {cmd}"

    def test_lossless_falls_back_to_yuv420p_when_pix_fmt_unknown(self, tmp_path):
        """When pix_fmt cannot be probed, LOSSLESS mode falls back to yuv420p."""
        boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
        recovery = ChunkingRecovery()

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 13.33
        video_meta._frame_count      = 320
        # _pix_fmt intentionally left as None

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        captured_cmds: list[list[str]] = []

        def _fake_run_ffmpeg(cmd, output_file=None, **kwargs):
            captured_cmds.append([str(a) for a in cmd])
            if output_file is not None:
                output_file.write_bytes(b"\x00" * 100)
            result = MagicMock()
            result.success = True
            result.returncode = 0
            result.frame_count = 320
            result.stderr_lines = []
            return result

        with patch.object(VideoMetadata, "_probe_metadata"), \
             patch("pyqenc.phases.chunking.run_ffmpeg", side_effect=_fake_run_ffmpeg):
            split_chunks(
                video_meta, output_dir, boundaries, recovery,
                chunking_mode=ChunkingMode.LOSSLESS,
            )

        assert captured_cmds, "Expected at least one ffmpeg call"
        cmd = captured_cmds[0]
        assert "-pix_fmt" in cmd
        pf_idx = cmd.index("-pix_fmt")
        assert cmd[pf_idx + 1] == "yuv420p"
