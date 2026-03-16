"""FFmpeg wrapper with progress tracking and consistent output handling.

This module provides a wrapper around ffmpeg commands that:
- Adds required flags for progress reporting (-progress pipe:1)
- Prevents buffer overflow by properly handling stdout/stderr
- Yields progress updates during execution
- Returns final frame count and other metrics
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Generator

from pyqenc.constants import TIMEOUT_SECONDS_SHORT

if TYPE_CHECKING:
    from pyqenc.models import VideoMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FFmpegProgress:
    """Progress information from ffmpeg execution.

    Attributes:
        frame: Current frame number
        fps: Current frames per second
        time_us: Current time in microseconds
        time_seconds: Current time in seconds
        bitrate_kbps: Current bitrate in kbps
        speed: Processing speed multiplier (e.g., 2.5x)
    """

    frame: int = 0
    fps: float = 0.0
    time_us: int = 0
    time_seconds: float = 0.0
    bitrate_kbps: float = 0.0
    speed: float = 0.0

    @property
    def progress_percent(self) -> float | None:
        """Calculate progress percentage if duration is known."""
        return None  # Calculated externally based on expected duration


@dataclass(frozen=True, slots=True)
class FFmpegResult:
    """Result of ffmpeg execution.

    Attributes:
        success: Whether execution completed successfully
        returncode: Process return code
        final_progress: Final progress state
        stderr_tail: Last few lines of stderr for error reporting
        error_message: Error message if failed
    """

    success: bool
    returncode: int
    final_progress: FFmpegProgress | None
    stderr_tail: list[str]
    error_message: str | None = None


def _parse_progress_line(line: str, current: FFmpegProgress) -> FFmpegProgress | None:
    """Parse a single progress line from ffmpeg output.

    Args:
        line: Line from ffmpeg progress output
        current: Current progress state

    Returns:
        Updated FFmpegProgress if line contained progress info, None otherwise
    """
    line = line.strip()

    # Progress lines are key=value format
    if '=' not in line:
        return None

    key, _, value = line.partition('=')

    # Build new progress state by updating relevant field
    updates = {}

    if key == 'frame':
        try:
            updates['frame'] = int(value)
        except ValueError:
            pass
    elif key == 'fps':
        try:
            updates['fps'] = float(value)
        except ValueError:
            pass
    elif key == 'out_time_us':
        try:
            time_us = int(value)
            updates['time_us'] = time_us
            updates['time_seconds'] = time_us / 1_000_000.0
        except ValueError:
            pass
    elif key == 'bitrate':
        # Format: "1234.5kbits/s"
        try:
            bitrate_str = value.replace('kbits/s', '').replace('N/A', '0')
            updates['bitrate_kbps'] = float(bitrate_str)
        except ValueError:
            pass
    elif key == 'speed':
        # Format: "2.5x"
        try:
            speed_str = value.replace('x', '').replace('N/A', '0')
            updates['speed'] = float(speed_str)
        except ValueError:
            pass

    # Return updated progress if we got any updates
    if updates:
        # Create new progress with updated fields
        return FFmpegProgress(
            frame=updates.get('frame', current.frame),
            fps=updates.get('fps', current.fps),
            time_us=updates.get('time_us', current.time_us),
            time_seconds=updates.get('time_seconds', current.time_seconds),
            bitrate_kbps=updates.get('bitrate_kbps', current.bitrate_kbps),
            speed=updates.get('speed', current.speed),
        )

    return None


def _inject_progress_flags(cmd: list[str]) -> list[str]:
    """Inject required ffmpeg flags for progress reporting.

    Args:
        cmd: Original ffmpeg command

    Returns:
        Modified command with progress flags
    """
    # Find ffmpeg executable position
    ffmpeg_idx = 0
    for i, arg in enumerate(cmd):
        if 'ffmpeg' in arg.lower():
            ffmpeg_idx = i
            break

    # Insert flags right after ffmpeg executable
    new_cmd = cmd[:ffmpeg_idx + 1]
    new_cmd.extend([
        '-hide_banner',
        '-nostats',
        '-progress', 'pipe:1',  # Progress to stdout
    ])
    new_cmd.extend(cmd[ffmpeg_idx + 1:])

    return new_cmd


def run_ffmpeg_with_progress(
    cmd: list[str],
    expected_duration: float | None = None,
    stderr_buffer_lines: int = 10,
    timeout: int | None = None,
    video_meta: "VideoMetadata | None" = None,
) -> Generator[FFmpegProgress, None, FFmpegResult]:
    """Run ffmpeg command with progress tracking.

    This function modifies the command to include progress reporting flags,
    executes it, and yields progress updates as they arrive.

    If a ``VideoMetadata`` instance is supplied via ``video_meta``, the
    collected stderr lines are passed to
    ``video_meta.populate_from_ffmpeg_output()`` after the process finishes,
    opportunistically filling any still-``None`` backing fields.

    Args:
        cmd:                 FFmpeg command as list of strings.
        expected_duration:   Expected duration in seconds (for progress percentage).
        stderr_buffer_lines: Number of stderr lines to keep for error reporting.
        timeout:             Timeout in seconds (None for no timeout).
        video_meta:          Optional VideoMetadata instance to populate from stderr.

    Yields:
        FFmpegProgress objects with current execution state.

    Returns:
        FFmpegResult with final execution status.
    """
    # Inject progress flags
    modified_cmd = _inject_progress_flags(cmd)

    logger.debug(f"Running ffmpeg: {' '.join(modified_cmd)}")

    # Start process
    try:
        process = subprocess.Popen(
            modified_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
        )
    except Exception as e:
        logger.error(f"Failed to start ffmpeg: {e}")
        yield FFmpegResult(
            success=False,
            returncode=-1,
            final_progress=None,
            stderr_tail=[],
            error_message=f"Failed to start ffmpeg: {e}"
        )
        return

    # Track progress and stderr
    current_progress = FFmpegProgress()
    stderr_lines: list[str] = []

    try:
        # Read stdout for progress, stderr for errors
        import select
        import sys

        # On Windows, select doesn't work with pipes, so we need a different approach
        if sys.platform == 'win32':
            # Use threads to read both streams
            import queue
            import threading

            stdout_queue: queue.Queue = queue.Queue()
            stderr_queue: queue.Queue = queue.Queue()

            def read_stdout():
                try:
                    for line in process.stdout:
                        stdout_queue.put(('line', line))
                except Exception as e:
                    stdout_queue.put(('error', str(e)))
                finally:
                    stdout_queue.put(('done', None))

            def read_stderr() -> None:
                # ffmpeg writes status to stderr using \r (not \n), so we must
                # read in chunks rather than iterating lines — readline() would
                # block indefinitely waiting for a newline that never arrives.
                try:
                    while True:
                        chunk = process.stderr.read(4096)
                        if not chunk:
                            break
                        # Split on both \r and \n so we capture every logical line
                        for part in re.split(r'[\r\n]+', chunk):
                            if part:
                                stderr_queue.put(part)
                except Exception:
                    pass

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            # Process output
            stdout_done = False
            while not stdout_done:
                # Check for stdout progress
                try:
                    msg_type, data = stdout_queue.get(timeout=0.1)
                    if msg_type == 'done':
                        stdout_done = True
                    elif msg_type == 'line':
                        updated = _parse_progress_line(data, current_progress)
                        if updated:
                            current_progress = updated
                            yield current_progress
                except queue.Empty:
                    pass

                # Drain stderr
                while True:
                    try:
                        line = stderr_queue.get_nowait()
                        stderr_lines.append(line.rstrip())
                        if len(stderr_lines) > stderr_buffer_lines:
                            stderr_lines.pop(0)
                    except queue.Empty:
                        break

        else:
            # Unix: use select for non-blocking I/O
            while True:
                readable, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)

                if process.stdout in readable:
                    line = process.stdout.readline()
                    if not line:
                        break
                    updated = _parse_progress_line(line, current_progress)
                    if updated:
                        current_progress = updated
                        yield current_progress

                if process.stderr in readable:
                    # Read in chunks — ffmpeg uses \r not \n on stderr
                    chunk = process.stderr.read(4096)
                    if chunk:
                        for part in re.split(r'[\r\n]+', chunk):
                            if part:
                                stderr_lines.append(part)
                                if len(stderr_lines) > stderr_buffer_lines:
                                    stderr_lines.pop(0)

                # Check if process finished
                if process.poll() is not None:
                    break

        # Wait for process to complete
        returncode = process.wait(timeout=timeout)

        # Drain any remaining output
        if process.stdout:
            for line in process.stdout:
                updated = _parse_progress_line(line, current_progress)
                if updated:
                    current_progress = updated

        if process.stderr:
            remaining = process.stderr.read()
            if remaining:
                for part in re.split(r'[\r\n]+', remaining):
                    if part:
                        stderr_lines.append(part)
                        if len(stderr_lines) > stderr_buffer_lines:
                            stderr_lines.pop(0)

        # Build result
        success = returncode == 0
        error_msg = None if success else f"FFmpeg exited with code {returncode}"

        # Opportunistically populate VideoMetadata from collected stderr
        if video_meta is not None:
            video_meta.populate_from_ffmpeg_output(stderr_lines)

        result = FFmpegResult(
            success=success,
            returncode=returncode,
            final_progress=current_progress,
            stderr_tail=stderr_lines[-stderr_buffer_lines:],
            error_message=error_msg
        )

        yield result

    except subprocess.TimeoutExpired:
        process.kill()
        logger.error("FFmpeg process timed out")
        yield FFmpegResult(
            success=False,
            returncode=-1,
            final_progress=current_progress,
            stderr_tail=stderr_lines[-stderr_buffer_lines:],
            error_message="Process timed out"
        )

    except Exception as e:
        logger.error(f"Error during ffmpeg execution: {e}", exc_info=True)
        if process.poll() is None:
            process.kill()
        yield FFmpegResult(
            success=False,
            returncode=-1,
            final_progress=current_progress,
            stderr_tail=stderr_lines[-stderr_buffer_lines:],
            error_message=f"Execution error: {e}"
        )


def get_video_duration(video_file: Path) -> float:
    """Get video duration in seconds using ffprobe.

    Args:
        video_file: Path to video file

    Returns:
        Duration in seconds. NaN if cannot be determined.
    """
    cmd: list[str|PathLike] = ["ffprobe", "-v", "error",
                               "-select_streams", "v:0",
                               "-show_entries", "format=duration",
                               "-of", "default=noprint_wrappers=1:nokey=1",
                               str(video_file)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=TIMEOUT_SECONDS_SHORT)
        duration = float(result.stdout.strip())
        return duration
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired) as e:
        logger.error(f"Error getting video duration for \"{str(video_file)}\": {e}", exc_info=True)
        return float('nan')
