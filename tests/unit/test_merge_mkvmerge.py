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
``__new__``, no private ``_execute_merge`` / ``_collect_encoded_chunks`` calls,
no private-attr poking.

Covers:
- _build_mkvmerge_options: single chunk, multiple chunks, timestamps placement
- _write_mkvmerge_options_file: JSON written atomically
- Options file deleted on success, retained on failure (via run())
- Merge fails with a clear message when timestamps_path is None / missing (via run())
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from pyqenc.app_config import load_app_config
from pyqenc.constants import EXTRACTED_DIR, FINAL_OUTPUT_DIR, TIMESTAMPS_FILENAME
from pyqenc.metrics import NoOpMetricsCollector
from pyqenc.models import (
    CleanupLevel,
    ExtendedVideoMetadata,
    PhaseOutcome,
    VideoMetadata,
)
from pyqenc.phase import Artifact, Phase
from pyqenc.phases.audio import AudioPhase, AudioPhaseResult
from pyqenc.phases.encoding import (
    EncodedArtifact,
    EncodingPhase,
    EncodingPhaseResult,
)
from pyqenc.phases.extraction import ExtractionPhase, ExtractionPhaseResult
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.phases.merge import (
    MergePhase,
    _build_mkvmerge_options,
    _write_mkvmerge_options_file,
)
from pyqenc.phases.probe import ProbePhase, ProbePhaseResult
from pyqenc.state import ArtifactState, JobState

_APP_CONFIG = load_app_config(default_only=True)

# The single strategy under test and its filesystem-safe form.
_STRATEGY  = "slow+h265"
_SAFE_NAME = _STRATEGY.replace(":", "_")


# ---------------------------------------------------------------------------
# Real-construction helper
# ---------------------------------------------------------------------------

def _make_source_vm(path: Path) -> VideoMetadata:
    """Return a VideoMetadata with fast-probe fields pre-populated (no probing)."""
    meta = VideoMetadata(path=path)
    meta._duration_seconds = 3600.0
    meta._fps              = 24.0
    meta._resolution       = "1920x1080"
    return meta


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
    source_vm = _make_source_vm(source)

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
        job        = JobState(source=source_vm),
        force_wipe = False,
        config     = config,
        work_dir   = work_dir,
        source     = source,
    )

    registry: dict[type[Phase], Phase] = {JobPhase: job}

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
        video           = source_vm,
        timestamps_path = timestamps_path,
    )
    registry[ExtractionPhase] = extraction

    probe = ProbePhase(config, registry, collector=collector, crop_params=None)
    probe.result = ProbePhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        artifacts = [Artifact(path=work_dir / "probe.yaml", state=ArtifactState.COMPLETE)],
        message   = "probe complete",
        source    = ExtendedVideoMetadata.from_base(source_vm, frame_count=frame_count),
    )
    registry[ProbePhase] = probe

    encoding = EncodingPhase(config, registry, collector=collector)
    encoding.result = EncodingPhaseResult(
        outcome   = PhaseOutcome.COMPLETED,
        artifacts = [],
        message   = "encoding complete",
        encoded   = [EncodedArtifact(
            path     = chunk,
            state    = ArtifactState.COMPLETE,
            chunk_id = "chunk1",
            strategy = _STRATEGY,
        )],
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
            assert f"+{chunk}" in args, (
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
        assert args[o_index + 1] == str(output)

    def test_timestamps_placement_before_first_chunk(self, tmp_path: Path) -> None:
        """'--timestamps 0:<path>' must appear before the first chunk."""
        chunks  = [tmp_path / f"chunk{i}.mkv" for i in range(2)]
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options(chunks, output, ts_path)

        assert "--timestamps" in args
        ts_index    = args.index("--timestamps")
        ts_value    = args[ts_index + 1]
        chunk0_index = args.index(str(chunks[0]))

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

    def test_returns_list_of_strings(self, tmp_path: Path) -> None:
        """Return type must be list[str]."""
        chunk   = tmp_path / "chunk1.mkv"
        output  = tmp_path / "output.mkv"
        ts_path = tmp_path / "timestamps.txt"

        args = _build_mkvmerge_options([chunk], output, ts_path)

        assert isinstance(args, list)
        assert all(isinstance(a, str) for a in args)


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
    real MergePhase; only mkvmerge (``subprocess.run``) and ``get_frame_count``
    are mocked.
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
                result.stderr = "mkvmerge: error: something went wrong"
                return result

            with patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run):
                result = merge.run(dry_run=False)

            assert result.outcome == PhaseOutcome.FAILED, (
                f"Expected FAILED, got {result.outcome}"
            )
            assert options_file.exists(), (
                "Options file must be retained after a failed merge"
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
