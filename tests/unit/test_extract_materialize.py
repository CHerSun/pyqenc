"""Materialization mode tests (2026-10-05 cli-intent-commands, Req 9).

The `extract` command's phase mechanics: pass-through video/audio streams as
presence-based, filter-driven material rows; remuxed to Matroska containers
(video ``.mkv``, audio ``.mka`` — any codec, timestamps kept) via a plain
ffmpeg stream copy; ``extraction.yaml`` recording the materialization facts;
later processing runs preserving the materialized files. Drives the public
``ExtractionPhase.run()`` exactly like ``test_extraction_pts`` — only genuine
external shell-outs are patched.
"""

# CHerSun 2026

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from pyqenc.constants import EXTRACTED_DIR, TEMP_SUFFIX, TIMESTAMPS_FILENAME
from pyqenc.models import PhaseOutcome
from pyqenc.utils.ffmpeg_runner import FFmpegRequest, compose_command
from tests.unit.test_extraction_pts import (
    _audio_json,
    _ffprobe_json,
    _make_extraction_phase,
    _make_work_and_source,
    _video_json,
)


def _fake_timestamps(*args: Path, **kwargs: object) -> None:
    """Stand-in for the patched-away timestamps extraction: write the index.

    The PTS index is the video row's standing material component — produced
    in materialization runs too — so the fake must materialize it for the
    row to complete.
    """
    args[2].write_bytes(b"0\n0\n")


def _run_materialize(
    tmp_path: Path,
    ffprobe_data: dict,
    *,
    exclude:      str | None = None,
    ffmpeg_ok:    bool       = True,
    video_required: bool     = False,
    materialize:  bool       = True,
):
    """Drive a real materialization-mode ExtractionPhase.run(dry_run=False).

    Returns ``(result, ffmpeg_cmds, subprocess_cmds, work_dir, source)``.
    The mkvextract fakes materialize their ``N:<file>`` pairs (chapters /
    attachments only — tracks remux through ffmpeg); with ``ffmpeg_ok=False``
    every ffmpeg call reports failure, surfacing the error path.
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
        if ffmpeg_ok and request.output is not None:
            request.output.parent.mkdir(parents=True, exist_ok=True)
            request.output.write_bytes(b"x" * 16)
        return MagicMock(success=ffmpeg_ok, returncode=0 if ffmpeg_ok else 1,
                         stderr_lines=[], frame_count=None)

    def fake_subprocess(cmd: list, **kwargs: object) -> MagicMock:
        subprocess_cmds.append(list(cmd))
        if str(cmd[0]) == "mkvextract":
            for keyword in ("tracks", "attachments"):
                if keyword in cmd:
                    for token in cmd[cmd.index(keyword) + 1:]:
                        Path(str(token).split(":", 1)[1]).write_bytes(b"x" * 16)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("pyqenc.phases.extraction._probe_streams_json", return_value=ffprobe_data),
        patch("pyqenc.phases.extraction.run_ffmpeg", side_effect=fake_run_ffmpeg),
        patch("pyqenc.phases.extraction._extract_timestamps", side_effect=_fake_timestamps),
        patch("subprocess.run", side_effect=fake_subprocess),
    ):
        result = phase.run(dry_run=False)

    return result, ffmpeg_cmds, subprocess_cmds, work_dir, source


class TestVideoRowComponents:
    """The video artifact's components are cumulative (user review 2026-10-05):
    the PTS index always; the container additionally in extract runs; neither
    when the row is not wanted."""

    def test_materialize_run_produces_both_components(self, tmp_path: Path) -> None:
        result, _, _, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )
        assert result.outcome is PhaseOutcome.COMPLETED
        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert TIMESTAMPS_FILENAME in names
        assert any(n.endswith(".mkv") for n in names)

    def test_processing_run_produces_only_the_index(self, tmp_path: Path) -> None:
        result, _, _, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
            video_required=True, materialize=False,
        )
        assert result.outcome is PhaseOutcome.COMPLETED
        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert names == {TIMESTAMPS_FILENAME}

    def test_unwanted_video_row_produces_neither(self, tmp_path: Path) -> None:
        result, _, _, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
            exclude="video",
        )
        assert result.outcome is PhaseOutcome.COMPLETED
        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert TIMESTAMPS_FILENAME not in names
        assert not any(n.endswith(".mkv") for n in names)

    def test_partial_row_completes_its_missing_component(self, tmp_path: Path) -> None:
        """A pre-existing index with a missing container (a PARTIAL row) is
        completed by producing only the container — the index producer is
        not re-run."""
        work_dir, source = _make_work_and_source(tmp_path)
        extracted = work_dir / EXTRACTED_DIR
        extracted.mkdir(parents=True)
        (extracted / TIMESTAMPS_FILENAME).write_bytes(b"0\n0\n")

        phase = _make_extraction_phase(
            work_dir, source, video_required=False, materialize=True,
        )
        ffmpeg_cmds: list[list[str]] = []
        index_calls: list[Path] = []

        def fake_index(*args: Path) -> None:
            index_calls.append(args[2])

        def fake_run_ffmpeg(request: FFmpegRequest, **kwargs: object) -> MagicMock:
            ffmpeg_cmds.append([str(a) for a in compose_command(request)[0]])
            if request.output is not None:
                request.output.write_bytes(b"x" * 16)
            return MagicMock(success=True, returncode=0, stderr_lines=[], frame_count=None)

        with (
            patch("pyqenc.phases.extraction._probe_streams_json",
                  return_value=_ffprobe_json(_video_json(), _audio_json())),
            patch("pyqenc.phases.extraction.run_ffmpeg", side_effect=fake_run_ffmpeg),
            patch("pyqenc.phases.extraction._extract_timestamps", side_effect=fake_index),
            patch("subprocess.run"),
        ):
            result = phase.run(dry_run=False)

        assert result.outcome is PhaseOutcome.COMPLETED
        assert index_calls == []  # the index component was already present
        assert any("copy" in c for c in ffmpeg_cmds)  # the container was produced


class TestTrackMaterialization:
    def test_tracks_remux_to_containers(self, tmp_path: Path) -> None:
        result, ffmpeg_cmds, subprocess_cmds, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )

        assert result.outcome is PhaseOutcome.COMPLETED
        # No mkvextract for tracks — containers come from ffmpeg stream copies.
        assert not any("tracks" in c for c in subprocess_cmds)
        track_copies = [c for c in ffmpeg_cmds if "-map" in c]
        assert len(track_copies) == 2  # video (track 0) + audio (track 1)
        assert any("0:0" in c and "copy" in c for c in track_copies)
        assert any("0:1" in c and "copy" in c for c in track_copies)
        # The atomic-write protocol: ffmpeg writes a .tmp sibling with an
        # explicit matroska muxer; the runner renames it to the final name
        # only on success (pinned here at the composed-argv level).
        for cmd in track_copies:
            assert "-f" in cmd and "matroska" in cmd
            assert any(a.endswith(TEMP_SUFFIX) for a in cmd)

        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert any(n.endswith(".mkv") for n in names)  # video container
        assert any(n.endswith(".mka") for n in names)  # audio container

    def test_sidecar_records_materialization_facts(self, tmp_path: Path) -> None:
        _, _, _, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )
        sidecar = yaml.safe_load(
            (work_dir / "extraction.yaml").read_text(encoding="utf-8"),
        )
        assert sidecar["streams"]["video"]["extracted_path"].endswith(".mkv")
        assert any(
            a.get("extracted_path", "").endswith(".mka")
            for a in sidecar["streams"]["audio"]
        )

    def test_exclude_video_leaves_only_audio(self, tmp_path: Path) -> None:
        result, ffmpeg_cmds, _, work_dir, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
            exclude="video",
        )

        assert result.outcome is PhaseOutcome.COMPLETED
        track_copies = [c for c in ffmpeg_cmds if "-map" in c]
        assert len(track_copies) == 1 and "0:1" in track_copies[0]  # audio only
        names = {f.name for f in (work_dir / EXTRACTED_DIR).iterdir()}
        assert not any(n.endswith(".mkv") for n in names)

    def test_ffmpeg_failure_fails_the_row(self, tmp_path: Path) -> None:
        result, _, _, _, _ = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
            ffmpeg_ok=False,
        )

        assert result.outcome is PhaseOutcome.FAILED
        assert result.message and "materializing track" in result.message


class TestProcessingPreservesMaterializedFiles:
    def test_processing_rerun_reuses_and_preserves(self, tmp_path: Path) -> None:
        _, _, _, work_dir, source = _run_materialize(
            tmp_path, _ffprobe_json(_video_json(), _audio_json()),
        )
        extracted = work_dir / EXTRACTED_DIR
        # The materialize run already produced the index (the video row's
        # standing component); the processing rerun takes the all-complete
        # fast exit over exactly this set.
        materialized = sorted(f.name for f in extracted.iterdir())
        assert TIMESTAMPS_FILENAME in materialized

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
        assert sorted(f.name for f in extracted.iterdir()) == materialized
