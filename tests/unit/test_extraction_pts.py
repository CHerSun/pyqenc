"""Unit tests for stream enumeration and container-artifact extraction.

Observable-behavior only. Every ExtractionPhase test constructs a REAL phase
through its public constructor and a real phase registry whose ``JobPhase``
dependency carries a pre-set COMPLETED ``JobPhaseResult`` (including the
job's ``File``), then drives the public ``run()`` entry point and asserts on
the public ``ExtractionPhaseResult``, on-disk ``extracted/`` and
``extraction.yaml``. The only things mocked are genuine external shell-outs:
``_probe_streams_json`` (ffprobe enumeration — fed canned ffprobe JSON),
``run_ffmpeg`` and ``subprocess.run`` / ``_extract_timestamps`` — boundaries,
never phase internals.

Covers:
- Enumeration: typed streams from ffprobe JSON (fps fraction, layout, forced,
  attachment detection, multi-video warning)
- Video/audio tracks are NEVER extracted (direct-from-source model)
- Subtitle / attachment / chapters extraction commands and file-trust wrapping
- ``extraction.yaml``: persisted on execute, loaded on reuse (no re-probe),
  re-enumerated on source-identity mismatch
- Result payload: stream objects plus the interim legacy views
- ``_extract_timestamps``: correct header and integer-ms values per line
"""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from pyqenc.app_config import load_app_config
from pyqenc.constants import EXTRACTED_DIR, TIMESTAMPS_FILENAME
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import CleanupLevel, PhaseOutcome
from pyqenc.phase import Artifact, PhaseRegistry
from pyqenc.phases.extraction import (
    ExtractionPhase,
    ExtractionPhaseResult,
    TimestampArtifact,
    _enumerate_streams,
    _extract_timestamps,
)
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.state import ArtifactState
from pyqenc.stream_model import File
from pyqenc.utils.ffmpeg_runner import (
    _PROGRESS_FLAGS,
    FFmpegRequest,
    FFmpegRunResult,
    compose_command,
)
from pyqenc.utils.long_path import LongPath
from pyqenc.utils.yaml_utils import write_yaml_atomic

_APP_CONFIG = load_app_config(default_only=True)


# ---------------------------------------------------------------------------
# Shared real-construction helpers
# ---------------------------------------------------------------------------




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
    pre-set to a COMPLETED ``JobPhaseResult`` carrying the config (include/
    exclude filters), the job's ``File`` and the ``force_wipe`` flag under
    test, so the shared dependency walk treats the job as already-run without
    any mocking of phase internals.
    """
    collector = NoOpMetricsCollector()

    config = _APP_CONFIG.model_copy(deep=True)
    config.extraction.include = include
    config.extraction.exclude = exclude

    job_result = JobPhaseResult(
        outcome    = PhaseOutcome.COMPLETED,
        artifacts  = [Artifact(path=work_dir / "job.yaml", state=ArtifactState.COMPLETE)],
        message    = "job complete",
        file       = File(path=source, file_size_bytes=source.stat().st_size if source.exists() else 64),
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

    registry: PhaseRegistry = {JobPhase: job}
    return ExtractionPhase(
        config, registry, video_required=video_required, collector=collector,
    )


def _make_work_and_source(tmp_path: Path) -> tuple[Path, Path]:
    """Create a work dir and a fake source video; return (work_dir, source)."""
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.mkv"
    source.write_bytes(b"\x00" * 64)
    return work_dir, source


# ---------------------------------------------------------------------------
# Canned ffprobe JSON (the enumeration boundary)
# ---------------------------------------------------------------------------

def _video_json(track_id: int = 0, codec: str = "hevc") -> dict:
    return {
        "index": track_id, "codec_type": "video", "codec_name": codec,
        "r_frame_rate": "24000/1001", "width": 1920, "height": 1080,
        "pix_fmt": "yuv420p10le", "duration": "5964.48", "start_time": "0.000000",
        "tags": {"language": "eng"},
    }


def _audio_json(track_id: int = 1, codec: str = "flac") -> dict:
    return {
        "index": track_id, "codec_type": "audio", "codec_name": codec,
        "channel_layout": "5.1(side)", "duration": "5964.50",
        "tags": {"language": "eng", "title": "Surround 5.1"},
    }


def _subtitle_json(track_id: int = 3, codec: str = "subrip", forced: int = 0) -> dict:
    return {
        "index": track_id, "codec_type": "subtitle", "codec_name": codec,
        "duration": "5964.48",
        "disposition": {"forced": forced},
        "tags": {"language": "eng", "title": "Full"},
    }


def _attachment_json(track_id: int = 4, filename: str = "font.ttf") -> dict:
    return {
        "index": track_id, "codec_type": "video", "codec_name": "ttf",
        "disposition": {"attached_pic": 1},
        "tags": {"filename": filename, "mimetype": "image/x-font"},
    }


def _ffprobe_json(*streams: dict, chapters: list | None = None) -> dict:
    data: dict = {"streams": list(streams)}
    if chapters is not None:
        data["chapters"] = chapters
    return data


# ---------------------------------------------------------------------------
# Enumeration (pure function on parsed ffprobe JSON)
# ---------------------------------------------------------------------------

class TestEnumerateStreams:
    def test_typed_streams_built_from_ffprobe_json(self) -> None:
        """Bug prevented: enumeration losing type-specific fast-facet fields —
        fps fraction, channel layout, forced flag, attachment filename."""
        file = File(path=LongPath("D:/media/source.mkv"), file_size_bytes=5)
        video, audio, subs, attachments, has_chapters = _enumerate_streams(
            _ffprobe_json(
                _video_json(), _audio_json(), _subtitle_json(), _attachment_json(),
                chapters=[{"id": 0}],
            ),
            file,
        )

        assert len(video) == 1 and video[0].info.fps_fraction == __import__("fractions").Fraction(24000, 1001)
        assert video[0].info.resolution == "1920x1080"
        assert len(audio) == 1 and audio[0].info.layout is not None
        assert audio[0].info.layout.normalized == "5.1"
        assert len(subs) == 1 and subs[0].info.is_forced is False
        assert len(attachments) == 1 and attachments[0].info.filename == "font.ttf"
        assert has_chapters is True
        # Every stream composes the single job File.
        assert all(s.file is file for s in [*video, *audio, *subs, *attachments])

    def test_data_streams_are_skipped(self) -> None:
        file = File(path=LongPath("src.mkv"))
        video, audio, subs, attachments, has_chapters = _enumerate_streams(
            _ffprobe_json(_video_json(), {"index": 9, "codec_type": "data", "codec_name": "bin_data"}),
            file,
        )
        assert len(video) == 1 and not audio and not subs and not attachments
        assert has_chapters is False

    def test_multi_video_streams_warn(self) -> None:
        """Req 13: more than one video stream triggers a prominent warning —
        scene detection and default-selector consumers use the first."""
        import pyqenc.phases.extraction as ex_mod

        file = File(path=LongPath("src.mkv"))
        with patch.object(ex_mod.logger, "warning") as warn_mock:
            _enumerate_streams(_ffprobe_json(_video_json(0), _video_json(2)), file)

        assert any("video stream" in str(call) for call in warn_mock.call_args_list)


# ---------------------------------------------------------------------------
# 2.1  _extract_timestamps format (real public helper at its subprocess boundary)
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

    def _mock_run(self, pts_ms: list[int]) -> object:
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
        output = tmp_path / TIMESTAMPS_FILENAME
        with patch("subprocess.run", side_effect=self._mock_run([0, 42, 83])):
            _extract_timestamps(Path("source.mkv"), 0, output)
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# timestamp format v2"

    def test_values_are_integer_ms(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME
        with patch("subprocess.run", side_effect=self._mock_run([0, 42, 83, 125])):
            _extract_timestamps(Path("source.mkv"), 0, output)
        lines = output.read_text(encoding="utf-8").splitlines()
        assert [int(line) for line in lines[1:]] == sorted([0, 42, 83, 125])

    def test_output_file_created(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME
        with patch("subprocess.run", side_effect=self._mock_run([0, 42])):
            _extract_timestamps(Path("source.mkv"), 0, output)
        assert output.exists()

    def test_tmp_file_not_left_behind(self, tmp_path: Path) -> None:
        output = tmp_path / TIMESTAMPS_FILENAME
        with patch("subprocess.run", side_effect=self._mock_run([0, 42])):
            _extract_timestamps(Path("source.mkv"), 0, output)
        assert not (tmp_path / "timestamps.tmp").exists()


# ---------------------------------------------------------------------------
# Command-capture harness — drive a REAL phase through run(dry_run=False)
# ---------------------------------------------------------------------------

def _run_and_capture(
    tmp_path:  Path,
    ffprobe_data: dict,
    *,
    subprocess_side_effect: object | None = None,
    make_outputs: bool = True,
):
    """Drive a REAL ExtractionPhase.run(dry_run=False) with canned ffprobe JSON.

    Returns ``(ffmpeg_cmds, subprocess_cmds, work_dir, source)`` where
    ``ffmpeg_cmds`` holds composed launch argvs in call order.

    External boundaries patched: ``_probe_streams_json`` (canned JSON),
    ``run_ffmpeg`` (composed-argv sink), ``_extract_timestamps`` (own boundary),
    and — when a side effect is given — ``subprocess.run`` (chapters path).
    """
    work_dir, source = _make_work_and_source(tmp_path)
    phase = _make_extraction_phase(work_dir, source)

    ffmpeg_cmds:     list[list[str]] = []
    subprocess_cmds: list[list] = []

    def fake_run_ffmpeg(request: FFmpegRequest, **kwargs: object) -> FFmpegRunResult:
        ffmpeg_cmds.append([str(a) for a in compose_command(request)])
        result = MagicMock()
        result.success = True
        result.returncode = 0
        result.stderr_lines = []
        result.frame_count = None
        if make_outputs and request.output is not None:
            request.output.parent.mkdir(parents=True, exist_ok=True)
            request.output.write_bytes(b"x" * 16)
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

    with (
        patch("pyqenc.phases.extraction._probe_streams_json", return_value=ffprobe_data),
        patch("pyqenc.phases.extraction.run_ffmpeg", side_effect=fake_run_ffmpeg),
        patch("pyqenc.phases.extraction._extract_timestamps"),
        patch("pyqenc.phases.extraction.log_disk_space_info"),
        patch("subprocess.run", side_effect=sp_effect),
    ):
        phase.run(dry_run=False)

    return ffmpeg_cmds, subprocess_cmds, work_dir, source


# ---------------------------------------------------------------------------
# Direct-from-source: video/audio are never extracted
# ---------------------------------------------------------------------------

class TestNoVideoAudioExtraction:
    def test_video_and_audio_produce_no_ffmpeg_commands(self, tmp_path: Path) -> None:
        """Req 7.4: video and audio tracks are never copied to extracted/ —
        the phase extracts only container artifacts."""
        ffmpeg_cmds, _, work_dir, _ = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )
        assert ffmpeg_cmds == [], f"Unexpected extraction commands: {ffmpeg_cmds}"
        extracted = list((work_dir / EXTRACTED_DIR).glob("*")) if (work_dir / EXTRACTED_DIR).exists() else []
        assert extracted == [], f"Unexpected extracted files: {extracted}"

    def test_result_carries_streams_and_legacy_views(self, tmp_path: Path) -> None:
        """ExtractionPhaseResult exposes the stream objects; the interim
        legacy views derive from them and point at the SOURCE."""
        work_dir, source = _make_work_and_source(tmp_path)
        phase = _make_extraction_phase(work_dir, source)
        with (
            patch("pyqenc.phases.extraction._probe_streams_json",
                  return_value=_ffprobe_json(_video_json(), _audio_json())),
            patch("pyqenc.phases.extraction.log_disk_space_info"),
        ):
            result = phase.run(dry_run=True)

        assert result.video_stream is not None
        assert result.video_stream.info.fps_fraction is not None
        assert len(result.audio_streams) == 1
        assert result.audio_streams[0].info.layout is not None
        # Stream objects compose the job File — path is the source.
        assert result.video_stream is not None
        assert result.video_stream.file.path == File(path=source).path


# ---------------------------------------------------------------------------
# Golden composed argv — subtitle and attachment call sites
# ---------------------------------------------------------------------------

class TestExtractionCommandGolden:
    """Pin the full composed argv for the remaining extraction call sites."""

    def test_text_subtitle_copy_golden(self, tmp_path: Path) -> None:
        """Text subtitles: selector from as_input, single srt muxer for the
        .tmp output, file name owned by the stream class."""
        ffmpeg_cmds, _, work_dir, _ = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), _subtitle_json(track_id=3)),
        )
        sub_cmds = [c for c in ffmpeg_cmds if "-map" in c and c[c.index("-map") + 1] == "0:3"]
        assert sub_cmds, f"Expected a subtitle extraction command: {ffmpeg_cmds}"
        out_tmp = work_dir / EXTRACTED_DIR / "#3 (subrip) lang=eng title=Full.tmp"
        assert sub_cmds[0] == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-i", str(work_dir.parent / "source.mkv"),
            "-map", "0:3",
            "-c", "copy",
            "-map_chapters", "-1",
            "-f", "srt", str(out_tmp),
        ]

    def test_attachment_dump_golden(self, tmp_path: Path) -> None:
        """Attachments dump to a .tmp sibling (file-trust rule, Req 7.7) — the
        phase renames only on verified success."""
        ffmpeg_cmds, _, work_dir, _ = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), _attachment_json(track_id=4)),
            make_outputs=False,
        )
        att_cmds = [c for c in ffmpeg_cmds if "-dump_attachment:4" in c]
        assert att_cmds, f"Expected an attachment command: {ffmpeg_cmds}"
        tmp_target = work_dir / EXTRACTED_DIR / "#4 (attachment) font.tmp"
        assert att_cmds[0] == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-i", str(work_dir.parent / "source.mkv"),
            "-dump_attachment:4", str(tmp_target),
            "-t", "0",
            "-map_chapters", "-1",
            "-f", "null", "-",
        ]

    def test_bitmap_subtitle_stays_on_matroska_muxer(self, tmp_path: Path) -> None:
        ffmpeg_cmds, _, _, _ = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), _subtitle_json(track_id=5, codec="hdmv_pgs_subtitle")),
        )
        sub_cmds = [c for c in ffmpeg_cmds if "-map" in c and c[c.index("-map") + 1] == "0:5"]
        assert sub_cmds
        muxers = [sub_cmds[0][i + 1] for i, a in enumerate(sub_cmds[0]) if a == "-f"]
        assert muxers == ["matroska"]


# ---------------------------------------------------------------------------
# Subtitle / chapters dispatch
# ---------------------------------------------------------------------------

class TestSubtitleChaptersDispatch:
    def test_chapters_fall_back_to_ffprobe_xml(self, tmp_path: Path) -> None:
        """Chapter extraction falls back to ffprobe -show_chapters xml when
        mkvextract is unavailable (subprocess boundary)."""

        def _sp(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            if cmd and str(cmd[0]) == "mkvextract":
                raise _subprocess.CalledProcessError(1, cmd, stderr=b"not an mkv")
            result.returncode = 0
            result.stdout     = "<chapters/>"
            result.stderr     = ""
            return result

        _, subprocess_cmds, _, _ = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), chapters=[{"id": 0}]),
            subprocess_side_effect=_sp,
        )
        ffprobe_calls = [c for c in subprocess_cmds if c and str(c[0]) == "ffprobe" and "-show_chapters" in c]
        assert ffprobe_calls, f"Expected ffprobe chapters fallback: {subprocess_cmds}"
        flat = [str(a) for a in ffprobe_calls[0]]
        assert "-print_format" in flat and flat[flat.index("-print_format") + 1] == "xml"

    def test_subtitles_use_run_ffmpeg_never_mkvextract(self, tmp_path: Path) -> None:
        """Subtitle tracks extract through the unified runner, never mkvextract."""
        ffmpeg_cmds, subprocess_cmds, _, _ = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), _subtitle_json(track_id=2)),
        )
        assert any("-map" in c for c in ffmpeg_cmds)
        assert not [c for c in subprocess_cmds if c and str(c[0]) == "mkvextract"]


# ---------------------------------------------------------------------------
# extraction.yaml — persistence, reuse without re-probe, identity mismatch
# ---------------------------------------------------------------------------

class TestExtractionSidecarLifecycle:
    def test_sidecar_persisted_on_execute(self, tmp_path: Path) -> None:
        """A fresh run writes extraction.yaml with the stream inventory
        (info slices + source identity)."""
        _, _, work_dir, source = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), _audio_json(), _subtitle_json()),
        )
        data = yaml.safe_load((work_dir / "extraction.yaml").read_text(encoding="utf-8"))
        assert set(data) == {"source", "streams", "timestamps_path"}
        assert data["source"]["path"] == str(source)
        assert data["streams"]["video"]["fps_fraction"] == [24000, 1001]
        assert len(data["streams"]["audio"]) == 1
        assert data["streams"]["subtitles"][0]["track_id"] == 3

    def test_reuse_run_loads_sidecar_without_ffprobe(self, tmp_path: Path) -> None:
        """Bug prevented: the every-run ffprobe re-probe — a matching sidecar
        must be loaded instead."""
        _, _, work_dir, source = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )
        assert (work_dir / "extraction.yaml").exists()

        phase = _make_extraction_phase(work_dir, source)
        with (
            patch("pyqenc.phases.extraction._probe_streams_json") as probe_mock,
            patch("pyqenc.phases.extraction.log_disk_space_info"),
        ):
            result = phase.run(dry_run=True)

        probe_mock.assert_not_called()
        assert result.video_stream is not None
        assert result.video_stream.info.codec_name == "hevc"
        assert len(result.audio_streams) == 1

    def test_identity_mismatch_reenumerates(self, tmp_path: Path) -> None:
        """A sidecar recorded for a different source identity is ignored and
        the source is re-enumerated (Req 2.8)."""
        _, _, work_dir, source = _run_and_capture(
            tmp_path, _ffprobe_json(_video_json()),
        )
        # Corrupt the recorded identity.
        sidecar_path = work_dir / "extraction.yaml"
        data = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
        data["source"]["file_size_bytes"] = 999999
        write_yaml_atomic(sidecar_path, data)

        phase = _make_extraction_phase(work_dir, source)
        with (
            patch("pyqenc.phases.extraction._probe_streams_json",
                  return_value=_ffprobe_json(_video_json())) as probe_mock,
            patch("pyqenc.phases.extraction.log_disk_space_info"),
        ):
            phase.run(dry_run=True)

        assert probe_mock.called


# ---------------------------------------------------------------------------
# Timestamps artifact result plumbing
# ---------------------------------------------------------------------------

class TestTimestampsPathOnResult:
    """ExtractionPhaseResult.timestamps_path carries the value it is built with."""

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


class TestMergeFailsWithoutTimestamps:
    """The public run() result carries timestamps_path=None when no file exists."""

    def test_extraction_result_timestamps_path_none_when_artifact_absent(
        self, tmp_path: Path,
    ) -> None:
        work_dir, source = _make_work_and_source(tmp_path)
        phase = _make_extraction_phase(work_dir, source)

        with patch("pyqenc.phases.extraction._probe_streams_json",
                   return_value=_ffprobe_json(_video_json())):
            result = phase.run(dry_run=True)

        ts_artifacts = [a for a in result.artifacts if isinstance(a, TimestampArtifact)]
        assert len(ts_artifacts) == 1
        assert ts_artifacts[0].state == ArtifactState.ABSENT
        assert result.timestamps_path is None
