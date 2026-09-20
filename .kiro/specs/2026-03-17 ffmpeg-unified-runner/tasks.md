# Implementation Plan

- [x] 1. Create `pyqenc/utils/ffmpeg_runner.py` with core runner






  - Define `FFmpegRunResult` dataclass (`returncode`, `success`, `stderr_lines`, `frame_count`)
  - Implement `_inject_flags(cmd)` — inserts `-hide_banner -nostats -progress pipe:1` after the `ffmpeg` executable, idempotent
  - Implement `_read_stdout(stdout, callback) -> int | None` — readline loop, accumulates `key=value` pairs into a block dict, dispatches `callback(frame, out_time_s)` on `progress=continue|end`, returns `frame` from final `progress=end`
  - Implement `_read_stderr(stderr) -> list[str]` — readline loop, collects all non-empty lines
  - Implement `run_ffmpeg_async(cmd, progress_callback, video_meta) -> FFmpegRunResult` — launches subprocess, gathers both readers, awaits process, logs stderr tail on failure, populates `video_meta` if provided
  - Implement `run_ffmpeg(cmd, progress_callback, video_meta) -> FFmpegRunResult` — sync wrapper with event-loop guard
  - Implement `get_frame_count(video_file) -> int` — convenience helper using null-copy command
  - _Requirements: 1.1–1.7, 2.1–2.3, 3.1–3.3, 4.1–4.2, 6.4, 7.1–7.4_

- [x] 2. Migrate `quality.py` and `visualization.py` to use FFmpegRunner





- [x] 2.1 Update `run_metric` in `quality.py`


  - Change signature to `async def run_metric(..., progress_callback: ProgressCallback | None = None) -> FFmpegRunResult`
  - Replace `asyncio.create_subprocess_exec` call with `run_ffmpeg_async(cmd, progress_callback=progress_callback)`
  - Return the `FFmpegRunResult` directly
  - _Requirements: 5.5_

- [x] 2.2 Update `_generate_metrics` in `visualization.py`


  - Replace the `_wait_with_progress` inner function and `asyncio.gather(*[_wait_with_progress(p) for p in processes])` with direct `await run_metric(...)` calls that pass `bar_advance` as `progress_callback`
  - Remove `_drain_stderr` and `_read_progress` coroutines from `visualization.py`
  - _Requirements: 5.5, 5.9_

- [x] 3. Migrate encoding, chunking, and extraction phases






- [x] 3.1 Update `_encode_with_ffmpeg` in `encoding.py`

  - Replace `subprocess.run(..., stdout=DEVNULL, stderr=DEVNULL)` with `run_ffmpeg(cmd)`
  - Check `result.success` instead of `result.returncode`; runner already logs stderr on failure
  - _Requirements: 5.1_


- [x] 3.2 Update `split_chunks_from_state` in `chunking.py`

  - Replace `subprocess.run(cmd, capture_output=True, text=True)` with `run_ffmpeg(cmd, video_meta=chunk_meta)`
  - Remove the explicit `chunk_meta.populate_from_ffmpeg_output(proc.stderr.splitlines())` call — runner handles it
  - Check `result.success` for failure detection
  - _Requirements: 5.2_


- [x] 3.3 Update `_detect_crop_parameters` and `extract_streams` in `extraction.py`

  - Replace `subprocess.run` in `_detect_crop_parameters` with `run_ffmpeg(cmd)`; parse `result.stderr_lines` for `cropdetect` lines
  - Replace each `subprocess.run` in `extract_streams` (video and audio track copy loops) with `run_ffmpeg(cmd)`; check `result.success`
  - _Requirements: 5.3, 5.4_

- [x] 4. Migrate merge, models, and audio





- [x] 4.1 Update `merge_final_video` in `merge.py`


  - Replace `subprocess.run(concat_cmd, capture_output=True, text=True, check=True)` with `run_ffmpeg(concat_cmd)`
  - Check `result.success`; log failure and continue to next strategy on error
  - _Requirements: 5.6_

- [x] 4.2 Update `_run_ffmpeg_null` in `models.py`


  - Replace `subprocess.run` with `run_ffmpeg(cmd)`
  - Return `(result.frame_count, result.stderr_lines)` — same tuple signature, cleaner implementation
  - _Requirements: 5.7_

- [x] 4.3 Update audio strategy methods in `audio.py`


  - Replace `subprocess.run` in `ConversionStrategy.execute`, `DownmixStrategy.execute`, and the FLAC conversion loop with `run_ffmpeg(cmd)`
  - Replace `asyncio.create_subprocess_exec` + `process.communicate()` in `execute_async` methods with `await run_ffmpeg_async(cmd)`
  - _Requirements: 5.8_

- [x] 5. Clean up legacy ffmpeg utilities





  - Update `get_frame_count` in `ffmpeg.py` to delegate to `ffmpeg_runner.get_frame_count` (or inline the call)
  - Remove `detect_scenes`, `segment_video`, `detect_crop_parameters`, `verify_frame_counts` from `ffmpeg.py` (dead code)
  - Delete `ffmpeg_wrapper.py` (fully superseded)
  - Delete `ffmpeg.py` once `get_frame_count` is moved and no other callers remain
  - Update `orchestrator.py` import of `get_frame_count` to point to `ffmpeg_runner`
  - Update `merge.py` import of `get_frame_count` to point to `ffmpeg_runner`
  - _Requirements: 6.1–6.4_

- [x] 6. Write unit tests for `ffmpeg_runner.py`





  - Test `_inject_flags`: flags inserted after `ffmpeg`, not duplicated if already present
  - Test `_read_stdout` with a mock `asyncio.StreamReader`: progress blocks parsed correctly, callback invoked per block, `frame_count` from `progress=end` returned
  - Test `run_ffmpeg` event-loop guard: calling from a running loop raises `RuntimeError`
  - _Requirements: 1.2, 1.3, 7.3_
