"""Unit tests for pyqenc/utils/ffmpeg_runner.py.

Covers:
- _inject_flags: flags inserted after ffmpeg, idempotent if already present
- _read_stdout: progress blocks parsed, callback invoked, frame_count from progress=end
- run_ffmpeg: raises RuntimeError when called from a running event loop
- _resolve_tmp_paths: validates output paths in cmd, substitutes with .tmp siblings
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyqenc.utils.ffmpeg_runner import (
    _PROGRESS_FLAGS,
    _inject_flags,
    _read_stdout,
    _resolve_tmp_paths,
    run_ffmpeg,
)


# ---------------------------------------------------------------------------
# _inject_flags
# ---------------------------------------------------------------------------

class TestInjectFlags:
    def test_flags_inserted_after_ffmpeg(self) -> None:
        cmd = ["ffmpeg", "-i", "input.mkv", "-f", "null", "-"]
        result = _inject_flags(cmd)
        assert result[1:5] == _PROGRESS_FLAGS
        assert result[0] == "ffmpeg"
        assert result[5:] == ["-i", "input.mkv", "-f", "null", "-"]

    def test_idempotent_when_flags_already_present(self) -> None:
        cmd = ["ffmpeg"] + _PROGRESS_FLAGS + ["-i", "input.mkv"]
        result = _inject_flags(cmd)
        # Flags should not be duplicated
        assert result.count("-hide_banner") == 1
        assert result.count("-nostats") == 1
        assert result.count("-progress") == 1

    def test_original_list_not_mutated(self) -> None:
        cmd = ["ffmpeg", "-i", "input.mkv"]
        original = list(cmd)
        _inject_flags(cmd)
        assert cmd == original

    def test_ffmpeg_not_first_element(self) -> None:
        # e.g. a wrapper like ["nice", "-n", "10", "ffmpeg", "-i", "in.mkv"]
        cmd = ["nice", "-n", "10", "ffmpeg", "-i", "in.mkv"]
        result = _inject_flags(cmd)
        ffmpeg_idx = result.index("ffmpeg")
        assert result[ffmpeg_idx + 1 : ffmpeg_idx + 5] == _PROGRESS_FLAGS


# ---------------------------------------------------------------------------
# _read_stdout
# ---------------------------------------------------------------------------

def _make_stream(lines: list[str]) -> asyncio.StreamReader:
    """Build a mock StreamReader that yields the given lines then EOF."""
    reader = MagicMock(spec=asyncio.StreamReader)
    encoded = [line.encode() for line in lines] + [b""]
    reader.readline = MagicMock(side_effect=[asyncio.coroutine(lambda v=v: v)() for v in encoded])
    return reader


def _async_values(values: list[bytes]) -> MagicMock:
    """Return a mock whose readline() is an async function cycling through values."""
    idx = 0

    async def _readline() -> bytes:
        nonlocal idx
        val = values[idx]
        idx += 1
        return val

    reader = MagicMock(spec=asyncio.StreamReader)
    reader.readline = _readline
    return reader


class TestReadStdout:
    def _run(self, lines: list[str], callback=None) -> tuple[int | None, list[tuple]]:
        """Helper: run _read_stdout with given stdout lines, return (frame_count, calls)."""
        calls: list[tuple] = []

        def cb(frame: int, out_time_s: float) -> None:
            calls.append((frame, out_time_s))

        reader = _async_values([line.encode() + b"\n" for line in lines] + [b""])
        frame_count = asyncio.run(_read_stdout(reader, cb if callback is None else callback))
        return frame_count, calls

    def test_single_continue_block_invokes_callback(self) -> None:
        lines = [
            "frame=10",
            "out_time_us=333333",
            "fps=30",
            "progress=continue",
        ]
        frame_count, calls = self._run(lines)
        assert len(calls) == 1
        assert calls[0][0] == 10
        assert abs(calls[0][1] - 0.333333) < 1e-6
        assert frame_count is None  # no progress=end

    def test_progress_end_sets_frame_count(self) -> None:
        lines = [
            "frame=100",
            "out_time_us=3333333",
            "progress=end",
        ]
        frame_count, calls = self._run(lines)
        assert frame_count == 100
        assert len(calls) == 1

    def test_multiple_blocks_callback_called_per_block(self) -> None:
        lines = [
            "frame=5",  "out_time_us=100000", "progress=continue",
            "frame=10", "out_time_us=200000", "progress=continue",
            "frame=15", "out_time_us=300000", "progress=end",
        ]
        frame_count, calls = self._run(lines)
        assert len(calls) == 3
        assert [c[0] for c in calls] == [5, 10, 15]
        assert frame_count == 15

    def test_no_callback_still_returns_frame_count(self) -> None:
        lines = [
            "frame=42",
            "out_time_us=1000000",
            "progress=end",
        ]
        reader = _async_values([line.encode() + b"\n" for line in lines] + [b""])
        frame_count = asyncio.run(_read_stdout(reader, None))
        assert frame_count == 42

    def test_callback_exception_is_swallowed(self) -> None:
        def bad_cb(frame: int, out_time_s: float) -> None:
            raise ValueError("boom")

        lines = ["frame=1", "out_time_us=0", "progress=end"]
        reader = _async_values([line.encode() + b"\n" for line in lines] + [b""])
        # Should not raise
        frame_count = asyncio.run(_read_stdout(reader, bad_cb))
        assert frame_count == 1

    def test_empty_stream_returns_none(self) -> None:
        reader = _async_values([b""])
        frame_count = asyncio.run(_read_stdout(reader, None))
        assert frame_count is None


# ---------------------------------------------------------------------------
# run_ffmpeg event-loop guard
# ---------------------------------------------------------------------------

class TestRunFfmpegEventLoopGuard:
    def test_raises_runtime_error_inside_running_loop(self) -> None:
        async def _inner() -> None:
            with pytest.raises(RuntimeError, match="run_ffmpeg_async"):
                run_ffmpeg(["ffmpeg", "-version"], output_file=None)

        asyncio.run(_inner())


# ---------------------------------------------------------------------------
# _resolve_tmp_paths
# ---------------------------------------------------------------------------

class TestResolveTmpPaths:
    def test_single_output_substituted_with_tmp(self) -> None:
        out = Path("/tmp/output.mkv")
        cmd: list = ["ffmpeg", "-i", "input.mkv", str(out)]
        modified_cmd, tmp_to_final = _resolve_tmp_paths(cmd, out)
        # Final path replaced with .tmp sibling
        assert str(out) not in [str(a) for a in modified_cmd]
        tmp_path = out.parent / f"{out.stem}.tmp"
        assert str(tmp_path) in [str(a) for a in modified_cmd]
        assert tmp_to_final[tmp_path] == out

    def test_multiple_outputs_all_substituted(self) -> None:
        out1 = Path("/tmp/video.mkv")
        out2 = Path("/tmp/audio.mka")
        cmd: list = ["ffmpeg", "-i", "in.mkv", str(out1), str(out2)]
        modified_cmd, tmp_to_final = _resolve_tmp_paths(cmd, [out1, out2])
        cmd_strs = [str(a) for a in modified_cmd]
        assert str(out1) not in cmd_strs
        assert str(out2) not in cmd_strs
        assert len(tmp_to_final) == 2

    def test_raises_value_error_when_path_not_in_cmd(self) -> None:
        out = Path("/tmp/output.mkv")
        cmd: list = ["ffmpeg", "-i", "input.mkv", "/tmp/other.mkv"]
        with pytest.raises(ValueError, match="not found in ffmpeg cmd"):
            _resolve_tmp_paths(cmd, out)

    def test_tmp_stem_has_no_original_suffix(self) -> None:
        """Req 7.3: tmp file is <stem>.tmp, not <stem><original_suffix>.tmp."""
        out = Path("/tmp/chunk.1920x800.crf22.0.mkv")
        cmd: list = ["ffmpeg", "-i", "in.mkv", str(out)]
        _, tmp_to_final = _resolve_tmp_paths(cmd, out)
        tmp_path = list(tmp_to_final.keys())[0]
        assert tmp_path.name == "chunk.1920x800.crf22.0.tmp"
