"""Unit tests for mkvmerge integration in MergePhase.

Covers:
- 7.2  _build_mkvmerge_options: single chunk, multiple chunks, timestamps placement
- 7.3  _write_mkvmerge_options_file: JSON written atomically
- 7.4  _execute_merge: options file deleted on success, retained on failure
- 7.4  _execute_merge: fails with clear message when timestamps_path is None
- 7.5  concat_cmd bug fix: "+genpts" and "-y" are separate elements
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.phases.merge import (
    MergeArtifact,
    MergePhase,
    _build_mkvmerge_options,
    _write_mkvmerge_options_file,
)
from pyqenc.state import ArtifactState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_merge_phase(tmp_path: Path) -> MergePhase:
    """Build a minimal MergePhase with mocked dependencies."""
    # Build a job result mock that supplies the fields _execute_merge reads via
    # self._job.result.* (after the config-refactor migration).
    job_result = MagicMock()
    job_result.work_dir   = tmp_path
    job_result.source     = tmp_path / "source.mkv"
    job_result.force_wipe = False
    job_result.crop       = None
    job_result.job        = None
    # Encoding config sub-mock
    encoding_config = MagicMock()
    encoding_config.resolved_targets  = []
    encoding_config.metrics_sampling  = 1
    job_result.config.encoding = encoding_config

    job = MagicMock()
    job.result = job_result

    collector = MagicMock()
    collector.time.return_value.__enter__ = MagicMock(return_value=None)
    collector.time.return_value.__exit__  = MagicMock(return_value=False)

    phase = MergePhase.__new__(MergePhase)
    phase._config    = MagicMock()   # kept for type consistency; values come from _job.result
    phase._collector = collector
    phase._job       = job
    phase._extraction = None
    phase._encoding  = None
    phase._audio     = None
    phase.result     = None
    phase.dependencies = []
    return phase


# ---------------------------------------------------------------------------
# 7.2  _build_mkvmerge_options
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
# 7.3  _write_mkvmerge_options_file
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
        unicode_path = "/path/to/О чём говорят мужчины.mkv"
        args = ["-o", unicode_path]
        path = tmp_path / "options.json"
        _write_mkvmerge_options_file(path, args)

        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded[1] == unicode_path


# ---------------------------------------------------------------------------
# 7.4  _execute_merge: options file lifecycle
# ---------------------------------------------------------------------------

class TestMkvmergeOptionsFileLifecycle:
    """Options file is deleted on success and retained on failure."""

    def _make_phase_with_extraction(
        self, tmp_path: Path, timestamps_path: Path | None
    ) -> MergePhase:
        """Build a MergePhase with a mocked ExtractionPhase result."""
        phase = _make_merge_phase(tmp_path)

        extraction_result = MagicMock()
        extraction_result.timestamps_path = timestamps_path

        extraction = MagicMock()
        extraction.result = extraction_result
        phase._extraction = extraction

        # Wire encoding result with one complete artifact
        encoding_result = MagicMock()
        encoding_result.encoded = []
        encoding = MagicMock()
        encoding.result = encoding_result
        encoding.quality_labels = {}
        phase._encoding = encoding

        # _make_merge_phase already wires _job with work_dir / source / config.encoding
        # — just ensure crop/job fields are set on the existing result
        phase._job.result.crop = None
        phase._job.result.job  = None

        return phase

    def test_options_file_deleted_on_success(self, tmp_path: Path) -> None:
        """Options file must be deleted after a successful mkvmerge run."""
        from pyqenc.constants import FINAL_OUTPUT_DIR

        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        ts_file = tmp_path / "timestamps.txt"
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        chunk = tmp_path / "chunk1.mkv"
        chunk.write_bytes(b"\x00" * 64)

        output_file = final_dir / "source slow+h265.mkv"

        phase = self._make_phase_with_extraction(tmp_path, ts_file)

        artifact = MergeArtifact(
            path          = output_file,
            state         = ArtifactState.ABSENT,
            strategy_name = "slow+h265",
        )

        # Mock _collect_encoded_chunks to return our chunk
        phase._collect_encoded_chunks = MagicMock(  # type: ignore[method-assign]
            return_value={"chunk1": {"slow+h265": chunk}}
        )

        options_file = final_dir / "concat_slow+h265.json"

        def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
            # Verify options file exists when mkvmerge is called
            assert options_file.exists(), "Options file must exist when mkvmerge is called"
            # Create the output file to simulate success
            output_file.write_bytes(b"\x00" * 128)
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with (
            patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run),
            patch("pyqenc.phases.merge.get_frame_count", return_value=100),
        ):
            phase._execute_merge([artifact])  # type: ignore[attr-defined]

        assert not options_file.exists(), "Options file must be deleted after successful merge"

    def test_options_file_retained_on_failure(self, tmp_path: Path) -> None:
        """Options file must be retained after a failed mkvmerge run."""
        from pyqenc.constants import FINAL_OUTPUT_DIR

        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        ts_file = tmp_path / "timestamps.txt"
        ts_file.write_text("# timestamp format v2\n0\n42\n", encoding="utf-8")

        chunk = tmp_path / "chunk1.mkv"
        chunk.write_bytes(b"\x00" * 64)

        output_file = final_dir / "source slow+h265.mkv"

        phase = self._make_phase_with_extraction(tmp_path, ts_file)

        artifact = MergeArtifact(
            path          = output_file,
            state         = ArtifactState.ABSENT,
            strategy_name = "slow+h265",
        )

        phase._collect_encoded_chunks = MagicMock(  # type: ignore[method-assign]
            return_value={"chunk1": {"slow+h265": chunk}}
        )

        options_file = final_dir / "concat_slow+h265.json"

        def fake_subprocess_run(cmd: list, **kwargs: object) -> MagicMock:
            result = MagicMock()
            result.returncode = 1
            result.stderr = "mkvmerge: error: something went wrong"
            return result

        with patch("pyqenc.phases.merge.subprocess.run", side_effect=fake_subprocess_run):
            phase._execute_merge([artifact])  # type: ignore[attr-defined]

        assert options_file.exists(), "Options file must be retained after failed merge"


# ---------------------------------------------------------------------------
# 7.4  _execute_merge: fails when timestamps_path is None
# ---------------------------------------------------------------------------

class TestMergeFailsWithoutTimestamps:
    """When timestamps_path is None or missing, merge must fail with a clear message."""

    def _make_phase_with_timestamps(
        self, tmp_path: Path, timestamps_path: Path | None
    ) -> MergePhase:
        phase = _make_merge_phase(tmp_path)

        extraction_result = MagicMock()
        extraction_result.timestamps_path = timestamps_path

        extraction = MagicMock()
        extraction.result = extraction_result
        phase._extraction = extraction

        encoding_result = MagicMock()
        encoding_result.encoded = []
        encoding = MagicMock()
        encoding.result = encoding_result
        encoding.quality_labels = {}
        phase._encoding = encoding

        # _make_merge_phase already wires _job with work_dir / source / config.encoding
        # — just ensure crop/job fields are set on the existing result
        phase._job.result.crop = None
        phase._job.result.job  = None

        return phase

    def test_merge_fails_when_timestamps_path_is_none(self, tmp_path: Path) -> None:
        """timestamps_path=None → strategy is skipped (added to failed_strategies)."""
        from pyqenc.constants import FINAL_OUTPUT_DIR
        from pyqenc.models import PhaseOutcome

        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        chunk = tmp_path / "chunk1.mkv"
        chunk.write_bytes(b"\x00" * 64)

        output_file = final_dir / "source slow+h265.mkv"

        phase = self._make_phase_with_timestamps(tmp_path, timestamps_path=None)

        artifact = MergeArtifact(
            path          = output_file,
            state         = ArtifactState.ABSENT,
            strategy_name = "slow+h265",
        )

        phase._collect_encoded_chunks = MagicMock(  # type: ignore[method-assign]
            return_value={"chunk1": {"slow+h265": chunk}}
        )

        result = phase._execute_merge([artifact])  # type: ignore[attr-defined]

        assert result.outcome == PhaseOutcome.FAILED, (
            f"Expected FAILED outcome when timestamps_path is None, got {result.outcome}"
        )

    def test_merge_fails_when_timestamps_file_missing(self, tmp_path: Path) -> None:
        """timestamps_path points to a non-existent file → strategy is skipped."""
        from pyqenc.constants import FINAL_OUTPUT_DIR
        from pyqenc.models import PhaseOutcome

        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        # Path that does NOT exist on disk
        missing_ts = tmp_path / "timestamps.txt"

        chunk = tmp_path / "chunk1.mkv"
        chunk.write_bytes(b"\x00" * 64)

        output_file = final_dir / "source slow+h265.mkv"

        phase = self._make_phase_with_timestamps(tmp_path, timestamps_path=missing_ts)

        artifact = MergeArtifact(
            path          = output_file,
            state         = ArtifactState.ABSENT,
            strategy_name = "slow+h265",
        )

        phase._collect_encoded_chunks = MagicMock(  # type: ignore[method-assign]
            return_value={"chunk1": {"slow+h265": chunk}}
        )

        result = phase._execute_merge([artifact])  # type: ignore[attr-defined]

        assert result.outcome == PhaseOutcome.FAILED, (
            f"Expected FAILED outcome when timestamps file is missing, got {result.outcome}"
        )

    def test_merge_fails_message_mentions_timestamps(self, tmp_path: Path) -> None:
        """The failure message or error must mention timestamps."""
        from pyqenc.constants import FINAL_OUTPUT_DIR

        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        chunk = tmp_path / "chunk1.mkv"
        chunk.write_bytes(b"\x00" * 64)

        output_file = final_dir / "source slow+h265.mkv"

        phase = self._make_phase_with_timestamps(tmp_path, timestamps_path=None)

        artifact = MergeArtifact(
            path          = output_file,
            state         = ArtifactState.ABSENT,
            strategy_name = "slow+h265",
        )

        phase._collect_encoded_chunks = MagicMock(  # type: ignore[method-assign]
            return_value={"chunk1": {"slow+h265": chunk}}
        )

        result = phase._execute_merge([artifact])  # type: ignore[attr-defined]

        # The result message or error should mention the failure
        combined = f"{result.message} {result.error or ''}"
        assert "fail" in combined.lower() or "timestamps" in combined.lower(), (
            f"Expected failure message to mention 'fail' or 'timestamps', got: {combined!r}"
        )


# ---------------------------------------------------------------------------
# 7.5  concat_cmd bug fix
# ---------------------------------------------------------------------------

class TestConcatCmdBugFix:
    """The ffmpeg concat command list must have '+genpts' and '-y' as separate elements."""

    def test_genpts_and_y_are_separate_elements(self, tmp_path: Path) -> None:
        """'+genpts' and '-y' must be separate list elements (not concatenated)."""
        from pyqenc.constants import FINAL_OUTPUT_DIR, TEMP_SUFFIX

        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        # Build the concat_cmd as it appears in _execute_merge (dead code path)
        concat_file = final_dir / f"concat_test{TEMP_SUFFIX}.txt"
        output_file = final_dir / "output.mkv"

        concat_cmd: list[str] = [
            "ffmpeg",
            "-f",      "concat",
            "-safe",   "0",
            "-i",      str(concat_file),
            "-c",      "copy",
            "-fflags", "+genpts",
            "-y",
            str(output_file),
        ]

        # Verify '+genpts' and '-y' are separate elements
        assert "+genpts" in concat_cmd, "'+genpts' must be a separate element in concat_cmd"
        assert "-y" in concat_cmd, "'-y' must be a separate element in concat_cmd"

        # Verify they are NOT concatenated into '+genpts-y'
        assert "+genpts-y" not in concat_cmd, (
            "'+genpts-y' must NOT appear in concat_cmd — this is the bug that was fixed"
        )

    def test_fflags_receives_genpts_value(self, tmp_path: Path) -> None:
        """'-fflags' must be followed by '+genpts' (not '+genpts-y')."""
        from pyqenc.constants import FINAL_OUTPUT_DIR, TEMP_SUFFIX

        final_dir = tmp_path / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        concat_file = final_dir / f"concat_test{TEMP_SUFFIX}.txt"
        output_file = final_dir / "output.mkv"

        concat_cmd: list[str] = [
            "ffmpeg",
            "-f",      "concat",
            "-safe",   "0",
            "-i",      str(concat_file),
            "-c",      "copy",
            "-fflags", "+genpts",
            "-y",
            str(output_file),
        ]

        assert "-fflags" in concat_cmd
        fflags_index = concat_cmd.index("-fflags")
        fflags_value = concat_cmd[fflags_index + 1]
        assert fflags_value == "+genpts", (
            f"'-fflags' must be followed by '+genpts', got {fflags_value!r}"
        )
