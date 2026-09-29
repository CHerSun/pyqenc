"""Unit tests for mkvmerge integration in MergePhase.

Observable-behavior only: the two module-level pure helpers
(``_build_mkvmerge_options`` / ``_write_mkvmerge_options_file``) are exercised
directly as public functions, and the phase-level behaviors (options-file
lifecycle, timestamps-required guard) are driven through a REAL ``MergePhase``
built via its real constructor and a real phase registry whose Job / Extraction
/ Probe / Encoding / Audio dependencies carry pre-set COMPLETED typed results,
then run through the public ``merge.run(dry_run=False)`` entry point. Only the
external shell-outs are mocked: mkvmerge (``subprocess.run``) and the
frame-count check (``get_frame_count``) — boundaries, never phase internals. No
``__new__``, no private ``_execute`` / ``_collect_encoded_chunks`` calls,
no private-attr poking.

Covers:
- _build_mkvmerge_options: single chunk, multiple chunks, timestamps placement
- _default_duration_ns / _build_mkvpropedit_args: exact ns conversion, argv pin
- _write_mkvmerge_options_file: JSON written atomically
- Options file deleted on success, retained on failure (via run())
- mkvpropedit failure fails the strategy without writing a sidecar (via run())
- Merge fails with a clear message when timestamps_path is None / missing (via run())
"""

from __future__ import annotations

import json
import os
import tempfile
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock, patch

from pyqenc.app_config import load_app_config
from pyqenc.constants import EXTRACTED_DIR, FINAL_OUTPUT_DIR, TIMESTAMPS_FILENAME
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import (
    CleanupLevel,
    PhaseOutcome,
)
from pyqenc.phase import Artifact, PhaseRegistry
from pyqenc.phases.audio import AudioPhase, AudioPhaseResult
from pyqenc.phases.encoding import (
    EncodingPhase,
    EncodingPhaseResult,
)
from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.phases.merge import (
    MergePhase,
    _build_mkvmerge_options,
    _build_mkvpropedit_args,
    _default_duration_ns,
    _log_missed_targets_warning,
    _write_mkvmerge_options_file,
)
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState


def _extended_stream(path: Path, frame_count: int) -> "ExtendedVideoStream":
    """An ExtendedVideoStream for the source (fast facet + frame count)."""
    from fractions import Fraction

    from pyqenc.stream_model import (
        ExtendedVideoStream,
        File,
        VideoStream,
        VideoStreamInfo,
    )

    return ExtendedVideoStream(
        stream=VideoStream(
            file=File(path=path, file_size_bytes=64),
            info=VideoStreamInfo(
                track_id=0, codec_name="hevc", fps=24.0,
                fps_fraction=Fraction(24, 1), resolution="1920x1080",
                duration_seconds=3600.0,
            ),
        ),
        frame_count=frame_count,
        crop=__import__("pyqenc.models", fromlist=["CropParams"]).CropParams(),
    )

_APP_CONFIG = load_app_config(default_only=True)

# The single strategy under test and its filesystem-safe form.
_STRATEGY  = "slow+h265"
_SAFE_NAME = _STRATEGY.replace(":", "_")


# ---------------------------------------------------------------------------
# Real-construction helper
# ---------------------------------------------------------------------------

def _by_strategy_name(encoded) -> dict:
    """Key an EncodedChunk by its own strategy name (as encoding.py does)."""
    return {encoded.strategy.display_name(): encoded}


def _encoded_chunk(path: Path, chunk_id: str, strategy_name: str):
    """A minimal EncodedChunk fixture for merge consumption."""
    from decimal import Decimal

    from pyqenc.models import CodecConfig, CropParams, Strategy
    from pyqenc.stream_model import (
        EncodedChunk,
        ExtendedVideoStream,
        File,
        VideoStream,
        VideoStreamInfo,
    )

    codec = CodecConfig(
        name="h265-10bit", default_quality=Decimal("20"), default_preset="slow",
        quality_range=(Decimal("0"), Decimal("51")), presets=["slow"],
    )
    preset, _, profile = strategy_name.partition("+")
    strategy = Strategy(preset=preset, profile=profile or "h265", codec=codec, profile_args=[])
    return EncodedChunk(
        stream = ExtendedVideoStream(
            stream = VideoStream(
                file = File(path=path, file_size_bytes=path.stat().st_size if path.exists() else 64),
                info = VideoStreamInfo(track_id=0, resolution="1920x1080"),
            ),
            frame_count = 24,
            crop        = CropParams(),
        ),
        chunk    = _make_chunk_window(path.parent / "source.mkv", chunk_id),
        strategy = strategy,
        crf      = Decimal("20"),
    )


def _make_chunk_window(source, chunk_id):
    """A minimal VideoStreamChunk for the fixture."""
    from pyqenc.models import CropParams
    from pyqenc.stream_model import (
        ExtendedVideoStream,
        File,
        VideoStream,
        VideoStreamChunk,
        VideoStreamInfo,
    )

    start, end = 0.0, 1.0
    if "-" in chunk_id:
        try:
            from pyqenc.stream_model import VideoStreamChunk as _VSC
            bounds = _VSC.parse_chunk_id(chunk_id, ExtendedVideoStream(
                stream=VideoStream(file=File(path=source), info=VideoStreamInfo(track_id=0)),
                frame_count=24, crop=CropParams(),
            ))
            start, end = bounds.start_timestamp, bounds.end_timestamp
        except Exception:
            pass
    return VideoStreamChunk(
        stream = ExtendedVideoStream(
            stream = VideoStream(
                file = File(path=source),
                info = VideoStreamInfo(track_id=0, resolution="1920x1080"),
            ),
            frame_count = 24,
            crop        = CropParams(),
        ),
        start_timestamp = start,
        end_timestamp   = end,
        frame_count     = 24,
    )



def _make_merge_phase(
    work_dir:        Path,
    source:          Path,
    chunk:           Path,
    *,
    timestamps_path: Path | None,
    frame_count:     int = 100,
) -> MergePhase:
    """Build a REAL ``MergePhase`` via its real constructor and a real registry.

    Every dependency (Job / Extraction / Probe / Encoding / Audio) is a real
    phase instance whose public ``result`` is pre-set to a COMPLETED typed
    result, so the shared dependency walk is a no-op and ``merge.run()`` reaches
    its own recovery + merge work without any internal mocking. The encoding
    result carries a real COMPLETE ``EncodedArtifact`` for ``chunk`` so the real
    ``_collect_encoded_chunks`` reads it. ``resolved_targets`` is empty (default
    config) so quality measurement is skipped — no extra shell-out.

    Args:
        work_dir:        Pipeline work directory.
        source:          Source video path.
        chunk:           A real encoded chunk file on disk.
        timestamps_path: Timestamps file for the ExtractionPhase result; may be
                         ``None`` (or a missing path) to exercise the guard.
        frame_count:     Source frame count recorded by the ProbePhase result.
    """
    collector = NoOpMetricsCollector()
    config    = _APP_CONFIG.model_copy(deep=True)

    job = JobPhase(
        config, None,
        source     = source,
        work_dir   = work_dir,
        force      = False,
        cleanup    = CleanupLevel.NONE,
        no_metrics = True,
        collector  = collector,
    )
    job.result = JobPhaseResult(
        outcome    = PhaseOutcome.COMPLETED,
        artifacts  = [Artifact(path=work_dir / "job.yaml", state=ArtifactState.COMPLETE)],
        message    = "job complete",
        force_wipe = False,
        config     = config,
        work_dir   = work_dir,
        source     = source,
    )

    registry: PhaseRegistry = {JobPhase: job}

    extraction = ExtractionPhase(config, registry, video_required=True, collector=collector)
    ts_artifacts = (
        [Artifact(path=timestamps_path, state=ArtifactState.COMPLETE)]
        if timestamps_path is not None
        else []
    )
    extraction.result = ExtractionPhaseResult(
        outcome         = PhaseOutcome.COMPLETED,
        artifacts       = ts_artifacts,
        message         = "extraction complete",
        timestamps_path = timestamps_path,
    )
    registry[ExtractionPhase] = extraction

    probe = ProbePhase(config, registry, collector=collector, crop_params=None)
    probe.result = ProbePhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        artifacts = [Artifact(path=work_dir / "probe.yaml", state=ArtifactState.COMPLETE)],
        message   = "probe complete",
        stream    = _extended_stream(source, frame_count),
    )
    registry[ProbePhase] = probe

    encoding = EncodingPhase(config, registry, collector=collector)
    encoding.result = EncodingPhaseResult(
        outcome        = PhaseOutcome.COMPLETED,
        artifacts      = [],
        message        = "encoding complete",
        encoded_chunks = {
            "chunk1": _by_strategy_name(_encoded_chunk(chunk, "chunk1", _STRATEGY)),
        },
    )
    registry[EncodingPhase] = encoding

    audio = AudioPhase(config, registry, collector=collector)
    audio.result = AudioPhaseResult(
        outcome     = PhaseOutcome.COMPLETED,
        artifacts   = [],
        message     = "audio complete",
        audio_files = [],
    )
    registry[AudioPhase] = audio

    return MergePhase(config, registry, collector=collector)


# ---------------------------------------------------------------------------
# _build_mkvmerge_options
# ---------------------------------------------------------------------------

class TestBuildMkvmergeOptions:
    """_build_mkvmerge_options returns the correct argument list."""

    def test_single_chunk_no_plus_prefix(self, tmp_path: Path) -> None:
        """One chunk → no '+' prefix on the chunk path."""
        chunk   = tmp_path / "chunk1.mkv"
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options([chunk], output, ts_path)

        # The chunk path must appear without a '+' prefix
        assert str(chunk) in args
        assert f"+{chunk}" not in args

    def test_multiple_chunks_first_no_prefix(self, tmp_path: Path) -> None:
        """N chunks → first chunk has no '+' prefix."""
        chunks  = [tmp_path / f"chunk{i}.mkv" for i in range(3)]
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options(chunks, output, ts_path)

        assert str(chunks[0]) in args
        assert f"+{chunks[0]}" not in args

    def test_multiple_chunks_subsequent_have_plus_prefix(self, tmp_path: Path) -> None:
        """N chunks → all chunks after the first are preceded by '+'."""
        chunks  = [tmp_path / f"chunk{i}.mkv" for i in range(3)]
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options(chunks, output, ts_path)

        for chunk in chunks[1:]:
            assert f"+{os.fspath(chunk)}" in args, (
                f"Expected '+{chunk}' in args, got: {args}"
            )

    def test_output_flag_present(self, tmp_path: Path) -> None:
        """'-o' and the output path must be in the args."""
        chunk   = tmp_path / "chunk1.mkv"
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options([chunk], output, ts_path)

        assert "-o" in args
        o_index = args.index("-o")
        assert args[o_index + 1] == os.fspath(output)

    def test_timestamps_placement_before_first_chunk(self, tmp_path: Path) -> None:
        """'--timestamps 0:<path>' must appear before the first chunk."""
        chunks  = [tmp_path / f"chunk{i}.mkv" for i in range(2)]
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options(chunks, output, ts_path)

        assert "--timestamps" in args
        ts_index    = args.index("--timestamps")
        ts_value    = args[ts_index + 1]
        chunk0_index = args.index(os.fspath(chunks[0]))

        assert ts_value == f"0:{ts_path}", (
            f"Expected '0:{ts_path}', got {ts_value!r}"
        )
        assert ts_index < chunk0_index, (
            "--timestamps must appear before the first chunk"
        )

    def test_timestamps_not_applied_to_subsequent_chunks(self, tmp_path: Path) -> None:
        """'--timestamps' must appear exactly once (only for the first chunk)."""
        chunks  = [tmp_path / f"chunk{i}.mkv" for i in range(3)]
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options(chunks, output, ts_path)

        assert args.count("--timestamps") == 1, (
            f"Expected exactly 1 '--timestamps', got {args.count('--timestamps')}"
        )

    def test_no_default_duration_flag(self, tmp_path: Path) -> None:
        """Bug guarded: ``--default-duration`` is an mkvmerge *input-track*
        reinterpretation option — it never reaches the output header, so its
        presence would imply a false guarantee while mkvmerge keeps deriving
        DefaultDuration from the ms-rounded restored timestamps. The header is
        restored by the post-merge mkvpropedit step instead."""
        chunk   = tmp_path / "chunk1.mkv"
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options([chunk], output, ts_path)

        assert "--default-duration" not in args

    def test_returns_list_of_strings(self, tmp_path: Path) -> None:
        """Return type must be list[str]."""
        chunk   = tmp_path / "chunk1.mkv"
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options([chunk], output, ts_path)

        assert isinstance(args, list)
        assert all(isinstance(a, str) for a in args)


# ---------------------------------------------------------------------------
# _default_duration_ns / _build_mkvpropedit_args
# ---------------------------------------------------------------------------

class TestDefaultDurationNs:
    """fps → nanosecond DefaultDuration conversion is exact at NTSC rates."""

    def test_ntsc_rate_rounds_exactly(self) -> None:
        """Bug guarded: float math drifts at NTSC rates — the exact rational
        path must produce the canonical 24000/1001 duration of 41 708 333 ns
        (the value the source container itself carries)."""
        assert _default_duration_ns(Fraction(24000, 1001)) == 41_708_333

    def test_integer_rate(self) -> None:
        """24 fps → 1e9/24 ns rounded to the nearest integer."""
        assert _default_duration_ns(Fraction(24, 1)) == 41_666_667

    def test_common_rates_stay_exact(self) -> None:
        """25/50/60 fps divide 1e9 exactly — no rounding may occur."""
        for fps, expected in ((Fraction(25), 40_000_000),
                              (Fraction(50), 20_000_000),
                              (Fraction(60), 16_666_667)):
            assert _default_duration_ns(fps) == expected


class TestBuildMkvpropeditArgs:
    """The post-merge header patch command shape."""

    def test_full_command_pinned(self, tmp_path: Path) -> None:
        """Bug guarded: mkvpropedit rejects suffixed values ('41708333ns' is
        not an unsigned integer) — the value must be a bare integer, applied
        to the first video track of the merged file. The output is passed as
        a path-like (str(Path) in command building is forbidden — it can drop
        the extended-length prefix)."""
        output = tmp_path / "output.mkv"

        args = _build_mkvpropedit_args(output, Fraction(24000, 1001))

        assert args == [
            "mkvpropedit", output,
            "--edit", "track:v1",
            "--set", "default-duration=41708333",
        ]
        assert all(isinstance(a, (str, Path)) for a in args)


# ---------------------------------------------------------------------------
# _write_mkvmerge_options_file
# ---------------------------------------------------------------------------

class TestWriteMkvmergeOptionsFile:
    """_write_mkvmerge_options_file writes a valid JSON array atomically."""

    def test_file_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "options.json"
        _write_mkvmerge_options_file(path, ["-o", "out.mkv", "chunk.mkv"])
        assert path.exists()

    def test_content_is_valid_json_array(self, tmp_path: Path) -> None:
        args = ["-o", "out.mkv", "--timestamps", "0:/ts.txt", "chunk.mkv"]
        path = tmp_path / "options.json"
        _write_mkvmerge_options_file(path, args)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == args

    def test_tmp_file_not_left_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "options.json"
        _write_mkvmerge_options_file(path, ["-o", "out.mkv"])

        tmp_file = tmp_path / "options.tmp"
        assert not tmp_file.exists()

    def test_unicode_paths_preserved(self, tmp_path: Path) -> None:
        """Non-ASCII characters in paths must be preserved (ensure_ascii=False)."""
        unicode_path = "/path/to/movie.mkv"
        args = ["-o", unicode_path]
        path = tmp_path / "options.json"
        _write_mkvmerge_options_file(path, args)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded[1] == unicode_path


# ---------------------------------------------------------------------------
# Options-file lifecycle (driven through run())
# ---------------------------------------------------------------------------

class TestMkvmergeOptionsFileLifecycle:
    """The concat options file is deleted on success and retained on failure.

    Driven through the public ``merge.run(dry_run=False)`` surface against a
    real MergePhase; only the external shell-outs (mkvmerge + mkvpropedit via
    ``subprocess.run``) and ``get_frame_count`` are mocked.
    """

    def test_options_file_deleted_on_success(self) -> None:
        """Bug guarded: a leftover ``concat_*.json`` after a SUCCESSFUL merge
        would pollute ``final/`` and mislead recovery into thinking a merge is
        mid-flight. On success the options file must be gone.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            work_dir = tmp_path / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            source = tmp_path / "source.mkv"
            source.write_bytes(b"\x00" * 64)

            ts_file = work_dir / EXTRACTED_DIR / TIMESTAMPS_FILENAME
            ts_file.parent.mkdir(parents=True, exist_ok=True)
            ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

            chunk = work_dir / "chunk1.mkv"
            chunk.write_bytes(b"\x00" * 64)

            merge = _make_merge_phase(work_dir, source, chunk, timestamps_path=ts_file)

            final_dir    = work_dir / FINAL_OUTPUT_DIR
            output_file  = final_dir / f"{source.stem} {_SAFE_NAME}.mkv"
            options_file = final_dir / f"concat_{_SAFE_NAME}.json"

            def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
                if cmd[0] == "mkvpropedit":
                    # Header patch runs after the options file is cleaned up.
                    assert output_file.exists(), "Output file must exist when mkvpropedit is called"
                    result = MagicMock()
                    result.returncode = 0
                    result.stderr = ""
                    return result
                # Options file must exist at the moment mkvmerge is invoked.
                assert options_file.exists(), "Options file must exist when mkvmerge is called"
                output_file.write_bytes(b"\x00" * 128)
                result = MagicMock()
                result.returncode = 0
                result.stderr = ""
                return result

            with (
                patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run),
                patch("pyqenc.phases.merge.get_frame_count", return_value=100),
            ):
                result = merge.run(dry_run=False)

            assert result.outcome == PhaseOutcome.COMPLETED, (
                f"Expected COMPLETED, got {result.outcome} (error={result.error!r})"
            )
            assert not options_file.exists(), (
                "Options file must be deleted after a successful merge"
            )

    def test_options_file_retained_on_failure(self) -> None:
        """Bug guarded: discarding the ``concat_*.json`` when mkvmerge FAILS
        would destroy the exact argument list needed to reproduce/debug the
        failure. On failure the options file must remain on disk.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            work_dir = tmp_path / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            source = tmp_path / "source.mkv"
            source.write_bytes(b"\x00" * 64)

            ts_file = work_dir / EXTRACTED_DIR / TIMESTAMPS_FILENAME
            ts_file.parent.mkdir(parents=True, exist_ok=True)
            ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

            chunk = work_dir / "chunk1.mkv"
            chunk.write_bytes(b"\x00" * 64)

            merge = _make_merge_phase(work_dir, source, chunk, timestamps_path=ts_file)

            final_dir    = work_dir / FINAL_OUTPUT_DIR
            options_file = final_dir / f"concat_{_SAFE_NAME}.json"

            def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
                result = MagicMock()
                result.returncode = 1
                result.stderr = "error: something went wrong"
                return result

            with patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run):
                result = merge.run(dry_run=False)

            assert result.outcome == PhaseOutcome.FAILED, (
                f"Expected FAILED, got {result.outcome}"
            )
            assert options_file.exists(), (
                "Options file must be retained after a failed merge"
            )

    def test_propedit_failure_fails_strategy(self) -> None:
        """Bug guarded: silently swallowing a mkvpropedit failure would
        deliver a final whose header misdeclares the frame rate and reads as
        VFR — the exact defect the patch step exists to fix. A non-zero exit
        must fail the strategy merge (no sidecar → output stays PARTIAL for
        recovery to re-merge).
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            work_dir = tmp_path / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            source = tmp_path / "source.mkv"
            source.write_bytes(b"\x00" * 64)

            ts_file = work_dir / EXTRACTED_DIR / TIMESTAMPS_FILENAME
            ts_file.parent.mkdir(parents=True, exist_ok=True)
            ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

            chunk = work_dir / "chunk1.mkv"
            chunk.write_bytes(b"\x00" * 64)

            merge = _make_merge_phase(work_dir, source, chunk, timestamps_path=ts_file)

            final_dir   = work_dir / FINAL_OUTPUT_DIR
            output_file = final_dir / f"{source.stem} {_SAFE_NAME}.mkv"

            def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
                result = MagicMock()
                if cmd[0] == "mkvpropedit":
                    result.returncode = 2
                    result.stderr = "Error: The changes could not be written."
                else:
                    output_file.write_bytes(b"\x00" * 128)
                    result.returncode = 0
                    result.stderr = ""
                return result

            with patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run):
                result = merge.run(dry_run=False)

            assert result.outcome == PhaseOutcome.FAILED, (
                f"Expected FAILED, got {result.outcome}"
            )
            sidecar = output_file.with_suffix(".yaml")
            assert output_file.exists(), "Concatenated output stays on disk for debugging"
            assert not sidecar.exists(), (
                "No sidecar may be written when the header patch fails — "
                "the artifact must stay PARTIAL so recovery re-merges"
            )


# ---------------------------------------------------------------------------
# Timestamps required (driven through run())
# ---------------------------------------------------------------------------

class TestMergeFailsWithoutTimestamps:
    """When ``timestamps_path`` is None or points to a missing file, the merge
    must fail rather than silently producing output without PTS restoration.

    Driven through the public ``merge.run(dry_run=False)`` surface.
    """

    def test_merge_fails_when_timestamps_path_is_none(self) -> None:
        """Bug guarded: merging without timestamps would drop PTS restoration,
        breaking source-fidelity for VFR content. A ``None`` timestamps path
        must yield a FAILED result.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            work_dir = tmp_path / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            source = tmp_path / "source.mkv"
            source.write_bytes(b"\x00" * 64)

            chunk = work_dir / "chunk1.mkv"
            chunk.write_bytes(b"\x00" * 64)

            merge = _make_merge_phase(work_dir, source, chunk, timestamps_path=None)

            with patch("pyqenc.phases.merge.subprocess.run") as mock_run:
                result = merge.run(dry_run=False)
                mock_run.assert_not_called()

            assert result.outcome == PhaseOutcome.FAILED, (
                f"Expected FAILED when timestamps_path is None, got {result.outcome}"
            )

    def test_merge_fails_when_timestamps_file_missing(self) -> None:
        """Bug guarded: a stale timestamps path that no longer exists on disk
        must not be trusted — merging would fail at the mkvmerge boundary or
        drop PTS. A missing timestamps file must yield a FAILED result.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            work_dir = tmp_path / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            source = tmp_path / "source.mkv"
            source.write_bytes(b"\x00" * 64)

            # Path that does NOT exist on disk.
            missing_ts = work_dir / EXTRACTED_DIR / TIMESTAMPS_FILENAME

            chunk = work_dir / "chunk1.mkv"
            chunk.write_bytes(b"\x00" * 64)

            merge = _make_merge_phase(work_dir, source, chunk, timestamps_path=missing_ts)

            with patch("pyqenc.phases.merge.subprocess.run") as mock_run:
                result = merge.run(dry_run=False)
                mock_run.assert_not_called()

            assert result.outcome == PhaseOutcome.FAILED, (
                f"Expected FAILED when timestamps file is missing, got {result.outcome}"
            )

    def test_merge_fails_message_mentions_failure(self) -> None:
        """Bug guarded: a silent/empty failure message would leave the user with
        no signal about WHY the merge failed. The failure result must carry a
        message or error mentioning the failure/timestamps.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            work_dir = tmp_path / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            source = tmp_path / "source.mkv"
            source.write_bytes(b"\x00" * 64)

            chunk = work_dir / "chunk1.mkv"
            chunk.write_bytes(b"\x00" * 64)

            merge = _make_merge_phase(work_dir, source, chunk, timestamps_path=None)

            with patch("pyqenc.phases.merge.subprocess.run"):
                result = merge.run(dry_run=False)

            combined = f"{result.message} {result.error or ''}"
            assert "fail" in combined.lower() or "timestamps" in combined.lower(), (
                f"Expected failure message to mention 'fail' or 'timestamps', got: {combined!r}"
            )


# ---------------------------------------------------------------------------
# Missed-targets warning (completion-line escalation)
# ---------------------------------------------------------------------------

class TestMissedTargetsWarning:
    """A missed quality target escalates to a WARNING naming every miss.

    Bug guarded: the miss was previously signalled only by a ⚠ symbol on an
    INFO-level completion line — wrong level for a warning, and no
    wanted-vs-actual detail anywhere near the merge that produced it.
    """

    def test_warning_names_missed_metrics_with_both_values(self, caplog) -> None:
        """The warning is WARNING level, names the strategy and each missed
        metric with its measured AND target value; met metrics stay out."""
        import logging as _logging

        from pyqenc.models import QualityTarget

        targets = [
            QualityTarget(metric="vmaf", statistic="min", value=93.0),
            QualityTarget(metric="psnr", statistic="min", value=43.0),
        ]
        metrics = {"vmaf_min": 88.3, "psnr_min": 43.5}   # vmaf missed, psnr met

        with caplog.at_level(_logging.WARNING, logger="pyqenc.phases.merge"):
            _log_missed_targets_warning("ultrafast+h265", metrics, targets)

        warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
        assert len(warnings) == 1, "exactly one warning expected"
        msg = warnings[0].getMessage()
        assert "ultrafast+h265" in msg
        assert "vmaf-min" in msg and "88.3" in msg and "93.0" in msg
        assert "psnr-min" not in msg, "met metrics must not appear in the warning"

    def test_no_warning_when_all_targets_met(self, caplog) -> None:
        import logging as _logging

        from pyqenc.models import QualityTarget

        targets = [QualityTarget(metric="vmaf", statistic="min", value=93.0)]
        metrics = {"vmaf_min": 96.5}

        with caplog.at_level(_logging.WARNING, logger="pyqenc.phases.merge"):
            _log_missed_targets_warning("ultrafast+h265", metrics, targets)

        assert not [r for r in caplog.records if r.levelno == _logging.WARNING]
