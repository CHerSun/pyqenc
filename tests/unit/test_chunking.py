"""Unit tests for two-phase chunking (detect_scenes_to_state / split_chunks_from_state)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.models import (
    ChunkingMode,
    ChunkVideoMetadata,
    PhaseMetadata,
    PhaseState,
    PhaseStatus,
    PipelineState,
    SceneBoundary,
    VideoMetadata,
)
from pyqenc.phases.chunking import (
    _chunk_name,
    detect_scenes_to_state,
    split_chunks_from_state,
)
from pyqenc.progress import ProgressTracker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tracker(tmp_path: Path) -> ProgressTracker:
    """Create a ProgressTracker with a minimal initialized state."""
    tracker = ProgressTracker(tmp_path, batch_updates=False)
    state = PipelineState(
        version="1.0",
        source_video=VideoMetadata(path=tmp_path / "source.mkv"),
        current_phase="",
        phases={},
        chunks_metadata={},
        chunks={},
    )
    tracker.save_state(state)
    return tracker


# ---------------------------------------------------------------------------
# _chunk_name -- frame-range naming
# ---------------------------------------------------------------------------

class TestChunkName:
    def test_six_digit_padding(self):
        """Frame numbers are zero-padded to 6 digits."""
        assert _chunk_name(0, 319) == "chunk.000000-000319"

    def test_large_frame_numbers(self):
        """Large frame numbers are formatted correctly."""
        assert _chunk_name(100000, 199999) == "chunk.100000-199999"

    def test_inclusive_range(self):
        """End frame is inclusive (start + count - 1)."""
        # 320 frames: 0..319
        name = _chunk_name(0, 319)
        start, end = name.split(".")[1].split("-")
        assert int(end) - int(start) + 1 == 320


# ---------------------------------------------------------------------------
# detect_scenes_to_state -- zero-scene fallback
# ---------------------------------------------------------------------------

class TestDetectScenesToState:
    def test_zero_scenes_produces_single_boundary(self, tmp_path):
        """When the detector returns no scenes, a single boundary at frame 0 is stored."""
        tracker = _make_tracker(tmp_path)

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        # Patch scenedetect.detect to return an empty list
        with patch("pyqenc.phases.chunking.detect", return_value=[]):
            boundaries = detect_scenes_to_state(video_meta, tracker)

        assert len(boundaries) == 1
        assert boundaries[0].frame == 0
        assert boundaries[0].timestamp_seconds == 0.0

    def test_zero_scenes_persisted_in_state(self, tmp_path):
        """The single fallback boundary is persisted into pipeline state."""
        tracker = _make_tracker(tmp_path)
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        with patch("pyqenc.phases.chunking.detect", return_value=[]):
            detect_scenes_to_state(video_meta, tracker)

        state = tracker.load_state()
        assert state is not None
        chunking = state.phases.get("chunking")
        assert chunking is not None
        assert chunking.metadata is not None
        assert len(chunking.metadata.scene_boundaries) == 1

    def test_multiple_scenes_persisted(self, tmp_path):
        """Detected scene boundaries are all persisted."""
        tracker = _make_tracker(tmp_path)
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        # Build fake scene list: two scenes
        def _make_timecode(frame: int, seconds: float) -> MagicMock:
            tc = MagicMock()
            tc.get_frames.return_value = frame
            tc.get_seconds.return_value = seconds
            return tc

        fake_scenes = [
            (_make_timecode(0, 0.0),   _make_timecode(320, 13.33)),
            (_make_timecode(320, 13.33), _make_timecode(640, 26.67)),
        ]

        with patch("pyqenc.phases.chunking.detect", return_value=fake_scenes):
            boundaries = detect_scenes_to_state(video_meta, tracker)

        assert len(boundaries) == 2
        assert boundaries[0].frame == 0
        assert boundaries[1].frame == 320

        state = tracker.load_state()
        assert state is not None
        assert state.phases.get("chunking") is not None
        assert state.phases["chunking"].metadata is not None
        assert len(state.phases["chunking"].metadata.scene_boundaries) == 2

    def test_zero_scenes_logs_warning(self, tmp_path, caplog):
        """A warning is logged when zero scenes are detected."""
        import logging

        tracker = _make_tracker(tmp_path)
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")

        with patch("pyqenc.phases.chunking.detect", return_value=[]):
            with caplog.at_level(logging.WARNING, logger="pyqenc.phases.chunking"):
                detect_scenes_to_state(video_meta, tracker)

        assert any("0 scenes" in r.message or "one chunk" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# split_chunks_from_state -- resumption and skipping
# ---------------------------------------------------------------------------

class TestSplitChunksFromState:
    def _state_with_boundaries(
        self,
        tmp_path: Path,
        boundaries: list[SceneBoundary],
        existing_chunks: dict[str, ChunkVideoMetadata] | None = None,
    ) -> ProgressTracker:
        """Build a tracker whose state already has scene boundaries."""
        tracker = ProgressTracker(tmp_path, batch_updates=False)
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=tmp_path / "source.mkv"),
            current_phase="chunking",
            phases={
                "chunking": PhaseState(
                    status=PhaseStatus.IN_PROGRESS,
                    metadata=PhaseMetadata(scene_boundaries=boundaries),
                )
            },
            chunks_metadata=existing_chunks or {},
            chunks={},
        )
        tracker.save_state(state)
        return tracker

    def test_already_recorded_chunks_are_skipped(self, tmp_path):
        """Chunks already in chunks_metadata are not re-split."""
        boundaries = [
            SceneBoundary(frame=0,   timestamp_seconds=0.0),
            SceneBoundary(frame=320, timestamp_seconds=13.33),
        ]
        existing_chunk = ChunkVideoMetadata(
            path=tmp_path / "chunks" / "chunk.000000-000319.mkv",
            chunk_id="chunk.000000-000319",
            start_frame=0,
        )
        existing_chunk._frame_count = 320

        tracker = self._state_with_boundaries(
            tmp_path, boundaries,
            existing_chunks={"chunk.000000-000319": existing_chunk},
        )

        # Pre-fill video_meta so no probe is triggered
        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 26.67
        video_meta._frame_count      = 640

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        # The second chunk (320-639) would need ffmpeg; patch it to succeed
        second_chunk_file = output_dir / "chunk.000320-000639.mkv"
        second_chunk_file.write_bytes(b"\x00" * 100)  # non-empty fake file

        with patch("pyqenc.phases.chunking.subprocess.run") as mock_run, \
             patch("pyqenc.phases.chunking.get_frame_count", return_value=320):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = split_chunks_from_state(
                video_meta, output_dir, tracker, chunking_mode=ChunkingMode.REMUX
            )

        # First chunk was skipped (already recorded); second was processed
        assert len(result) == 2
        assert result[0].chunk_id == "chunk.000000-000319"
        # ffmpeg should NOT have been called for the already-recorded chunk
        # (it may have been called for the second chunk)
        for call in mock_run.call_args_list:
            args = call.args[0] if call.args else call.kwargs.get("args", [])
            assert "chunk.000000-000319.mkv" not in " ".join(str(a) for a in args)

    def test_no_boundaries_raises(self, tmp_path):
        """RuntimeError is raised when no boundaries are in state."""
        tracker = ProgressTracker(tmp_path, batch_updates=False)
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=tmp_path / "source.mkv"),
            current_phase="chunking",
            phases={},
            chunks_metadata={},
            chunks={},
        )
        tracker.save_state(state)

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        with pytest.raises(RuntimeError, match="No scene boundaries"):
            split_chunks_from_state(video_meta, tmp_path / "chunks", tracker)

    def test_missing_chunk_file_is_skipped(self, tmp_path):
        """A chunk whose file is missing after ffmpeg is not added to results."""
        boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
        tracker = self._state_with_boundaries(tmp_path, boundaries)

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 13.33
        video_meta._frame_count      = 320

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        # ffmpeg "succeeds" but leaves no file
        with patch("pyqenc.phases.chunking.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = split_chunks_from_state(
                video_meta, output_dir, tracker, chunking_mode=ChunkingMode.REMUX
            )

        assert result == []


# ---------------------------------------------------------------------------
# FFV1 command construction
# ---------------------------------------------------------------------------

class TestFFV1CommandConstruction:
    """Verify the ffmpeg command built by split_chunks_from_state for each mode."""

    def _make_tracker_with_boundaries(
        self,
        tmp_path: Path,
        boundaries: list[SceneBoundary],
    ) -> ProgressTracker:
        tracker = ProgressTracker(tmp_path, batch_updates=False)
        state = PipelineState(
            version="1.0",
            source_video=VideoMetadata(path=tmp_path / "source.mkv"),
            current_phase="chunking",
            phases={
                "chunking": PhaseState(
                    status=PhaseStatus.IN_PROGRESS,
                    metadata=PhaseMetadata(scene_boundaries=boundaries),
                )
            },
            chunks_metadata={},
            chunks={},
        )
        tracker.save_state(state)
        return tracker

    def _run_split(
        self,
        tmp_path: Path,
        chunking_mode: ChunkingMode,
        pix_fmt: str = "yuv420p10le",
    ) -> list[list[str]]:
        """Run split_chunks_from_state with mocked subprocess and return captured commands."""
        boundaries = [
            SceneBoundary(frame=0,   timestamp_seconds=0.0),
            SceneBoundary(frame=320, timestamp_seconds=13.33),
        ]
        tracker = self._make_tracker_with_boundaries(tmp_path, boundaries)

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 26.67
        video_meta._frame_count      = 640
        video_meta._pix_fmt          = pix_fmt  # pre-populate so no probe is triggered

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        captured_cmds: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kwargs):  # type: ignore[override]
            captured_cmds.append(list(cmd))
            # Create the output file so the post-split existence check passes
            out_file = Path(cmd[-1])
            out_file.write_bytes(b"\x00" * 100)
            return MagicMock(returncode=0, stderr="")

        with patch("pyqenc.phases.chunking.subprocess.run", side_effect=_fake_run), \
             patch("pyqenc.phases.chunking.get_frame_count", return_value=320):
            split_chunks_from_state(
                video_meta, output_dir, tracker, chunking_mode=chunking_mode
            )

        return captured_cmds

    # ------------------------------------------------------------------
    # Lossless mode assertions
    # ------------------------------------------------------------------

    def test_lossless_uses_ffv1_codec(self, tmp_path):
        """LOSSLESS mode must include -c:v ffv1 in the ffmpeg command."""
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        assert cmds, "Expected at least one ffmpeg call"
        for cmd in cmds:
            cmd_str = " ".join(cmd)
            assert "-c:v ffv1" in cmd_str, f"Missing -c:v ffv1 in: {cmd_str}"

    def test_lossless_includes_g1(self, tmp_path):
        """LOSSLESS mode must include -g 1 (all-intra)."""
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        for cmd in cmds:
            assert "-g" in cmd and "1" in cmd, f"Missing -g 1 in: {cmd}"
            g_idx = cmd.index("-g")
            assert cmd[g_idx + 1] == "1", f"Expected -g 1, got -g {cmd[g_idx + 1]}"

    def test_lossless_includes_level3(self, tmp_path):
        """LOSSLESS mode must include -level 3."""
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        for cmd in cmds:
            assert "-level" in cmd, f"Missing -level in: {cmd}"
            lvl_idx = cmd.index("-level")
            assert cmd[lvl_idx + 1] == "3", f"Expected -level 3, got -level {cmd[lvl_idx + 1]}"

    def test_lossless_passes_pix_fmt(self, tmp_path):
        """LOSSLESS mode must pass the source pixel format via -pix_fmt."""
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS, pix_fmt="yuv420p10le")
        for cmd in cmds:
            assert "-pix_fmt" in cmd, f"Missing -pix_fmt in: {cmd}"
            pf_idx = cmd.index("-pix_fmt")
            assert cmd[pf_idx + 1] == "yuv420p10le", (
                f"Expected -pix_fmt yuv420p10le, got {cmd[pf_idx + 1]}"
            )

    def test_lossless_no_stream_copy(self, tmp_path):
        """LOSSLESS mode must NOT contain -c copy."""
        cmds = self._run_split(tmp_path, ChunkingMode.LOSSLESS)
        for cmd in cmds:
            cmd_str = " ".join(cmd)
            assert "-c copy" not in cmd_str, f"Unexpected -c copy in lossless cmd: {cmd_str}"

    # ------------------------------------------------------------------
    # Remux mode assertions
    # ------------------------------------------------------------------

    def test_remux_uses_stream_copy(self, tmp_path):
        """REMUX mode must include -c copy."""
        cmds = self._run_split(tmp_path, ChunkingMode.REMUX)
        assert cmds, "Expected at least one ffmpeg call"
        for cmd in cmds:
            cmd_str = " ".join(cmd)
            assert "-c copy" in cmd_str, f"Missing -c copy in remux cmd: {cmd_str}"

    def test_remux_no_ffv1_flags(self, tmp_path):
        """REMUX mode must NOT contain any FFV1-specific flags."""
        cmds = self._run_split(tmp_path, ChunkingMode.REMUX)
        ffv1_flags = {"-c:v", "ffv1", "-g", "-level", "-coder", "-context", "-slices"}
        for cmd in cmds:
            for flag in ffv1_flags:
                assert flag not in cmd, f"Unexpected FFV1 flag '{flag}' in remux cmd: {cmd}"

    # ------------------------------------------------------------------
    # Pixel format fallback
    # ------------------------------------------------------------------

    def test_lossless_falls_back_to_yuv420p_when_pix_fmt_unknown(self, tmp_path):
        """When pix_fmt cannot be probed, LOSSLESS mode falls back to yuv420p."""
        boundaries = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
        tracker = self._make_tracker_with_boundaries(tmp_path, boundaries)

        video_meta = VideoMetadata(path=tmp_path / "source.mkv")
        video_meta._duration_seconds = 13.33
        video_meta._frame_count      = 320
        # _pix_fmt intentionally left as None

        output_dir = tmp_path / "chunks"
        output_dir.mkdir()

        captured_cmds: list[list[str]] = []

        def _fake_run(cmd: list[str], **_kwargs):  # type: ignore[override]
            captured_cmds.append(list(cmd))
            Path(cmd[-1]).write_bytes(b"\x00" * 100)
            return MagicMock(returncode=0, stderr="")

        # Patch _probe_metadata so it doesn't actually call ffprobe
        with patch.object(VideoMetadata, "_probe_metadata"), \
             patch("pyqenc.phases.chunking.subprocess.run", side_effect=_fake_run), \
             patch("pyqenc.phases.chunking.get_frame_count", return_value=320):
            split_chunks_from_state(
                video_meta, output_dir, tracker, chunking_mode=ChunkingMode.LOSSLESS
            )

        assert captured_cmds, "Expected at least one ffmpeg call"
        cmd = captured_cmds[0]
        assert "-pix_fmt" in cmd
        pf_idx = cmd.index("-pix_fmt")
        assert cmd[pf_idx + 1] == "yuv420p", (
            f"Expected fallback to yuv420p, got {cmd[pf_idx + 1]}"
        )
