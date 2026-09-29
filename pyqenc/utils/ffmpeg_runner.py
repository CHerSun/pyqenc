"""Unified async ffmpeg runner for the pyqenc pipeline.

All ffmpeg subprocess calls go through this module with a structured
:class:`FFmpegRequest` — inputs with per-input seek windows and ``-map``
selectors, the output stage, an optional ``-filter_complex`` graph. The runner
owns the whole command: it composes the argv (progress flags, ``-y``, the
``.tmp``-then-rename protocol with an explicit muxer, ``-map_chapters -1``),
launches the subprocess, reads stdout and stderr concurrently via
``readline()`` (safe because ``-nostats`` eliminates all ``\\r`` from stderr),
parses structured progress blocks from stdout, and returns a clean
:class:`FFmpegRunResult`.

Callers optionally supply a :data:`ProgressCallback` for live progress updates;
:frame counts are read from ``FFmpegRunResult.frame_count``.
"""
# CHerSun 2026

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pyqenc.constants import (
    FFMPEG_ARG_DURATION,
    FFMPEG_ARG_FILTER_COMPLEX,
    FFMPEG_ARG_FORMAT,
    FFMPEG_ARG_INPUT,
    FFMPEG_ARG_MAP,
    FFMPEG_ARG_MAP_CHAPTERS,
    FFMPEG_ARG_SEEK,
    FFMPEG_ARG_YES,
    FFMPEG_CODEC_COPY,
    FFMPEG_EXECUTABLE,
    FFMPEG_MAP_CHAPTERS_DISABLED,
    FFMPEG_MAP_FIRST_VIDEO,
    FFMPEG_MUXER_MATROSKA,
    FFMPEG_NULL_MUXER,
    FFMPEG_NULL_SINK,
    STDERR_TAIL_LINES,
    TEMP_SUFFIX,
)
from pyqenc.utils.long_path import LongPath

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Live process registry — used by the SIGINT handler to kill ffmpeg on exit
# ---------------------------------------------------------------------------

_procs_lock: threading.Lock      = threading.Lock()
_live_procs: set[asyncio.subprocess.Process] = set()


def kill_all_ffmpeg() -> None:
    """Kill all currently running ffmpeg subprocesses.

    Called from the SIGINT handler before ``os._exit`` so that ffmpeg
    processes do not outlive the main process.  Safe to call from any thread.
    """
    with _procs_lock:
        procs = set(_live_procs)
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[int, float], None]
"""Signature: ``(frame: int, out_time_seconds: float) -> None``"""


@dataclass(frozen=True)
class FFmpegInput:
    """One ffmpeg input: a file plus its per-input window and ``-map`` selector.

    Attributes:
        path:             The input file. Kept as a path object so subprocess
                          launch can apply :class:`LongPath` semantics.
        selector:         ``-map`` target identifying the stream inside its
                          container (e.g. ``"0:2"``); ``None`` emits no ``-map``
                          for this input.
        start_seconds:    Input-side seek target emitted as ``-ss`` before
                          ``-i`` (cue-point seek + decode to the exact frame).
        duration_seconds: Window duration emitted as ``-t`` before ``-i``,
                          bounding the frames read from this input.
        pre_input_args:   Tokens emitted before the window flags and ``-i``
                          (e.g. ``-hwaccel`` / ``-init_hw_device`` setup).
    """

    path:             LongPath
    selector:         str | None          = None
    start_seconds:    float | None        = None
    duration_seconds: float | None        = None
    pre_input_args:   tuple[str, ...]     = ()


@dataclass(frozen=True)
class FFmpegRequest:
    """A structured ffmpeg invocation composed and owned by the runner.

    Attributes:
        inputs:         Every input with its window/selector; windows are fully
                        bound to their input, multi-input graphs (quality
                        measurement) just carry two entries.
        output_args:    The output stage — codec / ``-vf`` / muxer-agnostic
                        tokens — emitted after the maps and any filter graph.
        filter_complex: Optional multi-input ``-filter_complex`` graph.
        output:         Intended final output path; ``None`` runs a null-output
                        command (``-f null -``, no ``.tmp`` protocol).
        output_format:  ffmpeg ``-f`` muxer token for the ``.tmp`` output (the
                        ``.tmp`` extension hides the container hint); ``None``
                        uses the Matroska default.
    """

    inputs:         list[FFmpegInput]
    output_args:    tuple[str, ...]
    filter_complex: str | None        = None
    output:         LongPath | None   = None
    output_format:  str | None        = None


@dataclass
class FFmpegRunResult:
    """Result of an ffmpeg subprocess execution.

    Attributes:
        returncode:   Raw process exit code.
        success:      ``True`` when ``returncode == 0`` (and, for file outputs,
                      the ``.tmp`` file exists and is non-empty).
        stderr_lines: All non-empty lines from stderr (~20–60 with ``-nostats``).
        frame_count:  ``frame`` value from the final ``progress=end`` block on
                      stdout; ``None`` if no ``progress=end`` was seen (e.g.
                      ffmpeg was killed or ``-progress`` was not injected).
    """

    returncode:   int
    success:      bool
    stderr_lines: list[str]       = field(default_factory=list)
    frame_count:  int | None      = None


# ---------------------------------------------------------------------------
# Command composition
# ---------------------------------------------------------------------------

_PROGRESS_FLAGS: list[str] = ["-hide_banner", "-nostats", "-progress", "pipe:1"]
"""Flags injected after the ffmpeg executable in every composed command."""


def _format_seconds(value: float) -> str:
    """Format a window bound for ``-ss``/``-t``.

    Plain ``str(float)`` reproduces the pre-request formatting at every
    converted call site (seek-target flooring to microseconds is a
    direct-from-source wiring decision and lands with that spec task).
    """
    return str(value)


def _launch_argv(
    request: FFmpegRequest,
) -> tuple[list[str | os.PathLike], tuple[Path, Path] | None]:
    """Compose the exact argv ffmpeg is launched with.

    Layout, in order: progress flags and ``-y``; per input
    ``pre_input_args [-ss start] [-t duration] -i path``; ``-map <selector>``
    per input in order; optional ``-filter_complex``; ``output_args``;
    ``-map_chapters -1``; the output stage — a ``<stem>.tmp`` sibling with an
    explicit ``-f <muxer>`` (returned as the tmp→final pair, renamed by the
    runner on success) or ``-f null -`` when ``output`` is ``None``.

    Args:
        request: The structured invocation.

    Returns:
        ``(argv, tmp_to_final)`` — the launch argv and, for file outputs, the
        ``(tmp_path, final_path)`` pair driving the rename protocol.
    """
    argv: list[str | os.PathLike] = [FFMPEG_EXECUTABLE, *_PROGRESS_FLAGS, FFMPEG_ARG_YES]

    for inp in request.inputs:
        argv.extend(inp.pre_input_args)
        if inp.start_seconds is not None:
            argv.extend([FFMPEG_ARG_SEEK, _format_seconds(inp.start_seconds)])
        if inp.duration_seconds is not None:
            argv.extend([FFMPEG_ARG_DURATION, _format_seconds(inp.duration_seconds)])
        argv.extend([FFMPEG_ARG_INPUT, inp.path])

    for inp in request.inputs:
        if inp.selector is not None:
            argv.extend([FFMPEG_ARG_MAP, inp.selector])

    if request.filter_complex is not None:
        argv.extend([FFMPEG_ARG_FILTER_COMPLEX, request.filter_complex])

    argv.extend(request.output_args)
    argv.extend([FFMPEG_ARG_MAP_CHAPTERS, FFMPEG_MAP_CHAPTERS_DISABLED])

    if request.output is None:
        argv.extend([FFMPEG_ARG_FORMAT, FFMPEG_NULL_MUXER, FFMPEG_NULL_SINK])
        return argv, None

    output = request.output
    tmp    = output.parent / f"{output.stem}{TEMP_SUFFIX}"
    muxer  = request.output_format if request.output_format is not None else FFMPEG_MUXER_MATROSKA
    argv.extend([FFMPEG_ARG_FORMAT, muxer, tmp])
    return argv, (tmp, output)


def compose_command(request: FFmpegRequest) -> list[str | os.PathLike]:
    """Return the exact argv ffmpeg is launched with for ``request``.

    The single argv authority: the runner launches this composition and the
    golden command tests pin it, so a request's meaning is inspectable without
    running ffmpeg.
    """
    argv, _tmp_to_final = _launch_argv(request)
    return argv


# ---------------------------------------------------------------------------
# Stdout / stderr readers
# ---------------------------------------------------------------------------

async def _read_stdout(
    stdout:   asyncio.StreamReader,
    callback: ProgressCallback | None,
) -> int | None:
    """Read ffmpeg stdout line-by-line and parse ``-progress pipe:1`` blocks.

    Accumulates ``key=value`` pairs into a dict representing the current
    progress block.  When ``progress=continue`` or ``progress=end`` is
    encountered the block is dispatched to ``callback`` (if provided) and the
    accumulator is cleared.

    Args:
        stdout:   Async stream reader attached to the subprocess stdout pipe.
        callback: Optional callable invoked as ``callback(frame, out_time_s)``
                  once per completed progress block.

    Returns:
        The ``frame`` value from the last ``progress=end`` block (total output
        frame count), or ``None`` if no ``progress=end`` was seen.
    """
    block:       dict[str, str] = {}
    final_frame: int | None     = None

    while True:
        raw = await stdout.readline()
        if not raw:
            break
        line = raw.decode(errors="replace").rstrip("\n").rstrip("\r")
        if not line:
            continue

        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key   = key.strip()
        value = value.strip()
        block[key] = value

        if key == "progress" and value in ("continue", "end"):
            # Extract frame and out_time_us from the completed block
            frame_str      = block.get("frame", "")
            out_time_str   = block.get("out_time_us", "")

            frame: int       = 0
            out_time_s: float = 0.0

            try:
                frame = int(frame_str)
            except (ValueError, TypeError):
                pass

            try:
                out_time_s = int(out_time_str) / 1_000_000.0
            except (ValueError, TypeError):
                pass

            if value == "end":
                final_frame = frame

            if callback is not None:
                try:
                    callback(frame, out_time_s)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("progress_callback raised (ignored): %s", exc)

            block = {}

    return final_frame


async def _read_stderr(stderr: asyncio.StreamReader) -> list[str]:
    """Read ffmpeg stderr and collect all non-empty lines.

    ffmpeg uses three different line endings depending on context:
    - ``\\r\\n`` — Windows-style (some header lines)
    - ``\\n``    — Unix-style (most header/summary lines)
    - ``\\r``    — in-place overwrite (VIF per-frame output, progress lines)

    ``readline()`` only splits on ``\\n``, so ``\\r``-only lines block until
    EOF.  Instead we read raw bytes in chunks and manually reconstruct lines
    from all three separators.

    Args:
        stderr: Async stream reader attached to the subprocess stderr pipe.

    Returns:
        List of all non-empty stripped lines from stderr.
    """
    _CHUNK = 4096
    lines:  list[str] = []
    buf:    bytes      = b""

    while True:
        chunk = await stderr.read(_CHUNK)
        if not chunk:
            break
        buf += chunk
        # Split on \r\n first (must come before \r to avoid double-splitting),
        # then \n, then \r — normalise to a single separator.
        normalised = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        # Keep the last (potentially incomplete) segment in the buffer.
        parts = normalised.split(b"\n")
        buf   = parts[-1]          # incomplete tail — wait for more bytes
        for part in parts[:-1]:
            line = part.decode(errors="replace").strip()
            if line:
                lines.append(line)

    # Flush whatever remains in the buffer after EOF
    if buf:
        line = buf.decode(errors="replace").strip()
        if line:
            lines.append(line)

    return lines


# ---------------------------------------------------------------------------
# Output finalization
# ---------------------------------------------------------------------------

def _finalize_output(
    tmp_to_final: tuple[Path, Path] | None,
    success:      bool,
) -> None:
    """Rename the temp file to its final name on success, or delete it on failure.

    Args:
        tmp_to_final: ``(tmp_path, final_path)`` pair, or ``None`` for null outputs.
        success:      Whether ffmpeg exited successfully with a non-empty output.
    """
    if tmp_to_final is None:
        return
    tmp_path, final_path = tmp_to_final
    if success:
        try:
            tmp_path.replace(final_path)
            logger.debug("Renamed %s → %s", tmp_path.name, final_path.name)
        except OSError:
            # Cross-device move — fall back to copy-then-delete
            logger.warning(
                "Cross-device rename for %s → %s; falling back to copy+delete",
                tmp_path.name, final_path.name,
            )
            shutil.copy2(tmp_path, final_path)
            tmp_path.unlink(missing_ok=True)
    else:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Could not delete temp file %s: %s", tmp_path, exc)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def run_ffmpeg_async(
    request:           FFmpegRequest,
    progress_callback: ProgressCallback | None = None,
    cwd:               Path | None             = None,
) -> FFmpegRunResult:
    """Run an ffmpeg request asynchronously with correct pipe handling.

    Composes the launch argv (progress flags, ``-y``, per-input windows and
    maps, ``-map_chapters -1``), launches the subprocess, reads stdout and
    stderr concurrently, and returns an :class:`FFmpegRunResult`.

    When ``request.output`` is set, the ``.tmp``-then-rename protocol applies:
    the output is substituted with a ``<stem>.tmp`` sibling plus an explicit
    ``-f <muxer>`` (``request.output_format``, default Matroska) before launch,
    then renamed to the final name on success or deleted on failure — a file
    at its final name is always the product of a complete, successful write.

    Args:
        request:           The structured ffmpeg invocation.
        progress_callback: Optional ``(frame, out_time_seconds)`` callable
                           invoked once per completed progress block.
        cwd:               Optional working directory for the subprocess.

    Returns:
        ``FFmpegRunResult`` with ``returncode``, ``success``, ``stderr_lines``,
        and ``frame_count``.
    """
    argv, tmp_to_final = _launch_argv(request)
    logger.debug("run_ffmpeg_async: %s", " ".join(str(a) for a in argv))

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    with _procs_lock:
        _live_procs.add(proc)
    try:
        frame_count, stderr_lines = await asyncio.gather(
            _read_stdout(proc.stdout, progress_callback),  # type: ignore[arg-type]
            _read_stderr(proc.stderr),                     # type: ignore[arg-type]
        )
        await proc.wait()
    finally:
        with _procs_lock:
            _live_procs.discard(proc)

    # Determine success: exit code 0 AND a non-empty temp output (if any)
    output_ok = (
        tmp_to_final[0].exists() and tmp_to_final[0].stat().st_size > 0
    ) if tmp_to_final is not None else True

    result = FFmpegRunResult(
        returncode   = proc.returncode,  # type: ignore[arg-type]
        success      = proc.returncode == 0 and output_ok,
        stderr_lines = stderr_lines,
        frame_count  = frame_count,
    )

    if not result.success:
        logger.error("ffmpeg exited with code %d", result.returncode)
        for line in result.stderr_lines[-STDERR_TAIL_LINES:]:
            logger.error("ffmpeg stderr: %s", line)

    _finalize_output(tmp_to_final, result.success)

    return result


def run_ffmpeg(
    request:           FFmpegRequest,
    progress_callback: ProgressCallback | None = None,
    cwd:               Path | None             = None,
) -> FFmpegRunResult:
    """Synchronous wrapper around ``run_ffmpeg_async``.

    Suitable for callers that are not in an async context.  Raises
    ``RuntimeError`` if called from within a running event loop — use
    ``run_ffmpeg_async`` instead in that case.

    Args:
        request:           The structured ffmpeg invocation.
        progress_callback: Optional ``(frame, out_time_seconds)`` callable.
        cwd:               Optional working directory for the subprocess.

    Returns:
        ``FFmpegRunResult``.

    Raises:
        RuntimeError: If called from within a running event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # No running loop — safe to call asyncio.run()
    else:
        raise RuntimeError(
            "run_ffmpeg() was called from within a running event loop. "
            "Use 'await run_ffmpeg_async(...)' instead."
        )

    return asyncio.run(
        run_ffmpeg_async(request, progress_callback, cwd)
    )


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------

class FrameCountError(Exception):
    """Raised when the frame count of a video file cannot be determined."""


def get_frame_count(video_file: Path) -> int:
    """Return the total frame count of ``video_file`` via an ffmpeg null-copy pass.

    Runs a null-output stream-copy pass with ``-progress pipe:1`` so the exact
    output frame count is read for free from the final ``progress=end`` block
    on stdout.

    Args:
        video_file: Path to the video file to count frames in.

    Returns:
        Total number of frames in the video.

    Raises:
        FrameCountError: If the frame count cannot be determined.
    """
    request = FFmpegRequest(
        inputs      = [FFmpegInput(path=video_file, selector=FFMPEG_MAP_FIRST_VIDEO)],
        output_args = ("-c", FFMPEG_CODEC_COPY),
    )
    result = run_ffmpeg(request)
    if result.frame_count is None:
        raise FrameCountError(
            f"Could not determine frame count for {video_file}. "
            f"ffmpeg exited with code {result.returncode}."
        )
    return result.frame_count
