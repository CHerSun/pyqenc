"""Unified async ffmpeg runner for the pyqenc pipeline.

This module provides a single async entry point for all ffmpeg subprocess
calls. It injects ``-hide_banner -nostats -progress pipe:1`` automatically,
reads stdout and stderr concurrently via ``readline()`` (safe because
``-nostats`` eliminates all ``\\r`` from stderr), parses structured progress
blocks from stdout, and returns a clean ``FFmpegRunResult``.

Callers optionally supply a ``ProgressCallback`` and/or a ``VideoMetadata``
instance to be populated in-place from the ffmpeg output.

The mandatory ``output_file`` parameter enforces the ``.tmp``-then-rename
protocol for all file-producing calls.  Pass ``None`` explicitly for
null-encode / metadata-probe commands that produce no file output.
"""
# CHerSun 2026

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import STDERR_TAIL_LINES, TEMP_SUFFIX

if TYPE_CHECKING:
    from pyqenc.models import VideoMetadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[int, float], None]
"""Signature: ``(frame: int, out_time_seconds: float) -> None``"""


@dataclass
class FFmpegRunResult:
    """Result of an ffmpeg subprocess execution.

    Attributes:
        returncode:   Raw process exit code.
        success:      ``True`` when ``returncode == 0``.
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
# Internal helpers
# ---------------------------------------------------------------------------

_PROGRESS_FLAGS: list[str] = ["-hide_banner", "-nostats", "-progress", "pipe:1"]
"""Flags injected after the ffmpeg executable by ``_inject_flags``."""


def _inject_flags(cmd: list[str | os.PathLike]) -> list[str | os.PathLike]:
    """Insert ``-hide_banner -nostats -progress pipe:1`` after the ffmpeg executable.

    The injection is idempotent — if all four flags are already present
    immediately after the executable the command is returned unchanged.

    Args:
        cmd: Full ffmpeg command including the executable as the first element.

    Returns:
        New list with the flags injected (original list is not mutated).
    """
    # Locate the ffmpeg executable (first element whose name contains "ffmpeg")
    ffmpeg_idx: int = 0
    for i, arg in enumerate(cmd):
        if "ffmpeg" in str(arg).lower():
            ffmpeg_idx = i
            break

    # Check whether all flags are already present right after the executable
    after = [str(a) for a in cmd[ffmpeg_idx + 1 : ffmpeg_idx + 1 + len(_PROGRESS_FLAGS)]]
    if after == _PROGRESS_FLAGS:
        return list(cmd)

    result: list[str | os.PathLike] = list(cmd[: ffmpeg_idx + 1])
    result.extend(_PROGRESS_FLAGS)
    result.extend(cmd[ffmpeg_idx + 1 :])
    return result


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
    """Read ffmpeg stderr line-by-line and collect all non-empty lines.

    With ``-nostats`` injected, stderr contains only the short header block
    (``Duration:``, ``Stream #0:0: Video:``, codec info) plus a one-line final
    summary — typically 20–60 lines, never large enough to be a memory concern.

    Args:
        stderr: Async stream reader attached to the subprocess stderr pipe.

    Returns:
        List of all non-empty stripped lines from stderr.
    """
    lines: list[str] = []
    while True:
        raw = await stderr.readline()
        if not raw:
            break
        line = raw.decode(errors="replace").strip()
        if line:
            lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _resolve_tmp_paths(
    cmd:         list[str | os.PathLike],
    output_file: Path | list[Path],
) -> tuple[list[str | os.PathLike], dict[Path, Path]]:
    """Validate output paths appear in cmd and substitute with ``.tmp`` siblings.

    Args:
        cmd:         Original ffmpeg command.
        output_file: Intended output path(s). Pass the same objects as in cmd.

    Returns:
        Tuple of ``(modified_cmd, tmp_to_final)`` where ``tmp_to_final`` maps
        each temp path back to its intended final path.

    Raises:
        ValueError: If any output path is not found in ``cmd``.
    """
    paths: list[Path] = [output_file] if isinstance(output_file, Path) else list(output_file)

    # Ensure that all given paths are present in the cmd. Just a safety check of dev intent.
    missing = set(paths) - set(cmd)
    if missing:
        raise ValueError(
            f"output_file path(s) {missing!r} not found in ffmpeg cmd. "
            "Ensure the exact Path object (or its str) is present in cmd."
        )

    # Build final→tmp mapping for substitution, then invert to tmp→final for the return value
    final_to_tmp: dict[Path, Path] = {p: p.parent / f"{p.stem}{TEMP_SUFFIX}" for p in paths}
    tmp_to_final: dict[Path, Path] = {tmp: Path(final) for final, tmp in final_to_tmp.items()}

    # Replace cmd args with temp files
    modified_cmd: list[str | os.PathLike] = []
    for arg in cmd:
        if arg in final_to_tmp:
            modified_cmd += ["-f", "matroska", final_to_tmp[arg]]
        else:
            modified_cmd.append(arg)

    return modified_cmd, tmp_to_final


def _finalize_outputs(tmp_to_final: dict[Path, Path], success: bool) -> None:
    """Rename temp files to final names on success, or delete them on failure.

    Args:
        tmp_to_final: Mapping of temp path → final path.
        success:      Whether ffmpeg exited successfully with non-empty outputs.
    """
    if success:
        for tmp_path, final_path in tmp_to_final.items():
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
        for tmp_path in tmp_to_final:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Could not delete temp file %s: %s", tmp_path, exc)


async def run_ffmpeg_async(
    cmd:               list[str | os.PathLike],
    output_file:       Path | list[Path] | None,
    progress_callback: ProgressCallback | None = None,
    video_meta:        VideoMetadata | None    = None,
    cwd:               Path | None             = None,
) -> FFmpegRunResult:
    """Run an ffmpeg command asynchronously with correct pipe handling.

    Injects ``-hide_banner -nostats -progress pipe:1`` into ``cmd``, launches
    the subprocess, reads stdout and stderr concurrently, and returns an
    ``FFmpegRunResult``.

    When ``output_file`` is a ``Path`` or ``list[Path]``, the runner enforces
    the ``.tmp``-then-rename protocol: each output path is substituted with a
    ``<stem>.tmp`` sibling before launching ffmpeg, then renamed to the final
    name on success or deleted on failure.

    Args:
        cmd:               Full ffmpeg command including the executable.
        output_file:       Intended output path(s), or ``None`` for commands
                           that produce no file output (null-encode, probing).
        progress_callback: Optional ``(frame, out_time_seconds)`` callable
                           invoked once per completed progress block.
        video_meta:        Optional ``VideoMetadata`` instance populated
                           in-place from stderr (and stdout frame count).
        cwd:               Optional working directory for the subprocess.

    Returns:
        ``FFmpegRunResult`` with ``returncode``, ``success``, ``stderr_lines``,
        and ``frame_count``.

    Raises:
        ValueError: If any path in ``output_file`` is not found in ``cmd``.
    """
    # Resolve .tmp substitutions before injecting progress flags
    tmp_to_final: dict[Path, Path] = {}
    if output_file is not None:
        cmd, tmp_to_final = _resolve_tmp_paths(list(cmd), output_file)

    modified_cmd = _inject_flags(list(cmd))
    logger.debug("run_ffmpeg_async: %s", " ".join(str(a) for a in modified_cmd))

    proc = await asyncio.create_subprocess_exec(
        *modified_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
    )

    frame_count, stderr_lines = await asyncio.gather(
        _read_stdout(proc.stdout, progress_callback),  # type: ignore[arg-type]
        _read_stderr(proc.stderr),                     # type: ignore[arg-type]
    )

    await proc.wait()

    # Determine success: exit code 0 AND all temp files non-empty
    all_outputs_ok = all(
        tmp_path.exists() and tmp_path.stat().st_size > 0
        for tmp_path in tmp_to_final
    ) if tmp_to_final else True

    result = FFmpegRunResult(
        returncode   = proc.returncode,  # type: ignore[arg-type]
        success      = proc.returncode == 0 and all_outputs_ok,
        stderr_lines = stderr_lines,
        frame_count  = frame_count,
    )

    if not result.success:
        logger.error("ffmpeg exited with code %d", result.returncode)
        for line in result.stderr_lines[-STDERR_TAIL_LINES:]:
            logger.error("ffmpeg stderr: %s", line)

    _finalize_outputs(tmp_to_final, result.success)

    if video_meta is not None:
        video_meta.populate_from_ffmpeg_output(stderr_lines)
        if frame_count is not None:
            # stdout progress=end value is authoritative over stderr-parsed value
            video_meta._frame_count = frame_count  # type: ignore[attr-defined]

    return result


def run_ffmpeg(
    cmd:               list[str | os.PathLike],
    output_file:       Path | list[Path] | None,
    progress_callback: ProgressCallback | None = None,
    video_meta:        VideoMetadata | None    = None,
    cwd:               Path | None             = None,
) -> FFmpegRunResult:
    """Synchronous wrapper around ``run_ffmpeg_async``.

    Suitable for callers that are not in an async context.  Raises
    ``RuntimeError`` if called from within a running event loop — use
    ``run_ffmpeg_async`` instead in that case.

    Args:
        cmd:               Full ffmpeg command including the executable.
        output_file:       Intended output path(s), or ``None`` for commands
                           that produce no file output (null-encode, probing).
        progress_callback: Optional ``(frame, out_time_seconds)`` callable.
        video_meta:        Optional ``VideoMetadata`` instance to populate.
        cwd:               Optional working directory for the subprocess.

    Returns:
        ``FFmpegRunResult``.

    Raises:
        RuntimeError: If called from within a running event loop.
        ValueError:   If any path in ``output_file`` is not found in ``cmd``.
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

    return asyncio.run(run_ffmpeg_async(cmd, output_file, progress_callback, video_meta, cwd))


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------

class FrameCountError(Exception):
    """Raised when the frame count of a video file cannot be determined."""


def get_frame_count(video_file: Path) -> int:
    """Return the total frame count of ``video_file`` via an ffmpeg null-copy pass.

    Uses ``ffmpeg -i <file> -map 0:v:0 -c copy -f null -`` with
    ``-progress pipe:1`` so the exact output frame count is read from the
    final ``progress=end`` block on stdout.

    Args:
        video_file: Path to the video file to count frames in.

    Returns:
        Total number of frames in the video.

    Raises:
        FrameCountError: If the frame count cannot be determined.
    """
    cmd: list[str | os.PathLike] = [
        "ffmpeg",
        "-i",    video_file,
        "-map",  "0:v:0",
        "-c",    "copy",
        "-f",    "null",
        "-",
    ]
    result = run_ffmpeg(cmd, output_file=None)
    if result.frame_count is None:
        raise FrameCountError(
            f"Could not determine frame count for {video_file}. "
            f"ffmpeg exited with code {result.returncode}."
        )
    return result.frame_count
