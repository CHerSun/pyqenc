"""Materialization mode tests (2026-10-05 cli-intent-commands, Req 9).

The `extract` command's phase mechanics: pass-through video/audio streams as
presence-based, filter-driven material rows; one mkvextract ``tracks`` batch
with a per-track ffmpeg fallback; ``extraction.yaml`` recording the
materialization facts; later processing runs preserving the materialized
files. Drives the public ``ExtractionPhase.run()`` exactly like
``test_extraction_pts`` — only genuine external shell-outs are patched.
"""

# CHerSun 2026

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from pyqenc.constants import EXTRACTED_DIR, TIMESTAMPS_FILENAME
from pyqenc.models import PhaseOutcome
from pyqenc.stream_model import (
    AudioStream,
    AudioStreamInfo,
    VideoStream,
    VideoStreamInfo,
)
from pyqenc.utils.ffmpeg_runner import FFmpegRequest, compose_command
from tests.unit.test_extraction_pts import (
    _audio_json,
    _ffprobe_json,
    _make_extraction_phase,
    _make_work_and_source,
    _video_json,
)


def _extract_timestamps_noop(*args: object, **kwargs: object) -> None:
    """Stand-in for the patched-away timestamps extraction."""


def _run_materialize(
    tmp_path: Path,
    ffprobe_data: dict,
    *,
    exclude:        str | None = None,
    mkvextract_ok:  bool       = True,
    video_required: bool       = False,
    materialize:    bool       = True,
):
    """Drive a real materialization-mode ExtractionPhase.run(dry_run=False).

    Returns ``(result, ffmpeg_cmds, subprocess_cmds, work_dir, source)``.
    The mkvextract ``tracks``/``attachments`` fakes materialize their
    ``N:<file>`` pairs; with ``mkvextract_ok=False`` every mkvextract call
    fails, forcing the ffmpeg fallbacks.
    """
    work_dir, source = _make_work_and_source(tmp_path)
    phase = _make_extraction_phase(
        work_dir, source,
        exclude=exclude, video_required=video_required, materialize=materialize,
    )

    ffmpeg_cmds:     list[list[str]] = []
    subprocess_cmds: list[list[str]] = []

    def fake_run_ffmpeg(request: FFmpegRequest, **kwargs: object) -> MagicMock:
        ffmpeg_cmds.append([str(a) for a in compose_command(request)[0]])
        if request.output is not None:
            request.output.parent.mkdir(parents=True, exist_ok=True)
            request.output.write_bytes(b"x" * 16)
        return MagicMock(success=True, returncode=0, stderr_lines=[], frame_count=None)

    def fake_subprocess(cmd: list, **kwargs: object) -> MagicMock:
        subprocess_cmds.append(list(cmd))
        if str(cmd[0]) == "mkvextract":
            if not mkvextract_ok:
                raise subprocess.CalledProcessError(1, cmd)
            for keyword in ("tracks", "attachments"):
                if keyword in cmd:
                    for token in cmd[cmd.index(keyword) + 1:]:
                        Path(str(token).split(":", 1)[1]).write_bytes(b"x" * 16)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("pyqenc.phases.extraction._probe_streams_json", return_value=ffprobe_data),
        patch("pyqenc.phases.extraction.run_ffmpeg", side_effect=fake_run_ffmpeg),
        patch("pyqenc.phases.extraction._extract_timestamps", side_effect=_extract_timestamps_noop),
        patch("subprocess.run", side_effect=fake_subprocess),
    ):
        result = phase.run(dry_run=False)

    return result, ffmpeg_cmds, subprocess_cmds, work_dir, source


def _track_specs(tracks_cmd: list[str]) -> list[str]:
    """The ``N:<file>`` specs of a mkvextract tracks command."""
    return [str(t) for t in tracks_cmd[tracks_cmd.index("tracks") + 1:]]


class TestTrackMaterialization:
    def test_tracks_batch_materializes_video_and_audio(self, tmp_path: Path) -> None:
        result, _, subprocess_cmds, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )

        assert result.outcome is PhaseOutcome.COMPLETED
        tracks_cmds = [c for c in subprocess_cmds if "tracks" in c]
        assert len(tracks_cmds) == 1  # ONE batch for every absent track
        specs = _track_specs(tracks_cmds[0])
        assert len(specs) == 2  # video (track 0) + audio (track 1)
        assert specs[0].startswith("0:") and specs[1].startswith("1:")

        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert any(n.endswith(".h265") for n in names)  # hevc elementary
        assert any(n.endswith(".flac") for n in names)  # flac elementary

    def test_sidecar_records_materialization_facts(self, tmp_path: Path) -> None:
        _, _, _, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )
        sidecar = yaml.safe_load(
            (work_dir / "extraction.yaml").read_text(encoding="utf-8"),
        )
        assert sidecar["streams"]["video"]["extracted_path"].endswith(".h265")
        assert any(
            a.get("extracted_path", "").endswith(".flac")
            for a in sidecar["streams"]["audio"]
        )

    def test_exclude_video_leaves_only_audio_in_batch(self, tmp_path: Path) -> None:
        result, _, subprocess_cmds, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
            exclude="video",
        )

        assert result.outcome is PhaseOutcome.COMPLETED
        tracks_cmds = [c for c in subprocess_cmds if "tracks" in c]
        specs = _track_specs(tracks_cmds[0])
        assert len(specs) == 1 and specs[0].startswith("1:")  # audio only
        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert not any(n.endswith(".h265") for n in names)

    def test_mkvextract_failure_falls_back_to_per_track_ffmpeg(self, tmp_path: Path) -> None:
        result, ffmpeg_cmds, _, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
            mkvextract_ok=False,
        )

        assert result.outcome is PhaseOutcome.COMPLETED
        track_copies = [c for c in ffmpeg_cmds if "-map" in c]
        assert len(track_copies) == 2
        assert any("0:0" in c and "copy" in c for c in track_copies)
        assert any("0:1" in c for c in track_copies)
        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert any(n.endswith(".h265") for n in names)
        assert any(n.endswith(".flac") for n in names)


class TestProcessingPreservesMaterializedFiles:
    def test_processing_rerun_reuses_and_preserves(self, tmp_path: Path) -> None:
        _, _, _, work_dir, source = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )
        extracted = work_dir / EXTRACTED_DIR
        materialized = sorted(f.name for f in extracted.iterdir())
        # The processing run's wanted video component, present beforehand so
        # the rerun takes the all-complete fast exit.
        (extracted / TIMESTAMPS_FILENAME).write_bytes(b"0\n0\n")

        phase = _make_extraction_phase(work_dir, source)  # processing mode
        with (
            patch(
                "pyqenc.phases.extraction._probe_streams_json",
                side_effect=AssertionError("sidecar load must not re-probe"),
            ),
            patch("subprocess.run") as sp,
            patch("pyqenc.phases.extraction.run_ffmpeg") as ff,
        ):
            result = phase.run(dry_run=False)

        assert result.outcome is PhaseOutcome.REUSED
        sp.assert_not_called()
        ff.assert_not_called()
        # Materialized AV survives a processing run untouched.
        assert sorted(f.name for f in extracted.iterdir()) == [
            *materialized, TIMESTAMPS_FILENAME,
        ]


class TestTrackExtensions:
    """Codec-derived elementary-stream extensions (the naming substrate)."""

    @pytest.mark.parametrize(
        ("codec", "ext"),
        [
            ("h264", "h264"), ("hevc", "h265"), ("av1", "obu"),
            ("vp9", "ivf"), ("theora", "ogv"), ("mpeg2video", "m2v"),
        ],
    )
    def test_video_extensions(self, codec: str, ext: str) -> None:
        stream = VideoStream.model_construct(
            info=VideoStreamInfo.model_construct(codec_name=codec),
        )
        assert stream.file_extension == ext

    @pytest.mark.parametrize(
        ("codec", "ext"),
        [
            ("eac3", "eac3"), ("ac3", "ac3"), ("aac", "aac"),
            ("dts", "dts"), ("truehd", "thd"), ("flac", "flac"),
            ("opus", "opus"), ("pcm_s16le", "wav"),
        ],
    )
    def test_audio_extensions(self, codec: str, ext: str) -> None:
        stream = AudioStream.model_construct(
            info=AudioStreamInfo.model_construct(codec_name=codec),
        )
        assert stream.file_extension == ext

    def test_eac3_wins_over_ac3_substring(self) -> None:
        stream = AudioStream.model_construct(
            info=AudioStreamInfo.model_construct(codec_name="eac3"),
        )
        assert stream.file_extension == "eac3"

    def test_unknown_codec_raises(self) -> None:
        stream = AudioStream.model_construct(
            info=AudioStreamInfo.model_construct(codec_name="warpdrive"),
        )
        with pytest.raises(ValueError, match="Unknown audio codec"):
            _ = stream.file_extension
