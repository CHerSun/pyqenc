# Design Document — FFmpeg Unified Runner

- Created: 2026-03-17
- Completed: 2026-03-17

## Overview

A single async module `pyqenc/utils/ffmpeg_runner.py` replaces all ad-hoc
`subprocess.run` / `asyncio.create_subprocess_exec` ffmpeg calls across the
pipeline. It injects `-hide_banner -nostats -progress pipe:1`, reads both
stdout and stderr concurrently via `readline()` (safe because `-nostats`
eliminates all `\r` from stderr), parses structured progress blocks from
stdout, and returns a clean `FFmpegRunResult`. Callers optionally supply a
`ProgressCallback` and/or a `VideoMetadata` instance to be populated in-place.

Legacy files `ffmpeg_wrapper.py` and `ffmpeg.py` are cleaned up; dead-code
functions are removed and the remaining `get_frame_count` helper moves into
`ffmpeg_runner.py`.

---

## Architecture

```
Caller (sync)          Caller (async)
     │                      │
run_ffmpeg(cmd, ...)   run_ffmpeg_async(cmd, ...)
     │                      │
     └──────────────────────┘
                  │
         _run_async(cmd, cb, vm)
                  │
        asyncio.create_subprocess_exec
          stdout=PIPE, stderr=PIPE
                  │
        ┌─────────┴──────────┐
   _read_stdout()        _read_stderr()
   (readline loop)       (readline loop)
   parse key=value       buffer all lines
   blocks; dispatch cb   for metadata + errors
        └─────────┬──────────┘
                  │
           FFmpegRunResult
           + populate video_meta
```

Both `_read_stdout` and `_read_stderr` are async coroutines run concurrently
via `asyncio.gather`. Neither blocks the other. The process is awaited only
after both readers finish, guaranteeing all pipe output is consumed before
`returncode` is read.

---

## Components and Interfaces

### `pyqenc/utils/ffmpeg_runner.py`

```python
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import STDERR_TAIL_LINES

if TYPE_CHECKING:
    from pyqenc.models import VideoMetadata

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class FFmpegRunResult:
    returncode:   int
    success:      bool
    stderr_lines: list[str]          # all non-empty lines — small with -nostats
    frame_count:  int | None = None  # from progress=end block on stdout

ProgressCallback = Callable[[int, float], None]
"""Signature: (frame: int, out_time_seconds: float) -> None"""

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def run_ffmpeg_async(
    cmd:               list[str | os.PathLike],
    progress_callback: ProgressCallback | None = None,
    video_meta:        VideoMetadata | None    = None,
) -> FFmpegRunResult: ...

def run_ffmpeg(
    cmd:               list[str | os.PathLike],
    progress_callback: ProgressCallback | None = None,
    video_meta:        VideoMetadata | None    = None,
) -> FFmpegRunResult: ...

# ---------------------------------------------------------------------------
# Convenience helper (replaces ffmpeg.py get_frame_count)
# ---------------------------------------------------------------------------

def get_frame_count(video_file: Path) -> int:
    """Return the total frame count of video_file via ffmpeg null-copy."""
    ...
```

#### Flag injection

`_inject_flags(cmd)` scans for the `ffmpeg` executable position and inserts
`["-hide_banner", "-nostats", "-progress", "pipe:1"]` immediately after it,
skipping any flag already present. This is idempotent — calling it twice on
the same command is safe.

#### `_read_stdout(stdout, callback) -> int | None`

Reads stdout line-by-line. Accumulates `key=value` pairs into a `dict`
representing the current progress block. When `progress=continue` or
`progress=end` is seen:

- Extracts `frame` (int) and `out_time_us` (int → divide by 1 000 000 for
  seconds).
- Invokes `callback(frame, out_time_seconds)` if provided, swallowing any
  exception with a `debug` log.
- Clears the accumulator for the next block.

Returns the `frame` value from the last `progress=end` block as the total
frame count, or `None` if no `progress=end` was seen.

#### `_read_stderr(stderr) -> list[str]`

Reads stderr line-by-line, stripping each line and collecting non-empty ones.
Returns the full list. With `-nostats` this is ~20–60 lines (header + final
summary) — never large enough to be a memory concern.

#### `run_ffmpeg_async`

```python
async def run_ffmpeg_async(cmd, progress_callback=None, video_meta=None):
    modified_cmd = _inject_flags(list(cmd))
    proc = await asyncio.create_subprocess_exec(
        *modified_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    frame_count, stderr_lines = await asyncio.gather(
        _read_stdout(proc.stdout, progress_callback),
        _read_stderr(proc.stderr),
    )
    await proc.wait()
    result = FFmpegRunResult(
        returncode   = proc.returncode,
        success      = proc.returncode == 0,
        stderr_lines = stderr_lines,
        frame_count  = frame_count,
    )
    if not result.success:
        for line in stderr_lines[-STDERR_TAIL_LINES:]:
            _logger.error("ffmpeg: %s", line)
    if video_meta is not None:
        video_meta.populate_from_ffmpeg_output(stderr_lines)
        if frame_count is not None:
            video_meta._frame_count = frame_count  # stdout value is authoritative
    return result
```

#### `run_ffmpeg` (sync wrapper)

```python
def run_ffmpeg(cmd, progress_callback=None, video_meta=None):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        raise RuntimeError(
            "run_ffmpeg() called from within a running event loop. "
            "Use run_ffmpeg_async() instead."
        )
    return asyncio.run(run_ffmpeg_async(cmd, progress_callback, video_meta))
```

#### `get_frame_count` (convenience helper)

```python
def get_frame_count(video_file: Path) -> int:
    cmd = ["ffmpeg", "-i", video_file, "-map", "0:v:0", "-c", "copy", "-f", "null", "-"]
    result = run_ffmpeg(cmd)
    if result.frame_count is None:
        raise FrameCountError(f"Could not determine frame count for {video_file}")
    return result.frame_count
```

---

## Call-site Migration

### `encoding.py` — `_encode_with_ffmpeg`

Replace `subprocess.run(..., stderr=DEVNULL)` with `run_ffmpeg(cmd)`.
On failure, `result.stderr_lines` is already logged by the runner; the method
just checks `result.success` and returns `False`.

### `chunking.py` — `split_chunks_from_state`

Replace `subprocess.run(cmd, capture_output=True, text=True)` with
`run_ffmpeg(cmd, video_meta=chunk_meta)`. The runner populates
`chunk_meta` directly; the explicit `chunk_meta.populate_from_ffmpeg_output(proc.stderr.splitlines())`
call is removed.

### `extraction.py` — `_detect_crop_parameters`

Replace `subprocess.run(cmd, capture_output=True, text=True)` with
`run_ffmpeg(cmd)`. Parse `result.stderr_lines` for `cropdetect` lines
(same logic, different source).

### `extraction.py` — `extract_streams` (track copy commands)

Replace each `subprocess.run(cmd, capture_output=True, text=True, check=True)`
with `run_ffmpeg(cmd)`. Check `result.success`; raise on failure.

### `quality.py` — `run_metric`

`run_metric` currently returns a `Coroutine[..., Process]` and the caller
manages the process lifecycle. After migration it becomes a plain async
function that calls `run_ffmpeg_async` and returns `FFmpegRunResult`.
`_generate_metrics` in `visualization.py` passes a `progress_callback` that
advances the `alive_bar`.

The `_wait_with_progress` inner function in `_generate_metrics` is removed;
`run_ffmpeg_async` handles waiting internally.

### `merge.py` — `merge_final_video`

Replace `subprocess.run(concat_cmd, capture_output=True, text=True, check=True)`
with `run_ffmpeg(concat_cmd)`. Check `result.success`.

### `models.py` — `_run_ffmpeg_null`

Replace `subprocess.run` with `run_ffmpeg(cmd)`. Return
`(result.frame_count, result.stderr_lines)` — same signature, cleaner
implementation.

### `audio.py` — strategy `execute` / `execute_async`

- `execute`: replace `subprocess.run` with `run_ffmpeg`.
- `execute_async`: replace `asyncio.create_subprocess_exec` +
  `process.communicate()` with `await run_ffmpeg_async`.
- FLAC conversion loop: replace `subprocess.run` with `run_ffmpeg`.

---

## Data Models

### `FFmpegRunResult`

```
FFmpegRunResult
├── returncode:   int           # raw process exit code
├── success:      bool          # returncode == 0
├── stderr_lines: list[str]     # all non-empty stderr lines (~20–60 with -nostats)
└── frame_count:  int | None    # frame value from progress=end block; None if not seen
```

### Progress block (internal, not exposed)

```
{
  "frame":        int,    # current output frame
  "fps":          float,
  "out_time_us":  int,    # microseconds of output processed
  "speed":        str,    # e.g. "3.5x"
  "progress":     str,    # "continue" | "end"
  ...
}
```

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| ffmpeg exits non-zero | `success=False`; last `STDERR_TAIL_LINES` logged at `error` |
| `progress_callback` raises | Swallowed; `debug` log; processing continues |
| `progress=end` never seen (e.g. ffmpeg killed) | `frame_count=None` in result |
| `run_ffmpeg` called inside event loop | `RuntimeError` with clear message |
| `video_meta` population fails | Exception propagates (caller's responsibility) |

---

## Testing Strategy

- Unit test `_inject_flags`: verifies flags are inserted after `ffmpeg`, not
  duplicated if already present.
- Unit test `_read_stdout` with a mock stream: verifies progress blocks are
  parsed correctly, callback is invoked per block, `frame_count` from
  `progress=end` is returned.
- Unit test `run_ffmpeg` event-loop guard: calling from a running loop raises
  `RuntimeError`.
- Integration test `get_frame_count` against the sample video: verifies the
  returned count matches a known value.

---

## Cleanup Plan

### Files to modify
- `pyqenc/utils/ffmpeg_runner.py` — **new file**
- `pyqenc/quality.py` — `run_metric` signature change
- `pyqenc/utils/visualization.py` — remove `_drain_stderr`, `_read_progress`, `_wait_with_progress`; update `_generate_metrics`
- `pyqenc/phases/encoding.py` — `_encode_with_ffmpeg`
- `pyqenc/phases/chunking.py` — `split_chunks_from_state`
- `pyqenc/phases/extraction.py` — `_detect_crop_parameters`, `extract_streams`
- `pyqenc/phases/merge.py` — `merge_final_video`
- `pyqenc/phases/audio.py` — strategy `execute` / `execute_async` methods
- `pyqenc/models.py` — `_run_ffmpeg_null`

### Files to delete
- `pyqenc/utils/ffmpeg_wrapper.py` — fully superseded
- `pyqenc/utils/ffmpeg.py` — dead code removed; `get_frame_count` moved to `ffmpeg_runner.py`

### `run_metric` signature change

Current: returns `Coroutine[Any, Any, Process]` — caller manages the process.

New: `async def run_metric(..., progress_callback: ProgressCallback | None = None) -> FFmpegRunResult`

`_generate_metrics` in `visualization.py` no longer needs to manage process
lifecycle or call `_wait_with_progress`; it just awaits `run_metric` and reads
`result.stderr_lines` for error reporting.
