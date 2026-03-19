# Requirements Document

- Created: 2026-03-17
- Completed: 2026-03-17

## Introduction

The pyqenc pipeline makes ffmpeg calls from at least seven different modules
(`extraction.py`, `chunking.py`, `encoding.py`, `merge.py`, `audio.py`,
`models.py`, and `quality.py`), each with its own ad-hoc subprocess management.
The result is recurring bugs: stderr not drained (buffer fills and blocks ffmpeg
indefinitely), progress not reported, and errors silently swallowed.

A correct async `_drain_stderr` coroutine and a `-progress pipe:1` reader
already exist in `visualization.py` but are private and not reused. A
synchronous generator-based wrapper (`run_ffmpeg_with_progress`) exists in
`ffmpeg_wrapper.py` but is only used by `get_frame_count` in `ffmpeg.py`, and
`ffmpeg.py` itself contains several functions that are either dead code or
duplicates of logic already in the phase modules.

**Key findings from direct ffmpeg testing:**

- With `-progress pipe:1 -nostats`, stdout carries structured `key=value`
  blocks terminated by `progress=continue` or `progress=end`. Each block
  contains `frame`, `out_time_us`, `fps`, `speed`, etc. The `frame` value in
  the final `progress=end` block is the **total frame count** of the output.
  Stdout is `\n`-delimited and safe to read line-by-line.
- With `-nostats`, stderr contains **zero `\r` bytes** — only the short header
  block (`Duration:`, `Stream #0:0: Video:`, codec info) plus a one-line final
  summary (~4 KB total). It is `\n`-delimited and safe to read line-by-line.
  The `\r`-spammy live status line is fully suppressed by `-nostats`.
- Because both streams are `\n`-delimited with `-nostats`, **both can use
  simple `readline()`** — no raw chunk reading is needed.
- All video metadata needed by `VideoMetadata.populate_from_ffmpeg_output`
  (`duration`, `fps`, `resolution`, `pix_fmt`, `frame_count`) is available
  from these two streams without any extra ffprobe call.

This spec defines a single async ffmpeg runner in
`pyqenc/utils/ffmpeg_runner.py` that all callers use. Callers prepare their
own argument lists; the runner owns subprocess lifecycle, stderr buffering,
stdout progress parsing, metadata population, and callback dispatch. The
existing `run_ffmpeg_with_progress` generator and the legacy `ffmpeg.py` /
`ffmpeg_wrapper.py` utilities are cleaned up as part of this work.

## Glossary

- **FFmpegRunner**: The new module `pyqenc/utils/ffmpeg_runner.py` containing
  the async entry point `run_ffmpeg_async` and the synchronous wrapper
  `run_ffmpeg`.
- **progress block**: A sequence of `key=value` lines on stdout terminated by
  `progress=continue` or `progress=end`. Each block represents one ffmpeg
  status update tick.
- **ProgressCallback**: A callable `(frame: int, out_time_seconds: float) -> None`
  supplied by the caller and invoked once per completed progress block.
- **FFmpegRunResult**: Dataclass returned by the runner containing
  `returncode: int`, `success: bool`, `stderr_lines: list[str]` (all
  non-empty lines from stderr — small with `-nostats`), and
  `frame_count: int | None` (the `frame` value from the final
  `progress=end` block).
- **STDERR_TAIL_LINES**: Named constant in `pyqenc/constants.py` controlling
  how many stderr lines are logged on failure (the full `stderr_lines` list
  is always returned to the caller).
- **`populate_from_ffmpeg_output`**: Method on `VideoMetadata` in `models.py`
  that parses `Duration:`, `Stream #0:0: Video:`, and `frame=N` lines from
  ffmpeg stderr to fill `_duration_seconds`, `_fps`, `_resolution`, `_pix_fmt`,
  and `_frame_count` backing fields.
- **`_drain_stderr`**: Existing private async coroutine in `visualization.py`
  that reads stderr in raw chunks to handle `\r`-delimited lines; will be
  removed since `-nostats` eliminates `\r` from stderr, making it redundant.
- **`_read_progress`**: Existing private async coroutine in `visualization.py`
  that reads `-progress pipe:1` stdout; will be removed once FFmpegRunner
  supersedes it.
- **`run_ffmpeg_with_progress`**: Existing synchronous generator in
  `ffmpeg_wrapper.py`; superseded for all pipeline-internal uses.
- **`_run_ffmpeg_null`**: Private function in `models.py` that runs
  `ffmpeg -c copy -f null` to count frames; will be replaced by FFmpegRunner.
- **`ffmpeg.py`**: Utility module `pyqenc/utils/ffmpeg.py` containing
  `get_frame_count`, `detect_scenes`, `segment_video`, `detect_crop_parameters`,
  and `verify_frame_counts`; most of these are dead code or duplicates.
- **`run_metric`**: Existing coroutine in `quality.py` that launches ffmpeg for
  PSNR/SSIM/VMAF measurement; it will be updated to use FFmpegRunner.

## Requirements

### Requirement 1

**User Story:** As a developer, I want a single async function that runs any
ffmpeg command and correctly handles both stdout and stderr, so that ffmpeg
never blocks on a full pipe and all output is available to callers.

#### Acceptance Criteria

1. THE FFmpegRunner SHALL accept a `cmd: list[str | os.PathLike]` argument
   containing the full ffmpeg command (including the `ffmpeg` executable) and
   SHALL launch it as an `asyncio` subprocess with `stdout=PIPE` and
   `stderr=PIPE`.
2. THE FFmpegRunner SHALL automatically inject `-hide_banner -nostats -progress
   pipe:1` into the command immediately after the `ffmpeg` executable when
   those flags are not already present, ensuring structured progress output on
   stdout and suppressing the `\r`-spammy live status line on stderr.
3. WHILE the ffmpeg subprocess is running, THE FFmpegRunner SHALL read stdout
   line-by-line and accumulate `key=value` pairs into a current progress block;
   WHEN a line `progress=continue` or `progress=end` is encountered, THE
   FFmpegRunner SHALL treat the accumulated pairs as one complete progress block
   and dispatch the ProgressCallback if provided.
4. WHILE the ffmpeg subprocess is running, THE FFmpegRunner SHALL read stderr
   line-by-line (using `readline()`) and buffer all non-empty lines; with
   `-nostats` injected, stderr contains zero `\r` bytes and is fully
   `\n`-delimited, so `readline()` is safe and correct.
5. THE FFmpegRunner SHALL run the stderr reader and stdout progress reader as
   concurrent async tasks so that neither blocks the other.
6. WHEN the ffmpeg subprocess exits, THE FFmpegRunner SHALL await both tasks
   before returning, ensuring all output is consumed.
7. THE FFmpegRunner SHALL return an `FFmpegRunResult` dataclass containing:
   `returncode: int`, `success: bool` (True when returncode == 0),
   `stderr_lines: list[str]` (all non-empty lines from stderr), and
   `frame_count: int | None` (the `frame` value from the final
   `progress=end` block — this is the total output frame count).

### Requirement 2

**User Story:** As a developer, I want to receive granular progress callbacks
during ffmpeg execution, so that callers can update a progress bar without
polling.

#### Acceptance Criteria

1. THE FFmpegRunner SHALL accept an optional `progress_callback:
   Callable[[int, float], None] | None` parameter where the arguments are
   `(frame: int, out_time_seconds: float)`; WHEN provided, THE FFmpegRunner
   SHALL invoke it once per completed progress block using the `frame` and
   `out_time_us` values from that block.
2. WHEN `progress_callback` is `None`, THE FFmpegRunner SHALL still read
   stdout completely to prevent pipe blocking, but SHALL NOT invoke any
   callback.
3. IF the progress callback raises an exception, THEN THE FFmpegRunner SHALL
   log a `debug`-level warning and continue processing without propagating
   the exception.

### Requirement 3

**User Story:** As a developer, I want the runner to populate a VideoMetadata
instance directly from ffmpeg output, so that callers get metadata without a
separate ffprobe call.

#### Acceptance Criteria

1. THE FFmpegRunner SHALL accept an optional `video_meta: VideoMetadata | None`
   parameter; WHEN provided, THE FFmpegRunner SHALL call
   `video_meta.populate_from_ffmpeg_output(result.stderr_lines)` before
   returning, so that `duration_seconds`, `fps`, `resolution`, `pix_fmt`, and
   `frame_count` are filled from the stderr header block.
2. WHEN `video_meta` is provided and `FFmpegRunResult.frame_count` is not
   `None`, THE FFmpegRunner SHALL also set `video_meta._frame_count` from
   `frame_count` (the stdout-derived value), overriding any stderr-parsed
   value, since the stdout `progress=end` block gives the exact output frame
   count.
3. THE FFmpegRunner SHALL NOT import `VideoMetadata` at module level; the
   import SHALL be deferred or use a `TYPE_CHECKING` guard so the runner
   remains importable without loading the full model graph.

### Requirement 4

**User Story:** As a developer, I want the runner to report errors clearly, so
that callers can log meaningful diagnostics when ffmpeg fails.

#### Acceptance Criteria

1. WHEN `FFmpegRunResult.success` is `False`, THE FFmpegRunner SHALL
   automatically log the last `STDERR_TAIL_LINES` lines from `stderr_lines`
   at `error` level, so callers do not need to repeat this logic.
2. THE FFmpegRunner SHALL NOT log stderr content when `success` is `True`,
   to avoid noise in normal operation.

### Requirement 5

**User Story:** As a developer, I want all existing ffmpeg call sites in the
pipeline to use the new runner, so that the pipe-blocking bug is eliminated
everywhere and progress callbacks are available wherever needed.

#### Acceptance Criteria

1. THE Pipeline SHALL update `ChunkEncoder._encode_with_ffmpeg` in
   `encoding.py` to use `run_ffmpeg` (synchronous wrapper) instead of
   `subprocess.run` with `stderr=DEVNULL`; on failure, `stderr_lines` SHALL
   be available for error logging.
2. THE Pipeline SHALL update `split_chunks_from_state` in `chunking.py` to
   use `run_ffmpeg` instead of `subprocess.run`; `result.stderr_lines` SHALL
   be passed to `chunk_meta.populate_from_ffmpeg_output()` so that duration,
   resolution, and frame count are populated from the split output.
3. THE Pipeline SHALL update `_detect_crop_parameters` in `extraction.py` to
   use `run_ffmpeg` instead of `subprocess.run`; `result.stderr_lines` SHALL
   be used for cropdetect line parsing.
4. THE Pipeline SHALL update the stream extraction calls in `extract_streams`
   in `extraction.py` (video and audio track copy commands) to use `run_ffmpeg`
   instead of `subprocess.run`.
5. THE Pipeline SHALL update `run_metric` in `quality.py` to use
   `run_ffmpeg_async` instead of `asyncio.create_subprocess_exec` directly;
   the `progress_callback` SHALL be wired to the `bar_advance` callable
   supplied by `_generate_metrics` in `visualization.py`.
6. THE Pipeline SHALL update `merge_final_video` in `merge.py` to use
   `run_ffmpeg` for the ffmpeg concat step instead of `subprocess.run`.
7. THE Pipeline SHALL update `_run_ffmpeg_null` in `models.py` to use
   `run_ffmpeg` instead of `subprocess.run`; `result.frame_count` SHALL
   replace the manual `frame=N` regex parse, and `result.stderr_lines` SHALL
   be passed to `populate_from_ffmpeg_output` for opportunistic metadata
   population.
8. THE Pipeline SHALL update the audio strategy `execute` / `execute_async`
   methods in `audio.py` (ConversionStrategy, DownmixStrategy, and the FLAC
   conversion loop) to use `run_ffmpeg` / `run_ffmpeg_async` instead of
   `subprocess.run` / `asyncio.create_subprocess_exec`.
9. WHEN all pipeline call sites use FFmpegRunner, THE Pipeline SHALL remove
   the private `_drain_stderr` and `_read_progress` coroutines from
   `visualization.py`.

### Requirement 6

**User Story:** As a developer, I want the legacy ffmpeg utility files cleaned
up, so that there is one clear place to look for ffmpeg execution and no dead
code remains.

#### Acceptance Criteria

1. THE Pipeline SHALL remove `detect_scenes`, `segment_video`, and
   `detect_crop_parameters` from `ffmpeg.py` — these are dead code (not
   imported anywhere in the pipeline) and duplicate logic already in the phase
   modules.
2. THE Pipeline SHALL update `get_frame_count` in `ffmpeg.py` to use
   `run_ffmpeg` instead of `run_ffmpeg_with_progress`; `result.frame_count`
   SHALL replace the manual frame-count extraction loop.
3. THE Pipeline SHALL remove `run_ffmpeg_with_progress`, `_inject_progress_flags`,
   `_parse_progress_line`, `FFmpegProgress`, and `FFmpegResult` from
   `ffmpeg_wrapper.py` once no callers remain; `get_video_duration` SHALL also
   be removed if it has no remaining callers, and `ffmpeg_wrapper.py` SHALL be
   deleted if it becomes empty.
4. IF `get_frame_count` is the only remaining function in `ffmpeg.py` after
   the dead-code removal, THEN it SHALL be moved into `ffmpeg_runner.py` as a
   convenience helper and `ffmpeg.py` SHALL be deleted.

### Requirement 7

**User Story:** As a developer, I want the runner to be usable from both async
and synchronous contexts, so that callers that cannot use `await` can still
benefit from correct pipe handling.

#### Acceptance Criteria

1. THE FFmpegRunner SHALL expose an async entry point
   `run_ffmpeg_async(cmd, progress_callback, video_meta) -> FFmpegRunResult`
   for callers that are already in an async context.
2. THE FFmpegRunner SHALL expose a synchronous wrapper
   `run_ffmpeg(cmd, progress_callback, video_meta) -> FFmpegRunResult` that
   calls `asyncio.run(run_ffmpeg_async(...))` for callers in a synchronous
   context.
3. WHEN `run_ffmpeg` is called from within an already-running event loop,
   THE FFmpegRunner SHALL detect this condition and raise a clear
   `RuntimeError` directing the caller to use `run_ffmpeg_async` instead,
   rather than silently deadlocking.
4. THE FFmpegRunner SHALL be importable from `pyqenc.utils.ffmpeg_runner`
   without importing any pipeline-specific models at module level, so it can
   be used in isolation.
