"""Unit tests for pyqenc/utils/ffmpeg_runner.py.

Covers:
- compose_command: the golden composed argv — progress flags, ``-y``, per-input
  ``-ss``/``-t``/``-i`` ordering, ``-map`` per selector, ``-filter_complex``,
  ``output_args``, ``-map_chapters -1``, ``.tmp`` output with explicit muxer,
  null output
- get_frame_count: builds its request internally and raises on missing count
- _read_stdout: progress blocks parsed, callback invoked, frame_count from progress=end
- run_ffmpeg: raises RuntimeError when called from a running event loop
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pyqenc.utils.ffmpeg_runner import (
    _PROGRESS_FLAGS,
    FFmpegInput,
    FFmpegRequest,
    FFmpegRunResult,
    FrameCountError,
    _read_stdout,
    compose_command,
    get_frame_count,
    run_ffmpeg,
)


def _flat(argv: list) -> list[str]:
    """Flatten a composed argv (Path objects → plain strings) for comparison."""
    return [str(a) for a in argv]


# ---------------------------------------------------------------------------
# compose_command — golden argv
# ---------------------------------------------------------------------------

class TestComposeCommand:
    """Pin the exact argv layout the runner composes from a request.

    Bug prevented: any drift in flag ordering or injection (progress flags,
    ``-y``, ``-map_chapters -1``, muxer-before-``.tmp``) silently changing the
    command ffmpeg receives at a converted call site.
    """

    def test_full_featured_request(self) -> None:
        """Inputs carry pre-input args, window flags and paths in order;
        maps follow all inputs; then filter_complex, output_args, chapter guard,
        muxer + .tmp output."""
        request = FFmpegRequest(
            inputs = [
                FFmpegInput(
                    path           = Path("/src/a.mkv"),
                    selector       = "0:1",
                    start_seconds  = 1.5,
                    duration_seconds = 2.25,
                    pre_input_args = ("-hwaccel", "cuda"),
                ),
                FFmpegInput(path=Path("/src/b.mkv")),
            ],
            output_args    = ("-c:v", "libx264"),
            filter_complex = "[0:v][1:v]psnr",
            output         = Path("/out/x.mkv"),
        )
        assert _flat(compose_command(request)) == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-hwaccel", "cuda", "-ss", "1.5", "-t", "2.25", "-i", str(Path("/src/a.mkv")),
            "-i", str(Path("/src/b.mkv")),
            "-map", "0:1",
            "-filter_complex", "[0:v][1:v]psnr",
            "-c:v", "libx264",
            "-map_chapters", "-1",
            "-f", "matroska", str(Path("/out/x.tmp")),
        ]

    def test_null_output_terminates_command(self) -> None:
        """No output file → the command ends with the null muxer sink."""
        request = FFmpegRequest(
            inputs      = [FFmpegInput(path=Path("/v.mkv"), selector="0:v:0")],
            output_args = ("-c", "copy"),
        )
        assert _flat(compose_command(request))[-5:] == [
            "-c", "copy", "-map_chapters", "-1", "-f", "null", "-",
        ][-5:]

    def test_null_output_full_argv(self) -> None:
        request = FFmpegRequest(
            inputs      = [FFmpegInput(path=Path("/v.mkv"), selector="0:v:0")],
            output_args = ("-c", "copy"),
        )
        assert _flat(compose_command(request)) == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-i", str(Path("/v.mkv")),
            "-map", "0:v:0",
            "-c", "copy",
            "-map_chapters", "-1",
            "-f", "null", "-",
        ]

    def test_explicit_output_format_names_the_muxer(self) -> None:
        """Bug prevented: an audio/flac output silently muxed as Matroska."""
        request = FFmpegRequest(
            inputs       = [FFmpegInput(path=Path("/src/a.mka"), selector="0:a:0")],
            output_args  = ("-c:a", "flac"),
            output       = Path("/out/a.flac"),
            output_format = "flac",
        )
        argv = _flat(compose_command(request))
        assert argv[-3:] == ["-f", "flac", str(Path("/out/a.tmp"))]

    def test_tmp_output_is_stem_tmp_sibling(self) -> None:
        """Bug prevented: tmp named <stem><suffix>.tmp instead of <stem>.tmp,
        which would leak the container hint and break the rename step."""
        request = FFmpegRequest(
            inputs      = [FFmpegInput(path=Path("/src/a.mkv"))],
            output_args = ("-c", "copy"),
            output      = Path("/out/chunk.1920x800.q22.mkv"),
        )
        argv = _flat(compose_command(request))
        assert argv[-1] == str(Path("/out/chunk.1920x800.q22.tmp"))

    def test_zero_start_is_emitted_not_skipped(self) -> None:
        """A 0.0-second window start must still emit ``-ss 0.0`` — a window
        starting at zero is a real seek target, not "no window"."""
        request = FFmpegRequest(
            inputs      = [FFmpegInput(path=Path("/v.mkv"), start_seconds=0.0, duration_seconds=1.0)],
            output_args = (),
        )
        argv = _flat(compose_command(request))
        assert argv[6:10] == ["-ss", "0.0", "-t", "1.0"]

    def test_selector_only_inputs_get_maps_in_order(self) -> None:
        """Maps are emitted once per input, in input order; selector-less
        inputs contribute none."""
        request = FFmpegRequest(
            inputs = [
                FFmpegInput(path=Path("/a.mkv")),
                FFmpegInput(path=Path("/b.mkv"), selector="1:2"),
                FFmpegInput(path=Path("/c.mkv"), selector="2:0"),
            ],
            output_args = (),
        )
        argv = _flat(compose_command(request))
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        assert maps == ["1:2", "2:0"]

    def test_y_and_chapter_guard_always_present(self) -> None:
        """Every request carries ``-y`` (stale .tmp must never hang ffmpeg)
        and ``-map_chapters -1`` (no output ever inherits input chapters)."""
        request = FFmpegRequest(inputs=[FFmpegInput(path=Path("/v.mkv"))], output_args=())
        argv = _flat(compose_command(request))
        assert argv[1:6] == [*_PROGRESS_FLAGS, "-y"]
        assert "-map_chapters" in argv
        assert argv[argv.index("-map_chapters") + 1] == "-1"


# ---------------------------------------------------------------------------
# get_frame_count
# ---------------------------------------------------------------------------

class TestGetFrameCount:
    def test_returns_frame_count_from_result(self) -> None:
        result = FFmpegRunResult(returncode=0, success=True, frame_count=42)
        with patch("pyqenc.utils.ffmpeg_runner.run_ffmpeg", return_value=result) as mock_run:
            assert get_frame_count(Path("/v.mkv")) == 42
        request = mock_run.call_args[0][0]
        assert _flat(compose_command(request)) == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-i", str(Path("/v.mkv")),
            "-map", "0:v:0",
            "-c", "copy",
            "-map_chapters", "-1",
            "-f", "null", "-",
        ]

    def test_raises_frame_count_error_when_undetermined(self) -> None:
        """Bug prevented: an uncountable video silently returning a bogus 0."""
        result = FFmpegRunResult(returncode=1, success=False, frame_count=None)
        with (
            patch("pyqenc.utils.ffmpeg_runner.run_ffmpeg", return_value=result),
            pytest.raises(FrameCountError),
        ):
            get_frame_count(Path("/v.mkv"))


# ---------------------------------------------------------------------------
# _read_stdout
# ---------------------------------------------------------------------------

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
        request = FFmpegRequest(inputs=[FFmpegInput(path=Path("/v.mkv"))], output_args=())

        async def _inner() -> None:
            with pytest.raises(RuntimeError, match="run_ffmpeg_async"):
                run_ffmpeg(request)

        asyncio.run(_inner())
