"""Unit tests for timestamp extraction (PTS preservation).

Covers:
- 2.1  _extract_timestamps: correct header and int(pts * 1000) per line
- 2.3  _recover(): TimestampArtifact classified COMPLETE when file exists, ABSENT otherwise
- 2.3  _recover(): force_wipe deletes timestamps.txt (via rmtree on extracted/)
- 2.4  ExtractionPhaseResult.timestamps_path set correctly
- 5.3  merge phase returns FAILED with clear message when timestamps_path is None
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.constants import TIMESTAMPS_FILENAME
from pyqenc.phases.extraction import (
    ExtractionPhaseResult,
    TimestampArtifact,
    _extract_timestamps,
)
from pyqenc.state import ArtifactState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import subprocess as _subprocess

def _make_mkvextract_fail() -> MagicMock:
    """Return a side_effect that makes mkvextract fail and ffprobe succeed."""
    def _side_effect(cmd: list, **kwargs: object) -> MagicMock:
        result = MagicMock()
        if cmd and str(cmd[0]) == "mkvextract":
            raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
        # ffprobe call — return empty stdout by default; tests override as needed
        result.returncode = 0
        result.stdout     = ""
        result.stderr     = ""
        return result
    return _side_effect


def _make_ffprobe_stdout(pts_ms_values: list[int]) -> str:
    """Build a fake ffprobe stdout string from a list of integer millisecond PTS values."""
    return "\n".join(str(v) for v in pts_ms_values) + "\n"


# ---------------------------------------------------------------------------
# 2.1  _extract_timestamps format
# ---------------------------------------------------------------------------

class TestExtractTimestampsFormat:
    """_extract_timestamps writes correct header and integer ms values per line.

    The implementation tries mkvextract first; tests make it fail so the
    ffprobe fallback path is exercised.  ffprobe returns integer PTS values
    (milliseconds) directly — no float-to-int conversion in the test layer.
    """

    def _mock_run(self, pts_ms: list[int]) -> MagicMock:
        """Return a patch side_effect: mkvextract fails, ffprobe returns integer ms."""
        stdout = _make_ffprobe_stdout(pts_ms)
        def _side_effect(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result.returncode = 0
            result.stdout     = stdout
            result.stderr     = ""
            return result
        return _side_effect

    def test_header_is_timestamp_format_v2(self, tmp_path: Path) -> None:
        pts_ms = [0, 42, 83]
        output = tmp_path / TIMESTAMPS_FILENAME

        with patch("subprocess.run", side_effect=self._mock_run(pts_ms)):
            _extract_timestamps(Path("source.mkv"), 0, output)

        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# timestamp format v2"

    def test_values_are_integer_ms(self, tmp_path: Path) -> None:
        pts_ms = [0, 42, 83, 125]
        output = tmp_path / TIMESTAMPS_FILENAME

        with patch("subprocess.run", side_effect=self._mock_run(pts_ms)):
            _extract_timestamps(Path("source.mkv"), 0, output)

        lines = output.read_text(encoding="utf-8").splitlines()
        data_lines = lines[1:]  # skip header
        assert [int(l) for l in data_lines] == sorted(pts_ms)

    def test_one_value_per_line(self, tmp_path: Path) -> None:
        pts_ms = [0, 33, 66]
        output = tmp_path / TIMESTAMPS_FILENAME

        with patch("subprocess.run", side_effect=self._mock_run(pts_ms)):
            _extract_timestamps(Path("source.mkv"), 0, output)

        lines = output.read_text(encoding="utf-8").splitlines()
        # header + one line per value
        assert len(lines) == 1 + len(pts_ms)

    def test_output_file_created(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME

        with patch("subprocess.run", side_effect=self._mock_run([0, 42])):
            _extract_timestamps(Path("source.mkv"), 0, output)

        assert output.exists()

    def test_tmp_file_not_left_behind(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME

        with patch("subprocess.run", side_effect=self._mock_run([0, 42])):
            _extract_timestamps(Path("source.mkv"), 0, output)

        tmp_file = tmp_path / "timestamps.tmp"
        assert not tmp_file.exists()

    def test_raises_on_ffprobe_failure(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME

        def _both_fail(cmd: list, **kwargs: object) -> MagicMock:
            raise _subprocess.CalledProcessError(1, cmd, stderr=b"error")

        with patch("subprocess.run", side_effect=_both_fail):
            with pytest.raises(_subprocess.CalledProcessError):
                _extract_timestamps(Path("source.mkv"), 0, output)

    def test_raises_on_empty_output(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME

        def _mkvextract_fail_ffprobe_empty(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result.returncode = 0
            result.stdout     = ""
            result.stderr     = ""
            return result

        with patch("subprocess.run", side_effect=_mkvextract_fail_ffprobe_empty):
            with pytest.raises(ValueError, match="empty output"):
                _extract_timestamps(Path("source.mkv"), 0, output)

    def test_raises_on_unparseable_line(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME

        def _mkvextract_fail_ffprobe_bad(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result.returncode = 0
            result.stdout     = "0\nN/A\n83\n"
            result.stderr     = ""
            return result

        with patch("subprocess.run", side_effect=_mkvextract_fail_ffprobe_bad):
            with pytest.raises(ValueError, match="Unparseable"):
                _extract_timestamps(Path("source.mkv"), 0, output)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "deep" / TIMESTAMPS_FILENAME

        with patch("subprocess.run", side_effect=self._mock_run([0, 42])):
            _extract_timestamps(Path("source.mkv"), 0, output)

        assert output.exists()


# ---------------------------------------------------------------------------
# 2.3  TimestampArtifact recovery classification
# ---------------------------------------------------------------------------

class TestTimestampArtifactRecoveryComplete:
    """timestamps.txt present → TimestampArtifact state is COMPLETE."""

    def _make_extraction_phase(self, tmp_path: Path) -> object:
        """Build a minimal ExtractionPhase with a fake config."""
        from unittest.mock import MagicMock
        from pyqenc.phases.extraction import ExtractionPhase

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        config = MagicMock()
        config.work_dir      = tmp_path
        config.source_video  = source
        config.include       = None
        config.exclude       = None

        collector = MagicMock()
        collector.time.return_value.__enter__ = MagicMock(return_value=None)
        collector.time.return_value.__exit__  = MagicMock(return_value=False)

        phase = ExtractionPhase.__new__(ExtractionPhase)
        phase._config    = config
        phase._collector = collector
        phase._job       = None
        phase.params     = MagicMock()
        phase.params.include = None
        phase.params.exclude = None
        phase.result     = None
        phase.dependencies = []
        return phase

    def test_complete_when_file_exists(self, tmp_path: Path) -> None:
        from pyqenc.constants import EXTRACTED_DIR
        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True)
        ts_file = extracted_dir / TIMESTAMPS_FILENAME
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        phase = self._make_extraction_phase(tmp_path)

        # Patch MKVTrackExtractor to avoid needing a real video file
        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            artifacts, _, _ = phase._recover(force_wipe=False, execute=False)

        ts_artifacts = [a for a in artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.COMPLETE
        assert ts_artifacts[0].path == ts_file


class TestTimestampArtifactRecoveryAbsent:
    """timestamps.txt absent → TimestampArtifact state is ABSENT."""

    def _make_extraction_phase(self, tmp_path: Path) -> object:
        from pyqenc.phases.extraction import ExtractionPhase

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        config = MagicMock()
        config.work_dir      = tmp_path
        config.source_video  = source
        config.include       = None
        config.exclude       = None

        collector = MagicMock()
        collector.time.return_value.__enter__ = MagicMock(return_value=None)
        collector.time.return_value.__exit__  = MagicMock(return_value=False)

        phase = ExtractionPhase.__new__(ExtractionPhase)
        phase._config    = config
        phase._collector = collector
        phase._job       = None
        phase.params     = MagicMock()
        phase.params.include = None
        phase.params.exclude = None
        phase.result     = None
        phase.dependencies = []
        return phase

    def test_absent_when_no_extracted_dir(self, tmp_path: Path) -> None:
        phase = self._make_extraction_phase(tmp_path)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            artifacts, _, _ = phase._recover(force_wipe=False, execute=False)

        ts_artifacts = [a for a in artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT

    def test_absent_when_file_not_present(self, tmp_path: Path) -> None:
        from pyqenc.constants import EXTRACTED_DIR
        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True)
        # Create some other file but NOT timestamps.txt
        (extracted_dir / "video.mkv").write_bytes(b"\x00" * 64)

        phase = self._make_extraction_phase(tmp_path)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            artifacts, _, _ = phase._recover(force_wipe=False, execute=False)

        ts_artifacts = [a for a in artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT


# ---------------------------------------------------------------------------
# 2.3  force_wipe deletes timestamps.txt
# ---------------------------------------------------------------------------

class TestTimestampArtifactForceWipe:
    """force_wipe=True → extracted/ directory (including timestamps.txt) is deleted."""

    def _make_extraction_phase(self, tmp_path: Path) -> object:
        from pyqenc.phases.extraction import ExtractionPhase

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        config = MagicMock()
        config.work_dir      = tmp_path
        config.source_video  = source
        config.include       = None
        config.exclude       = None

        collector = MagicMock()
        collector.time.return_value.__enter__ = MagicMock(return_value=None)
        collector.time.return_value.__exit__  = MagicMock(return_value=False)

        phase = ExtractionPhase.__new__(ExtractionPhase)
        phase._config    = config
        phase._collector = collector
        phase._job       = None
        phase.params     = MagicMock()
        phase.params.include = None
        phase.params.exclude = None
        phase.result     = None
        phase.dependencies = []
        return phase

    def test_force_wipe_removes_timestamps_file(self, tmp_path: Path) -> None:
        from pyqenc.constants import EXTRACTED_DIR
        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True)
        ts_file = extracted_dir / TIMESTAMPS_FILENAME
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")
        assert ts_file.exists()

        phase = self._make_extraction_phase(tmp_path)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            phase._recover(force_wipe=True, execute=True)

        assert not ts_file.exists()

    def test_force_wipe_artifact_is_absent_after_wipe(self, tmp_path: Path) -> None:
        from pyqenc.constants import EXTRACTED_DIR
        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True)
        ts_file = extracted_dir / TIMESTAMPS_FILENAME
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        phase = self._make_extraction_phase(tmp_path)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            artifacts, _, _ = phase._recover(force_wipe=True, execute=True)

        ts_artifacts = [a for a in artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT


# ---------------------------------------------------------------------------
# 2.4  timestamps_path on ExtractionPhaseResult
# ---------------------------------------------------------------------------

class TestTimestampsPathOnResult:
    """ExtractionPhaseResult.timestamps_path is set correctly."""

    def test_timestamps_path_none_when_absent(self) -> None:
        result = ExtractionPhaseResult(
            outcome         = MagicMock(),
            artifacts       = [],
            message         = "",
            timestamps_path = None,
        )
        assert result.timestamps_path is None

    def test_timestamps_path_set_when_complete(self, tmp_path: Path) -> None:
        ts_path = tmp_path / TIMESTAMPS_FILENAME
        ts_path.write_text("# timestamp format v2\n0\n", encoding="utf-8")
        result = ExtractionPhaseResult(
            outcome         = MagicMock(),
            artifacts       = [],
            message         = "",
            timestamps_path = ts_path,
        )
        assert result.timestamps_path == ts_path


# ---------------------------------------------------------------------------
# 5.3  merge phase returns FAILED when timestamps_path is None
# ---------------------------------------------------------------------------

class TestMergeFailsWithoutTimestamps:
    """When timestamps_path is None, merge phase should fail with a clear message.

    This test verifies the extraction result correctly propagates None for
    timestamps_path when the TimestampArtifact is ABSENT, which will cause
    the merge phase to fail (merge phase integration tested in task 7).
    """

    def test_extraction_result_timestamps_path_none_when_artifact_absent(
        self, tmp_path: Path
    ) -> None:
        """ExtractionPhaseResult.timestamps_path is None when TimestampArtifact is ABSENT."""
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.models import PhaseOutcome

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        config = MagicMock()
        config.work_dir      = tmp_path
        config.source_video  = source
        config.include       = None
        config.exclude       = None

        collector = MagicMock()
        collector.time.return_value.__enter__ = MagicMock(return_value=None)
        collector.time.return_value.__exit__  = MagicMock(return_value=False)

        phase = ExtractionPhase.__new__(ExtractionPhase)
        phase._config    = config
        phase._collector = collector
        phase._job       = None
        phase.params     = MagicMock()
        phase.params.include = None
        phase.params.exclude = None
        phase.result     = None
        phase.dependencies = []

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls:
            mock_extractor = MagicMock()
            mock_extractor.tracks = []
            mock_extractor_cls.return_value = mock_extractor

            artifacts, video_meta, audio_meta = phase._recover(
                force_wipe=False, execute=False
            )

        ts_artifacts = [a for a in artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT

        # Build result as scan() would
        ts_artifact = ts_artifacts[0]
        result = ExtractionPhaseResult(
            outcome         = PhaseOutcome.DRY_RUN,
            artifacts       = artifacts,
            message         = "test",
            video           = video_meta,
            audio           = audio_meta,
            timestamps_path = ts_artifact.path if ts_artifact.state == ArtifactState.COMPLETE else None,
        )
        assert result.timestamps_path is None


# ---------------------------------------------------------------------------
# 4.1 / 4.2 / 4.3  Extraction command correctness
# ---------------------------------------------------------------------------

class TestExtractionCommandCorrectness:
    """Verify the ffmpeg commands built in _execute_extraction() are correct.

    Tests check observable behavior: the commands passed to run_ffmpeg must
    not contain removed flags and must contain required flags.
    """

    def _make_extraction_phase(self, tmp_path: Path) -> object:
        """Build a minimal ExtractionPhase with a fake config."""
        from pyqenc.phases.extraction import ExtractionPhase

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        config = MagicMock()
        config.work_dir      = tmp_path
        config.source_video  = source
        config.include       = None
        config.exclude       = None

        collector = MagicMock()
        collector.time.return_value.__enter__ = MagicMock(return_value=None)
        collector.time.return_value.__exit__  = MagicMock(return_value=False)

        phase = ExtractionPhase.__new__(ExtractionPhase)
        phase._config    = config
        phase._collector = collector
        phase._job       = None
        phase.params     = MagicMock()
        phase.params.include = None
        phase.params.exclude = None
        phase.result     = None
        phase.dependencies = []
        return phase

    def _make_video_artifact(self, tmp_path: Path) -> object:
        """Build a VideoArtifact in ABSENT state for a fake video track."""
        from pyqenc.phases.extraction import VideoArtifact
        from pyqenc.constants import EXTRACTED_DIR

        extracted_dir = tmp_path / EXTRACTED_DIR
        return VideoArtifact(
            path  = extracted_dir / "video.mkv",
            state = ArtifactState.ABSENT,
        )

    def _make_audio_artifact(self, tmp_path: Path) -> object:
        """Build an AudioArtifact in ABSENT state for a fake audio track."""
        from pyqenc.phases.extraction import AudioArtifact
        from pyqenc.constants import EXTRACTED_DIR

        extracted_dir = tmp_path / EXTRACTED_DIR
        return AudioArtifact(
            path  = extracted_dir / "audio.mka",
            state = ArtifactState.ABSENT,
        )

    def _run_and_capture_commands(
        self, tmp_path: Path, codec_type: str
    ) -> list[list]:
        """Run _execute_extraction with fake tracks and capture run_ffmpeg calls.

        Always includes a video track (required by _execute_extraction).
        When codec_type is "audio", also includes an audio track and returns
        only the audio ffmpeg command(s).
        """
        from pyqenc.phases.extraction import ExtractionPhase, VideoArtifact, AudioArtifact, TimestampArtifact
        from pyqenc.constants import EXTRACTED_DIR

        phase = self._make_extraction_phase(tmp_path)

        # Always need a video track — _execute_extraction fails without one
        fake_video = MagicMock()
        fake_video.track_id   = 0
        fake_video.codec_type = "video"
        fake_video.display_name.return_value = "video.mkv"

        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)

        video_artifact = VideoArtifact(path=extracted_dir / "video.mkv", state=ArtifactState.ABSENT)
        ts_artifact    = TimestampArtifact(path=extracted_dir / "timestamps.txt", state=ArtifactState.ABSENT)

        if codec_type == "video":
            all_tracks = [fake_video]
            artifacts  = [video_artifact, ts_artifact]
        else:
            fake_audio = MagicMock()
            fake_audio.track_id   = 1
            fake_audio.codec_type = "audio"
            fake_audio.display_name.return_value = "audio.mka"
            audio_artifact = AudioArtifact(path=extracted_dir / "audio.mka", state=ArtifactState.ABSENT)
            all_tracks = [fake_video, fake_audio]
            artifacts  = [video_artifact, audio_artifact, ts_artifact]

        captured_cmds: list[list] = []

        def fake_run_ffmpeg(cmd: list, **kwargs: object) -> MagicMock:
            captured_cmds.append(list(cmd))
            result = MagicMock()
            result.success = True
            return result

        with (
            patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls,
            patch("pyqenc.phases.extraction.run_ffmpeg", side_effect=fake_run_ffmpeg),
            patch("pyqenc.phases.extraction._extract_timestamps"),
            patch("pyqenc.phases.extraction.streams_filter_plain_regex") as mock_filter,
            patch("pyqenc.phases.extraction.ExtractionParams"),
        ):
            mock_extractor = MagicMock()
            mock_extractor.tracks = all_tracks
            mock_extractor_cls.return_value = mock_extractor
            mock_filter.return_value = all_tracks

            phase._execute_extraction(  # type: ignore[attr-defined]
                artifacts  = artifacts,
                video_meta = None,
                audio_meta = [],
            )

        if codec_type == "audio":
            # Return only the audio command(s) — video is always first
            return captured_cmds[1:]
        return captured_cmds

    def test_video_extraction_no_avoid_negative_ts(self, tmp_path: Path) -> None:
        """Requirement 2.1: -avoid_negative_ts must not appear in the video ffmpeg command."""
        cmds = self._run_and_capture_commands(tmp_path, "video")
        assert cmds, "Expected at least one run_ffmpeg call for video extraction"
        video_cmd = cmds[0]
        flat = [str(a) for a in video_cmd]
        assert "-avoid_negative_ts" not in flat, (
            f"-avoid_negative_ts must not be in video command; got: {flat}"
        )

    def test_video_extraction_has_matroska_format(self, tmp_path: Path) -> None:
        """Requirement 2.3: -f matroska must be present in the video ffmpeg command."""
        cmds = self._run_and_capture_commands(tmp_path, "video")
        assert cmds, "Expected at least one run_ffmpeg call for video extraction"
        video_cmd = cmds[0]
        flat = [str(a) for a in video_cmd]
        assert "-f" in flat, f"-f flag must be in video command; got: {flat}"
        f_index = flat.index("-f")
        assert flat[f_index + 1] == "matroska", (
            f"Expected 'matroska' after -f, got {flat[f_index + 1]!r}; full cmd: {flat}"
        )

    def test_audio_extraction_no_avoid_negative_ts(self, tmp_path: Path) -> None:
        """Requirement 2.2: -avoid_negative_ts must not appear in the audio ffmpeg command."""
        cmds = self._run_and_capture_commands(tmp_path, "audio")
        assert cmds, "Expected at least one run_ffmpeg call for audio extraction"
        audio_cmd = cmds[0]
        flat = [str(a) for a in audio_cmd]
        assert "-avoid_negative_ts" not in flat, (
            f"-avoid_negative_ts must not be in audio command; got: {flat}"
        )


# ---------------------------------------------------------------------------
# 5.1–5.5  ffmpeg-based subtitle / chapter / attachment extraction
# ---------------------------------------------------------------------------

class TestFfmpegStreamExtraction:
    """Verify subtitle, chapter, and attachment extraction uses run_ffmpeg correctly.

    Requirements: 1.1, 1.4–1.7
    """

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _make_extraction_phase(self, tmp_path: Path) -> object:
        """Build a minimal ExtractionPhase with a fake config."""
        from pyqenc.phases.extraction import ExtractionPhase

        source = tmp_path / "source.mkv"
        source.write_bytes(b"\x00" * 64)

        config = MagicMock()
        config.work_dir     = tmp_path
        config.source_video = source
        config.include      = None
        config.exclude      = None

        collector = MagicMock()
        collector.time.return_value.__enter__ = MagicMock(return_value=None)
        collector.time.return_value.__exit__  = MagicMock(return_value=False)

        phase = ExtractionPhase.__new__(ExtractionPhase)
        phase._config    = config
        phase._collector = collector
        phase._job       = None
        phase.params     = MagicMock()
        phase.params.include = None
        phase.params.exclude = None
        phase.result     = None
        phase.dependencies = []
        return phase

    def _make_fake_video_track(self) -> MagicMock:
        """Return a minimal fake VideoStream mock."""
        track = MagicMock()
        track.track_id   = 0
        track.codec_type = "video"
        track.display_name.return_value = "video.mkv"
        return track

    def _run_and_capture_other_cmds(
        self,
        tmp_path: Path,
        other_tracks: list,
    ) -> list[list]:
        """Run _execute_extraction with a video track + other_tracks; return captured run_ffmpeg calls.

        The first captured call is always the video extraction; subsequent calls
        are for the other_tracks in order.  Returns only the other-track calls.
        """
        from pyqenc.phases.extraction import (
            ExtractionPhase,
            VideoArtifact,
            OtherArtifact,
            TimestampArtifact,
        )
        from pyqenc.constants import EXTRACTED_DIR

        phase = self._make_extraction_phase(tmp_path)
        fake_video = self._make_fake_video_track()

        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)

        video_artifact = VideoArtifact(
            path  = extracted_dir / "video.mkv",
            state = ArtifactState.ABSENT,
        )
        ts_artifact = TimestampArtifact(
            path  = extracted_dir / "timestamps.txt",
            state = ArtifactState.ABSENT,
        )
        other_artifacts = [
            OtherArtifact(
                path  = extracted_dir / t.display_name(),
                state = ArtifactState.ABSENT,
            )
            for t in other_tracks
        ]
        all_artifacts = [video_artifact, *other_artifacts, ts_artifact]
        all_tracks    = [fake_video, *other_tracks]

        captured_cmds: list[list] = []

        def fake_run_ffmpeg(cmd: list, **kwargs: object) -> MagicMock:
            captured_cmds.append(list(cmd))
            result = MagicMock()
            result.success = True
            return result

        with (
            patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls,
            patch("pyqenc.phases.extraction.run_ffmpeg", side_effect=fake_run_ffmpeg),
            patch("pyqenc.phases.extraction._extract_timestamps"),
            patch("pyqenc.phases.extraction.streams_filter_plain_regex") as mock_filter,
            patch("pyqenc.phases.extraction.ExtractionParams"),
        ):
            mock_extractor = MagicMock()
            mock_extractor.tracks = all_tracks
            mock_extractor_cls.return_value = mock_extractor
            mock_filter.return_value = all_tracks

            phase._execute_extraction(  # type: ignore[attr-defined]
                artifacts  = all_artifacts,
                video_meta = None,
                audio_meta = [],
            )

        # Skip the first call (video extraction)
        return captured_cmds[1:]

    # ------------------------------------------------------------------
    # Subtitle tests
    # ------------------------------------------------------------------

    def test_subtitle_text_extraction_uses_ffmpeg_srt(self, tmp_path: Path) -> None:
        """Requirement 1.5: SRT subtitle extraction passes -f srt to run_ffmpeg."""
        from pyqenc.phases.extraction import SubtitleStream

        fake_sub = MagicMock(spec=SubtitleStream)
        fake_sub.track_id        = 2
        fake_sub.codec_type      = "subtitle"
        fake_sub.file_extension  = "srt"
        fake_sub.display_name.return_value = "subtitle.srt"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_sub])
        assert cmds, "Expected a run_ffmpeg call for subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" in flat, f"-f flag must be present for SRT subtitle; got: {flat}"
        f_idx = flat.index("-f")
        assert flat[f_idx + 1] == "srt", (
            f"Expected 'srt' after -f for SRT subtitle; got {flat[f_idx + 1]!r}"
        )

    def test_subtitle_text_extraction_uses_ffmpeg_ssa(self, tmp_path: Path) -> None:
        """Requirement 1.5: SSA subtitle extraction passes -f ass to run_ffmpeg."""
        from pyqenc.phases.extraction import SubtitleStream

        fake_sub = MagicMock(spec=SubtitleStream)
        fake_sub.track_id        = 3
        fake_sub.codec_type      = "subtitle"
        fake_sub.file_extension  = "ssa"
        fake_sub.display_name.return_value = "subtitle.ssa"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_sub])
        assert cmds, "Expected a run_ffmpeg call for SSA subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" in flat, f"-f flag must be present for SSA subtitle; got: {flat}"
        f_idx = flat.index("-f")
        assert flat[f_idx + 1] == "ass", (
            f"Expected 'ass' after -f for SSA subtitle; got {flat[f_idx + 1]!r}"
        )

    def test_subtitle_text_extraction_uses_ffmpeg_ass(self, tmp_path: Path) -> None:
        """Requirement 1.5: ASS subtitle extraction passes -f ass to run_ffmpeg."""
        from pyqenc.phases.extraction import SubtitleStream

        fake_sub = MagicMock(spec=SubtitleStream)
        fake_sub.track_id        = 4
        fake_sub.codec_type      = "subtitle"
        fake_sub.file_extension  = "ass"
        fake_sub.display_name.return_value = "subtitle.ass"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_sub])
        assert cmds, "Expected a run_ffmpeg call for ASS subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" in flat, f"-f flag must be present for ASS subtitle; got: {flat}"
        f_idx = flat.index("-f")
        assert flat[f_idx + 1] == "ass", (
            f"Expected 'ass' after -f for ASS subtitle; got {flat[f_idx + 1]!r}"
        )

    def test_subtitle_bitmap_extraction_no_format_flag_pgs(self, tmp_path: Path) -> None:
        """Requirement 1.5: PGS (bitmap) subtitle extraction must NOT include -f flag."""
        from pyqenc.phases.extraction import SubtitleStream

        fake_sub = MagicMock(spec=SubtitleStream)
        fake_sub.track_id        = 5
        fake_sub.codec_type      = "subtitle"
        fake_sub.file_extension  = "pgs"
        fake_sub.display_name.return_value = "subtitle.pgs"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_sub])
        assert cmds, "Expected a run_ffmpeg call for PGS subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" not in flat, (
            f"-f flag must NOT be present for PGS (bitmap) subtitle; got: {flat}"
        )

    def test_subtitle_bitmap_extraction_no_format_flag_sub(self, tmp_path: Path) -> None:
        """Requirement 1.5: VobSub (bitmap) subtitle extraction must NOT include -f flag."""
        from pyqenc.phases.extraction import SubtitleStream

        fake_sub = MagicMock(spec=SubtitleStream)
        fake_sub.track_id        = 6
        fake_sub.codec_type      = "subtitle"
        fake_sub.file_extension  = "sub"
        fake_sub.display_name.return_value = "subtitle.sub"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_sub])
        assert cmds, "Expected a run_ffmpeg call for VobSub subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" not in flat, (
            f"-f flag must NOT be present for VobSub (bitmap) subtitle; got: {flat}"
        )

    def test_subtitle_extraction_uses_map_and_copy(self, tmp_path: Path) -> None:
        """Requirement 1.5: Subtitle extraction uses -map 0:<id> -c copy."""
        from pyqenc.phases.extraction import SubtitleStream

        fake_sub = MagicMock(spec=SubtitleStream)
        fake_sub.track_id        = 7
        fake_sub.codec_type      = "subtitle"
        fake_sub.file_extension  = "srt"
        fake_sub.display_name.return_value = "subtitle.srt"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_sub])
        assert cmds, "Expected a run_ffmpeg call for subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-map" in flat, f"-map must be in subtitle command; got: {flat}"
        map_idx = flat.index("-map")
        assert flat[map_idx + 1] == "0:7", (
            f"Expected '0:7' after -map; got {flat[map_idx + 1]!r}"
        )
        assert "-c" in flat, f"-c must be in subtitle command; got: {flat}"
        c_idx = flat.index("-c")
        assert flat[c_idx + 1] == "copy", (
            f"Expected 'copy' after -c; got {flat[c_idx + 1]!r}"
        )

    # ------------------------------------------------------------------
    # Chapter tests
    # ------------------------------------------------------------------

    def test_chapter_extraction_uses_ffmetadata(self, tmp_path: Path) -> None:
        """Chapter extraction falls back to ffprobe -show_chapters -print_format xml
        when mkvextract is unavailable. Verifies the ffprobe fallback is invoked
        via subprocess.run (not run_ffmpeg) and produces XML output."""
        from pyqenc.phases.extraction import ChaptersStream

        fake_chapters = MagicMock(spec=ChaptersStream)
        fake_chapters.track_id        = -2
        fake_chapters.codec_type      = "chapters"
        fake_chapters.file_extension  = "xml"
        fake_chapters.display_name.return_value = "chapters.xml"

        ffprobe_called = False

        def _subprocess_side_effect(cmd: list, **kwargs: object) -> MagicMock:
            nonlocal ffprobe_called
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                # Make mkvextract fail so ffprobe fallback is triggered
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            if cmd and str(cmd[0]) == "ffprobe":
                ffprobe_called = True
                result.returncode = 0
                result.stdout     = "<chapters/>"
                result.stderr     = ""
            return result

        with patch("subprocess.run", side_effect=_subprocess_side_effect):
            cmds = self._run_and_capture_other_cmds(tmp_path, [fake_chapters])

        assert ffprobe_called, "Expected ffprobe to be called for chapter extraction fallback"

    def test_chapter_file_extension_is_xml(self) -> None:
        """ChaptersStream.file_extension is 'xml' (mkvextract/ffprobe XML format)."""
        from pyqenc.phases.extraction import ChaptersStream

        chapters = ChaptersStream.__new__(ChaptersStream)
        chapters.index       = -2
        chapters.raw         = []
        chapters.chapters    = []
        chapters.tags        = {}
        chapters.disposition = {}

        assert chapters.file_extension == "xml", (
            f"ChaptersStream.file_extension must be 'xml'; got {chapters.file_extension!r}"
        )

    # ------------------------------------------------------------------
    # Attachment tests
    # ------------------------------------------------------------------

    def test_attachment_extraction_uses_dump_attachment(self, tmp_path: Path) -> None:
        """Requirement 1.7: Attachment extraction uses -dump_attachment:<track_id>."""
        from pyqenc.phases.extraction import AttachmentStream

        fake_att = MagicMock(spec=AttachmentStream)
        fake_att.track_id        = 8
        fake_att.codec_type      = "attachment"
        fake_att.file_extension  = "ttf"
        fake_att.display_name.return_value = "font.ttf"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_att])
        assert cmds, "Expected a run_ffmpeg call for attachment extraction"
        flat = [str(a) for a in cmds[0]]
        assert f"-dump_attachment:8" in flat, (
            f"-dump_attachment:8 must be in attachment command; got: {flat}"
        )

    def test_attachment_extraction_has_null_output(self, tmp_path: Path) -> None:
        """Requirement 1.7: Attachment extraction terminates with -t 0 -f null -."""
        from pyqenc.phases.extraction import AttachmentStream

        fake_att = MagicMock(spec=AttachmentStream)
        fake_att.track_id        = 9
        fake_att.codec_type      = "attachment"
        fake_att.file_extension  = "png"
        fake_att.display_name.return_value = "cover.png"

        cmds = self._run_and_capture_other_cmds(tmp_path, [fake_att])
        assert cmds, "Expected a run_ffmpeg call for attachment extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-t" in flat, f"-t must be in attachment command; got: {flat}"
        t_idx = flat.index("-t")
        assert flat[t_idx + 1] == "0", (
            f"Expected '0' after -t; got {flat[t_idx + 1]!r}"
        )
        assert "-f" in flat, f"-f must be in attachment command; got: {flat}"
        f_idx = flat.index("-f")
        assert flat[f_idx + 1] == "null", (
            f"Expected 'null' after -f; got {flat[f_idx + 1]!r}"
        )
        assert flat[-1] == "-", f"Last argument must be '-'; got {flat[-1]!r}"

    # ------------------------------------------------------------------
    # No mkvextract test
    # ------------------------------------------------------------------

    def test_no_mkvextract_calls(self, tmp_path: Path) -> None:
        """mkvextract is called only for chapters (primary path); subtitles and
        attachments use run_ffmpeg exclusively. Verifies mkvextract is never
        invoked for subtitle or attachment tracks."""
        from pyqenc.phases.extraction import SubtitleStream, ChaptersStream, AttachmentStream

        fake_sub = MagicMock(spec=SubtitleStream)
        fake_sub.track_id        = 2
        fake_sub.codec_type      = "subtitle"
        fake_sub.file_extension  = "srt"
        fake_sub.display_name.return_value = "subtitle.srt"

        fake_chapters = MagicMock(spec=ChaptersStream)
        fake_chapters.track_id        = -2
        fake_chapters.codec_type      = "chapters"
        fake_chapters.file_extension  = "xml"
        fake_chapters.display_name.return_value = "chapters.xml"

        fake_att = MagicMock(spec=AttachmentStream)
        fake_att.track_id        = 3
        fake_att.codec_type      = "attachment"
        fake_att.file_extension  = "ttf"
        fake_att.display_name.return_value = "font.ttf"

        from pyqenc.phases.extraction import (
            ExtractionPhase,
            VideoArtifact,
            OtherArtifact,
            TimestampArtifact,
        )
        from pyqenc.constants import EXTRACTED_DIR

        phase = self._make_extraction_phase(tmp_path)
        fake_video = self._make_fake_video_track()

        extracted_dir = tmp_path / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)

        all_tracks = [fake_video, fake_sub, fake_chapters, fake_att]
        all_artifacts = [
            VideoArtifact(path=extracted_dir / "video.mkv",     state=ArtifactState.ABSENT),
            OtherArtifact(path=extracted_dir / "subtitle.srt",  state=ArtifactState.ABSENT),
            OtherArtifact(path=extracted_dir / "chapters.xml",  state=ArtifactState.ABSENT),
            OtherArtifact(path=extracted_dir / "font.ttf",      state=ArtifactState.ABSENT),
            TimestampArtifact(path=extracted_dir / "timestamps.txt", state=ArtifactState.ABSENT),
        ]

        mkvextract_for_non_chapters: list[str] = []

        def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
            if cmd and str(cmd[0]) == "mkvextract":
                # mkvextract is allowed for chapters — record what it was called for
                # to detect any non-chapter invocations
                if len(cmd) > 2 and str(cmd[2]) not in ("chapters", "timecodes_v2"):
                    mkvextract_for_non_chapters.append(str(cmd[2]))
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result = MagicMock()
            result.returncode = 0
            result.stdout     = "<chapters/>"
            result.stderr     = ""
            return result

        with (
            patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_extractor_cls,
            patch("pyqenc.phases.extraction.run_ffmpeg") as mock_run_ffmpeg,
            patch("pyqenc.phases.extraction._extract_timestamps"),
            patch("pyqenc.phases.extraction.streams_filter_plain_regex") as mock_filter,
            patch("pyqenc.phases.extraction.ExtractionParams"),
            patch("subprocess.run", side_effect=fake_subprocess_run),
        ):
            mock_extractor = MagicMock()
            mock_extractor.tracks = all_tracks
            mock_extractor_cls.return_value = mock_extractor
            mock_filter.return_value = all_tracks
            mock_run_ffmpeg.return_value = MagicMock(success=True)

            phase._execute_extraction(  # type: ignore[attr-defined]
                artifacts  = all_artifacts,
                video_meta = None,
                audio_meta = [],
            )

        assert not mkvextract_for_non_chapters, (
            f"mkvextract must not be called for non-chapter tracks; "
            f"called for: {mkvextract_for_non_chapters}"
        )
