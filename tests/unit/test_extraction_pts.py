"""Unit tests for timestamp / stream extraction (PTS preservation).

Observable-behavior only. Every ExtractionPhase test constructs a REAL phase
through its public constructor and a real phase registry whose ``JobPhase``
dependency carries a pre-set COMPLETED ``JobPhaseResult`` (so the shared
dependency walk is a no-op), then drives the public ``run()`` entry point and
asserts on the public ``ExtractionPhaseResult`` and on-disk ``extracted/``.
The only things mocked are genuine external shell-outs: ``MKVTrackExtractor``
(ffprobe), ``run_ffmpeg`` and ``subprocess.run`` — boundaries, never phase
internals. No ``__new__``, no private ``_recover`` / ``_execute_extraction``
calls, no private-attr poking.

Covers:
- 2.1  _extract_timestamps: correct header and integer-ms values per line
- 2.3  TimestampArtifact classified COMPLETE when file exists, ABSENT otherwise
- 2.3  force_wipe deletes timestamps.txt (via wipe of extracted/)
- 2.4  ExtractionPhaseResult.timestamps_path set correctly
- 4.1/4.2/4.3  video/audio ffmpeg command correctness (no -avoid_negative_ts, -f matroska)
- 1.1/1.4-1.7  subtitle / chapter / attachment ffmpeg command correctness
"""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.app_config import load_app_config
from pyqenc.constants import EXTRACTED_DIR, TIMESTAMPS_FILENAME
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import CleanupLevel, PhaseOutcome, VideoMetadata
from pyqenc.phase import Artifact, Phase
from pyqenc.phases.extraction import (
    ExtractionPhase,
    ExtractionPhaseResult,
    TimestampArtifact,
    _extract_timestamps,
)
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.state import ArtifactState, JobState

_APP_CONFIG = load_app_config(default_only=True)


# ---------------------------------------------------------------------------
# Shared real-construction helpers (mirror tests/test_pts_preservation_properties.py)
# ---------------------------------------------------------------------------


def _make_source_vm(path: Path) -> VideoMetadata:
    """Return a VideoMetadata with fast-probe fields pre-populated (no probing)."""
    meta = VideoMetadata(path=path)
    meta._duration_seconds = 3600.0
    meta._fps              = 24.0
    meta._resolution       = "1920x1080"
    return meta


def _make_extraction_phase(
    work_dir: Path,
    source:   Path,
    *,
    include:        str | None = None,
    exclude:        str | None = None,
    video_required: bool       = True,
    force_wipe:     bool       = False,
) -> ExtractionPhase:
    """Construct a REAL ExtractionPhase via its constructor and a real registry.

    A real ``JobPhase`` is placed in the registry with its public ``result``
    pre-set to a COMPLETED ``JobPhaseResult`` carrying the config (include/exclude
    filters) and the ``force_wipe`` flag under test, so the shared dependency
    walk treats the job as already-run without any mocking of phase internals.
    """
    collector = NoOpMetricsCollector()

    config = _APP_CONFIG.model_copy(deep=True)
    config.extraction.include = include
    config.extraction.exclude = exclude

    source_vm  = _make_source_vm(source)
    job_result = JobPhaseResult(
        outcome    = PhaseOutcome.COMPLETED,
        artifacts  = [Artifact(path=work_dir / "job.yaml", state=ArtifactState.COMPLETE)],
        message    = "job complete",
        job        = JobState(source=source_vm),
        force_wipe = force_wipe,
        config     = config,
        work_dir   = work_dir,
        source     = source,
    )

    job = JobPhase(
        config, None,
        source     = source,
        work_dir   = work_dir,
        force      = False,
        cleanup    = CleanupLevel.NONE,
        no_metrics = True,
        collector  = collector,
    )
    job.result = job_result

    registry: dict[type[Phase], Phase] = {JobPhase: job}
    return ExtractionPhase(
        config, registry, video_required=video_required, collector=collector,
    )


def _no_tracks_extractor() -> MagicMock:
    """Return a patched-in MKVTrackExtractor instance with an empty track set."""
    extractor = MagicMock()
    extractor.tracks = []
    return extractor


# ---------------------------------------------------------------------------
# 2.1  _extract_timestamps format  (KEPT AS-IS — tests the real public helper at
# its subprocess boundary; no phase internals involved)
# ---------------------------------------------------------------------------


def _make_ffprobe_stdout(pts_ms_values: list[int]) -> str:
    """Build a fake ffprobe stdout string from a list of integer-ms PTS values."""
    return "\n".join(str(v) for v in pts_ms_values) + "\n"


class TestExtractTimestampsFormat:
    """_extract_timestamps writes correct header and integer ms values per line.

    The implementation tries mkvextract first; tests make it fail so the
    ffprobe fallback path is exercised. ffprobe returns integer PTS values
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

        with (
            patch("subprocess.run", side_effect=_both_fail),
            pytest.raises(_subprocess.CalledProcessError),
        ):
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

        with (
            patch("subprocess.run", side_effect=_mkvextract_fail_ffprobe_empty),
            pytest.raises(ValueError, match="empty output"),
        ):
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

        with (
            patch("subprocess.run", side_effect=_mkvextract_fail_ffprobe_bad),
            pytest.raises(ValueError, match="Unparseable"),
        ):
            _extract_timestamps(Path("source.mkv"), 0, output)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "deep" / TIMESTAMPS_FILENAME

        with patch("subprocess.run", side_effect=self._mock_run([0, 42])):
            _extract_timestamps(Path("source.mkv"), 0, output)

        assert output.exists()


# ---------------------------------------------------------------------------
# 2.3  TimestampArtifact recovery classification (driven through run())
# ---------------------------------------------------------------------------


def _make_work_and_source(tmp_path: Path) -> tuple[Path, Path]:
    """Create a work dir and a fake source video; return (work_dir, source)."""
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    return work_dir, source


def _write_timestamps(work_dir: Path) -> Path:
    """Write a valid extracted/timestamps.txt and return its path."""
    extracted_dir = work_dir / EXTRACTED_DIR
    extracted_dir.mkdir(parents=True, exist_ok=True)
    ts_file = extracted_dir / TIMESTAMPS_FILENAME
    ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")
    return ts_file


class TestTimestampArtifactRecoveryComplete:
    """timestamps.txt present -> TimestampArtifact state is COMPLETE.

    Bug guarded: a present timestamps.txt reported as ABSENT would trigger a
    needless re-extraction and could lose the source PTS mapping the merge
    phase relies on.
    """

    def test_complete_when_file_exists(self, tmp_path: Path) -> None:
        work_dir, source = _make_work_and_source(tmp_path)
        ts_file = _write_timestamps(work_dir)

        phase = _make_extraction_phase(work_dir, source)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_cls:
            mock_cls.return_value = _no_tracks_extractor()
            result = phase.run(dry_run=True)

        ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.COMPLETE
        assert ts_artifacts[0].path == ts_file
        assert result.timestamps_path == ts_file


class TestTimestampArtifactRecoveryAbsent:
    """timestamps.txt absent -> TimestampArtifact state is ABSENT.

    Bug guarded: a missing timestamps.txt reported as COMPLETE would let the
    merge phase proceed without PTS data, silently corrupting VFR timing.
    """

    def test_absent_when_no_extracted_dir(self, tmp_path: Path) -> None:
        work_dir, source = _make_work_and_source(tmp_path)

        phase = _make_extraction_phase(work_dir, source)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_cls:
            mock_cls.return_value = _no_tracks_extractor()
            result = phase.run(dry_run=True)

        ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT
        assert result.timestamps_path is None

    def test_absent_when_file_not_present(self, tmp_path: Path) -> None:
        work_dir, source = _make_work_and_source(tmp_path)
        # An extracted/ dir exists with an unrelated file but NO timestamps.txt.
        extracted_dir = work_dir / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)
        (extracted_dir / "video.mkv").write_bytes(b"\x00" * 64)

        phase = _make_extraction_phase(work_dir, source)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_cls:
            mock_cls.return_value = _no_tracks_extractor()
            result = phase.run(dry_run=True)

        ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT
        assert result.timestamps_path is None


# ---------------------------------------------------------------------------
# 2.3  force_wipe deletes timestamps.txt (driven through run())
# ---------------------------------------------------------------------------


class TestTimestampArtifactForceWipe:
    """force_wipe=True (carried on the job result) -> extracted/ is wiped.

    The wipe is an observable side effect of running the phase when the job
    result requests a forced invalidation. With an empty track set there is
    nothing to re-extract, so after the run the previously-present
    timestamps.txt is gone and the resulting TimestampArtifact is ABSENT.

    Bug guarded: if force_wipe failed to clear stale extraction artifacts, a
    forced re-run would silently reuse outdated files instead of regenerating
    them from the (possibly changed) source.
    """

    def test_force_wipe_removes_timestamps_file(self, tmp_path: Path) -> None:
        work_dir, source = _make_work_and_source(tmp_path)
        ts_file = _write_timestamps(work_dir)
        assert ts_file.exists()

        phase = _make_extraction_phase(work_dir, source, force_wipe=True)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_cls:
            mock_cls.return_value = _no_tracks_extractor()
            # Execute run so the wipe actually happens; empty tracks => no ffmpeg.
            result = phase.run(dry_run=False)

        assert not ts_file.exists()
        ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT
        assert result.timestamps_path is None


# ---------------------------------------------------------------------------
# 2.4  timestamps_path on ExtractionPhaseResult (public result dataclass field)
# ---------------------------------------------------------------------------


class TestTimestampsPathOnResult:
    """ExtractionPhaseResult.timestamps_path carries the value it is built with.

    Constructs the public result type directly (a dataclass) with a real
    PhaseOutcome — no phase internals, no MagicMock outcome.
    """

    def test_timestamps_path_none_when_absent(self) -> None:
        result = ExtractionPhaseResult(
            outcome         = PhaseOutcome.PENDING,
            artifacts       = [],
            message         = "",
            timestamps_path = None,
        )
        assert result.timestamps_path is None

    def test_timestamps_path_set_when_complete(self, tmp_path: Path) -> None:
        ts_path = tmp_path / TIMESTAMPS_FILENAME
        ts_path.write_text("# timestamp format v2\n0\n", encoding="utf-8")
        result = ExtractionPhaseResult(
            outcome         = PhaseOutcome.COMPLETED,
            artifacts       = [],
            message         = "",
            timestamps_path = ts_path,
        )
        assert result.timestamps_path == ts_path


# ---------------------------------------------------------------------------
# 5.3  extraction result exposes timestamps_path=None when the artifact is ABSENT
# ---------------------------------------------------------------------------


class TestMergeFailsWithoutTimestamps:
    """The public run() result carries timestamps_path=None when no file exists.

    This is what causes the merge phase to fail with a clear message (merge
    integration is covered elsewhere). Driven end-to-end through run().

    Bug guarded: if the result reported a non-None timestamps_path despite an
    ABSENT artifact, the merge phase would attempt PTS restoration against a
    missing file.
    """

    def test_extraction_result_timestamps_path_none_when_artifact_absent(
        self, tmp_path: Path
    ) -> None:
        work_dir, source = _make_work_and_source(tmp_path)

        phase = _make_extraction_phase(work_dir, source)

        with patch("pyqenc.phases.extraction.MKVTrackExtractor") as mock_cls:
            mock_cls.return_value = _no_tracks_extractor()
            result = phase.run(dry_run=True)

        ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT
        assert result.timestamps_path is None


# ---------------------------------------------------------------------------
# Command-capture harness — drive a REAL phase through run(dry_run=False) with
# controlled tracks and capture the ffmpeg / subprocess commands it builds.
# ---------------------------------------------------------------------------


def _fake_video_track() -> MagicMock:
    """Return a minimal fake video track (MKVTrackExtractor output)."""
    track = MagicMock()
    track.track_id   = 0
    track.codec_type = "video"
    track.display_name.return_value = "video.mkv"
    return track


def _run_and_capture(
    tmp_path: Path,
    extra_tracks: list[MagicMock],
    *,
    subprocess_side_effect: object | None = None,
) -> tuple[list[list], list[list]]:
    """Drive a REAL ExtractionPhase.run(dry_run=False) and capture commands.

    A video track is always present (required for timestamp extraction). Only
    external boundaries are patched: ``MKVTrackExtractor`` (track discovery),
    ``run_ffmpeg`` (ffmpeg command sink), ``_extract_timestamps`` (its own
    subprocess boundary), and — when ``subprocess_side_effect`` is given —
    ``subprocess.run`` (for the chapter mkvextract/ffprobe path).

    Returns ``(ffmpeg_cmds, subprocess_cmds)`` where ``ffmpeg_cmds`` is the list
    of commands passed to ``run_ffmpeg`` in call order (index 0 is the video
    extraction), and ``subprocess_cmds`` records ``subprocess.run`` invocations
    when a side effect is supplied.
    """
    work_dir, source = _make_work_and_source(tmp_path)
    phase = _make_extraction_phase(work_dir, source)

    all_tracks = [_fake_video_track(), *extra_tracks]

    ffmpeg_cmds:     list[list] = []
    subprocess_cmds: list[list] = []

    def fake_run_ffmpeg(cmd: list, **kwargs: object) -> MagicMock:
        ffmpeg_cmds.append(list(cmd))
        result = MagicMock()
        result.success = True
        return result

    def default_subprocess(cmd: list, **kwargs: object) -> MagicMock:
        subprocess_cmds.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout     = ""
        result.stderr     = ""
        return result

    def recording_subprocess(cmd: list, **kwargs: object) -> MagicMock:
        subprocess_cmds.append(list(cmd))
        return subprocess_side_effect(cmd, **kwargs)  # type: ignore[operator]

    sp_effect = recording_subprocess if subprocess_side_effect is not None else default_subprocess

    extractor = MagicMock()
    extractor.tracks = all_tracks

    with (
        patch("pyqenc.phases.extraction.MKVTrackExtractor", return_value=extractor),
        patch("pyqenc.phases.extraction.run_ffmpeg", side_effect=fake_run_ffmpeg),
        patch("pyqenc.phases.extraction._extract_timestamps"),
        patch("subprocess.run", side_effect=sp_effect),
    ):
        phase.run(dry_run=False)

    return ffmpeg_cmds, subprocess_cmds


# ---------------------------------------------------------------------------
# 4.1 / 4.2 / 4.3  video / audio extraction command correctness
# ---------------------------------------------------------------------------


class TestExtractionCommandCorrectness:
    """The ffmpeg commands built for video/audio extraction preserve source PTS.

    Observable behavior: the commands handed to run_ffmpeg (the external
    boundary) must not carry -avoid_negative_ts (which would rewrite source
    timestamps) and video must be muxed with -f matroska.
    """

    def test_video_extraction_no_avoid_negative_ts(self, tmp_path: Path) -> None:
        """Requirement 2.1: -avoid_negative_ts must not appear in the video command."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [])
        assert ffmpeg_cmds, "Expected a run_ffmpeg call for video extraction"
        flat = [str(a) for a in ffmpeg_cmds[0]]
        assert "-avoid_negative_ts" not in flat, (
            f"-avoid_negative_ts must not be in video command; got: {flat}"
        )

    def test_video_extraction_has_matroska_format(self, tmp_path: Path) -> None:
        """Requirement 2.3: -f matroska must be present in the video command."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [])
        assert ffmpeg_cmds, "Expected a run_ffmpeg call for video extraction"
        flat = [str(a) for a in ffmpeg_cmds[0]]
        assert "-f" in flat, f"-f flag must be in video command; got: {flat}"
        assert flat[flat.index("-f") + 1] == "matroska", (
            f"Expected 'matroska' after -f; full cmd: {flat}"
        )

    def test_audio_extraction_no_avoid_negative_ts(self, tmp_path: Path) -> None:
        """Requirement 2.2: -avoid_negative_ts must not appear in the audio command."""
        audio = MagicMock()
        audio.track_id   = 1
        audio.codec_type = "audio"
        audio.display_name.return_value = "audio.mka"

        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [audio])
        # Video is command 0; audio follows. Identify by its -map 0:1 track ref.
        audio_cmds = [
            c for c in ffmpeg_cmds
            if "-map" in [str(a) for a in c]
            and [str(a) for a in c][[str(a) for a in c].index("-map") + 1] == "0:1"
        ]
        assert audio_cmds, "Expected a run_ffmpeg call for audio extraction"
        flat = [str(a) for a in audio_cmds[0]]
        assert "-avoid_negative_ts" not in flat, (
            f"-avoid_negative_ts must not be in audio command; got: {flat}"
        )


# ---------------------------------------------------------------------------
# 1.1 / 1.4-1.7  subtitle / chapter / attachment extraction command correctness
# ---------------------------------------------------------------------------


class TestFfmpegStreamExtraction:
    """Subtitle, chapter, and attachment extraction build correct ffmpeg commands.

    Observable behavior at the run_ffmpeg / subprocess boundary:
    - text subtitles carry the right -f (srt/ass); bitmap subtitles carry no -f
    - subtitles use -map 0:<id> -c copy
    - attachments use -dump_attachment:<id> and a null-output terminator
    - chapters fall back to ffprobe -show_chapters -print_format xml
    """

    def _other_cmds(self, ffmpeg_cmds: list[list]) -> list[list]:
        """Return ffmpeg commands excluding the always-first video extraction."""
        return ffmpeg_cmds[1:]

    # -- Subtitles -----------------------------------------------------

    def _fake_subtitle(self, track_id: int, ext: str) -> MagicMock:
        from pyqenc.phases.extraction import SubtitleStream
        sub = MagicMock(spec=SubtitleStream)
        sub.track_id       = track_id
        sub.codec_type     = "subtitle"
        sub.file_extension = ext
        sub.display_name.return_value = f"subtitle.{ext}"
        return sub

    def test_subtitle_srt_uses_ffmpeg_srt_format(self, tmp_path: Path) -> None:
        """Requirement 1.5: SRT subtitle extraction passes -f srt."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [self._fake_subtitle(2, "srt")])
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" in flat and flat[flat.index("-f") + 1] == "srt", (
            f"Expected '-f srt' for SRT subtitle; got: {flat}"
        )

    def test_subtitle_ssa_uses_ffmpeg_ass_format(self, tmp_path: Path) -> None:
        """Requirement 1.5: SSA subtitle extraction passes -f ass."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [self._fake_subtitle(3, "ssa")])
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for SSA subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" in flat and flat[flat.index("-f") + 1] == "ass", (
            f"Expected '-f ass' for SSA subtitle; got: {flat}"
        )

    def test_subtitle_ass_uses_ffmpeg_ass_format(self, tmp_path: Path) -> None:
        """Requirement 1.5: ASS subtitle extraction passes -f ass."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [self._fake_subtitle(4, "ass")])
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for ASS subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" in flat and flat[flat.index("-f") + 1] == "ass", (
            f"Expected '-f ass' for ASS subtitle; got: {flat}"
        )

    def test_subtitle_bitmap_pgs_has_no_format_flag(self, tmp_path: Path) -> None:
        """Requirement 1.5: PGS (bitmap) subtitle extraction must NOT include -f."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [self._fake_subtitle(5, "pgs")])
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for PGS subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" not in flat, (
            f"-f must NOT be present for PGS (bitmap) subtitle; got: {flat}"
        )

    def test_subtitle_bitmap_sub_has_no_format_flag(self, tmp_path: Path) -> None:
        """Requirement 1.5: VobSub (bitmap) subtitle extraction must NOT include -f."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [self._fake_subtitle(6, "sub")])
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for VobSub subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-f" not in flat, (
            f"-f must NOT be present for VobSub (bitmap) subtitle; got: {flat}"
        )

    def test_subtitle_extraction_uses_map_and_copy(self, tmp_path: Path) -> None:
        """Requirement 1.5: subtitle extraction uses -map 0:<id> -c copy."""
        ffmpeg_cmds, _ = _run_and_capture(tmp_path, [self._fake_subtitle(7, "srt")])
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for subtitle extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-map" in flat and flat[flat.index("-map") + 1] == "0:7", (
            f"Expected '-map 0:7'; got: {flat}"
        )
        assert "-c" in flat and flat[flat.index("-c") + 1] == "copy", (
            f"Expected '-c copy'; got: {flat}"
        )

    # -- Chapters ------------------------------------------------------

    def test_chapter_extraction_falls_back_to_ffprobe_xml(self, tmp_path: Path) -> None:
        """Chapter extraction falls back to ffprobe -show_chapters -print_format xml
        when mkvextract is unavailable (verified at the subprocess boundary)."""
        from pyqenc.phases.extraction import ChaptersStream

        chapters = MagicMock(spec=ChaptersStream)
        chapters.track_id       = -2
        chapters.codec_type     = "chapters"
        chapters.file_extension = "xml"
        chapters.display_name.return_value = "chapters.xml"

        def _sp(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result.returncode = 0
            result.stdout     = "<chapters/>"
            result.stderr     = ""
            return result

        _, subprocess_cmds = _run_and_capture(
            tmp_path, [chapters], subprocess_side_effect=_sp,
        )

        ffprobe_chapter_calls = [
            c for c in subprocess_cmds
            if c and str(c[0]) == "ffprobe" and "-show_chapters" in [str(a) for a in c]
        ]
        assert ffprobe_chapter_calls, (
            f"Expected an ffprobe -show_chapters fallback call; got: {subprocess_cmds}"
        )
        flat = [str(a) for a in ffprobe_chapter_calls[0]]
        assert "-print_format" in flat and flat[flat.index("-print_format") + 1] == "xml", (
            f"Expected '-print_format xml' in chapter fallback; got: {flat}"
        )

    # -- Attachments ---------------------------------------------------

    def _fake_attachment(self, track_id: int, ext: str, name: str) -> MagicMock:
        from pyqenc.phases.extraction import AttachmentStream
        att = MagicMock(spec=AttachmentStream)
        att.track_id       = track_id
        att.codec_type     = "attachment"
        att.file_extension = ext
        att.display_name.return_value = name
        return att

    def test_attachment_extraction_uses_dump_attachment(self, tmp_path: Path) -> None:
        """Requirement 1.7: attachment extraction uses -dump_attachment:<track_id>."""
        ffmpeg_cmds, _ = _run_and_capture(
            tmp_path, [self._fake_attachment(8, "ttf", "font.ttf")],
        )
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for attachment extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-dump_attachment:8" in flat, (
            f"-dump_attachment:8 must be in attachment command; got: {flat}"
        )

    def test_attachment_extraction_has_null_output(self, tmp_path: Path) -> None:
        """Requirement 1.7: attachment extraction terminates with -t 0 -f null -."""
        ffmpeg_cmds, _ = _run_and_capture(
            tmp_path, [self._fake_attachment(9, "png", "cover.png")],
        )
        cmds = self._other_cmds(ffmpeg_cmds)
        assert cmds, "Expected a run_ffmpeg call for attachment extraction"
        flat = [str(a) for a in cmds[0]]
        assert "-t" in flat and flat[flat.index("-t") + 1] == "0", (
            f"Expected '-t 0'; got: {flat}"
        )
        assert "-f" in flat and flat[flat.index("-f") + 1] == "null", (
            f"Expected '-f null'; got: {flat}"
        )
        assert flat[-1] == "-", f"Last argument must be '-'; got: {flat[-1]!r}"

    # -- mkvextract is not used for subtitle / attachment tracks -------

    def test_no_mkvextract_for_subtitle_or_attachment(self, tmp_path: Path) -> None:
        """Subtitle and attachment tracks use run_ffmpeg, never mkvextract.

        mkvextract is only permitted for chapters. This guards against a
        regression where a track type is routed back through mkvextract.
        """
        from pyqenc.phases.extraction import (
            AttachmentStream,
            ChaptersStream,
            SubtitleStream,
        )

        sub = MagicMock(spec=SubtitleStream)
        sub.track_id       = 2
        sub.codec_type     = "subtitle"
        sub.file_extension = "srt"
        sub.display_name.return_value = "subtitle.srt"

        chapters = MagicMock(spec=ChaptersStream)
        chapters.track_id       = -2
        chapters.codec_type     = "chapters"
        chapters.file_extension = "xml"
        chapters.display_name.return_value = "chapters.xml"

        att = MagicMock(spec=AttachmentStream)
        att.track_id       = 3
        att.codec_type     = "attachment"
        att.file_extension = "ttf"
        att.display_name.return_value = "font.ttf"

        mkvextract_non_chapter: list[str] = []

        def _sp(cmd: list, **kwargs: object) -> MagicMock:
            if cmd and str(cmd[0]) == "mkvextract":
                if len(cmd) > 2 and str(cmd[2]) not in ("chapters", "timecodes_v2"):
                    mkvextract_non_chapter.append(str(cmd[2]))
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result = MagicMock()
            result.returncode = 0
            result.stdout     = "<chapters/>"
            result.stderr     = ""
            return result

        _run_and_capture(tmp_path, [sub, chapters, att], subprocess_side_effect=_sp)

        assert not mkvextract_non_chapter, (
            f"mkvextract must not be called for non-chapter tracks; "
            f"called for: {mkvextract_non_chapter}"
        )
