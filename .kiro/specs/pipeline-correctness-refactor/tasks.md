# Implementation Plan

## Gap Analysis Summary

Current state vs. design targets:
- `ChunkVideoMetadata` still exists (not renamed to `ChunkMetadata`); no `start_timestamp`/`end_timestamp` fields
- `ChunkInfo` dataclass duplicated in both `chunking.py` and `encoding.py`
- `AudioMetadata` model does not exist; `ExtractionResult.audio_files` is `list[Path]`
- `AttemptMetadata` model does not exist; `ChunkEncodingResult.encoded_file` is `Path | None`
- `VideoMetadata` has no `crop_params` field; crop lives in `PhaseMetadata.crop_params: str | None`
- `ExtractionResult` returns `video_files: list[VideoMetadata]` (not single `video: VideoMetadata`)
- `_detect_crop_parameters` returns `None` when no borders found (should return `CropParams()`)
- `_chunk_name` (frame-based) still present and called alongside `_chunk_name_duration`
- `_CHUNK_NAME_PATTERN` defined locally in `chunking.py`; `CHUNK_NAME_PATTERN`/`CHUNK_GLOB_PATTERN` not in `constants.py`
- Encoded attempt filenames include attempt number (`attempt_N`); no CRF-only naming
- No temp-file rename protocol in encoding; no `.tmp` cleanup at encoding start
- `_check_existing_encoding` uses progress tracker lookup, not filesystem scan
- `ChunkState`/`StrategyState`/`AttemptInfo` tracker models still present; design calls for removal
- Merge phase merges all strategies regardless of `optimal_strategy`; no `optimal_strategy` param
- Merge phase does not sort chunks by filename before merging
- `ENCODED_ATTEMPT_GLOB_PATTERN` / `ENCODED_ATTEMPT_NAME_PATTERN` not in `constants.py`
- Status symbols, separator lines, and filename chars already in `constants.py` ✓
- Work-directory source-file binding (Req 8) not implemented
- Quality metrics progress bar (Req 9) not implemented
- `PhaseOutcome` enum not defined; result types use `success/reused/needs_work` triple
- `populate_from_ffmpeg_output` called for chunk metadata after split (Req 2.8) not implemented

---

- [x] 1. Add `CHUNK_NAME_PATTERN`, `CHUNK_GLOB_PATTERN`, `ENCODED_ATTEMPT_GLOB_PATTERN`, and `ENCODED_ATTEMPT_NAME_PATTERN` to `constants.py`





  - Add `CHUNK_NAME_PATTERN` regex matching timestamp-based chunk stems (using `TIME_SEPARATOR_SAFE` and `TIME_SEPARATOR_MS` chars)
  - Add `CHUNK_GLOB_PATTERN = "*.mkv"` for chunk discovery
  - Add `ENCODED_ATTEMPT_GLOB_PATTERN = "*.crf*.mkv"` for encoded attempt discovery
  - Add `ENCODED_ATTEMPT_NAME_PATTERN` regex with named groups `chunk_id`, `resolution`, `crf`
  - Group all four under a clear `# Artifact discovery patterns` comment block
  - _Requirements: 3.4, 4.6, 7.4_

- [x] 2. Consolidate and extend data models in `models.py`









- [x] 2.1 Rename `ChunkVideoMetadata` to `ChunkMetadata` and add timestamp fields


  - Rename class to `ChunkMetadata`; remove `start_frame` field; add `start_timestamp: float` and `end_timestamp: float`
  - Update `model_validate_full` override to use new class name
  - Add `crop_params: CropParams | None = None` field to `VideoMetadata`; include it in `model_dump_full` / `model_validate_full`
  - Remove `start_frame: int = 0` from `VideoMetadata`
  - _Requirements: 1.1, 2.1_

- [x] 2.2 Add `AudioMetadata` and `AttemptMetadata` models


  - Define `AudioMetadata(BaseModel)` with fields: `path`, `codec`, `channels`, `language`, `title`, `duration_seconds`, `start_timestamp`
  - Define `AttemptMetadata(BaseModel)` with fields: `path`, `chunk_id`, `strategy`, `crf`, `resolution`, `file_size_bytes`
  - Update `ExtractionResult` dataclass: rename `video_files: list[VideoMetadata]` → `video: VideoMetadata`; rename `audio_files: list[Path]` → `audio: list[AudioMetadata]`; remove `crop_params` field (now on `video.crop_params`)
  - Update `ChunkEncodingResult.encoded_file` type from `Path | None` to `AttemptMetadata | None`
  - _Requirements: 1.4, 1.5, 1.6_

- [x] 2.3 Add `PhaseOutcome` enum and update result types


  - Define `PhaseOutcome(Enum)` with values `COMPLETED`, `REUSED`, `DRY_RUN`, `FAILED`
  - Update `ChunkingResult`, `EncodingResult`, `MergeResult` to use `outcome: PhaseOutcome` instead of `success/reused/needs_work` triple
  - Update `PhaseResult` in `orchestrator.py` similarly
  - _Requirements: 1.1, 5.1_

- [x] 3. Update extraction phase to use new models





- [x] 3.1 Update `_detect_crop_parameters` to always return `CropParams`


  - Change return type from `CropParams | None` to `CropParams`
  - Return `CropParams()` (all zeros) instead of `None` when no borders found or on failure
  - _Requirements: 2.1_

- [x] 3.2 Update `extract_streams` to return single `VideoMetadata` with `crop_params` populated


  - Change function to return `ExtractionResult` with `video: VideoMetadata` (first video stream only)
  - After crop detection/manual crop, set `video.crop_params` on the returned `VideoMetadata`
  - Populate `audio: list[AudioMetadata]` from extracted audio tracks (codec, channels, language, title, start_timestamp from `AudioStream`)
  - Remove `crop_params` as a separate field from `ExtractionResult`
  - _Requirements: 1.4, 1.6, 2.1_

- [x] 4. Update chunking phase to use `ChunkMetadata` and `CHUNK_NAME_PATTERN`





- [x] 4.1 Remove dead `_chunk_name` (frame-based) function and `ChunkInfo` dataclass from `chunking.py`


  - Delete `_chunk_name` function entirely
  - Delete `ChunkInfo` dataclass from `chunking.py`
  - Remove `_CHUNK_NAME_PATTERN` local variable; import `CHUNK_NAME_PATTERN` from `constants`
  - Update `ChunkingResult.chunks` type to `list[ChunkMetadata]`
  - _Requirements: 3.1, 3.4, 3.5_

- [x] 4.2 Update `split_chunks_from_state` to produce `ChunkMetadata` with timestamps

  - Assign `chunk_id` from `_chunk_name_duration(start_ts, end_ts)` before creating the file
  - Create `ChunkMetadata(path=chunk_file, chunk_id=stem, start_timestamp=start_ts, end_timestamp=end_ts)`
  - Call `chunk_meta.populate_from_ffmpeg_output(proc.stderr.splitlines())` after ffmpeg split to fill duration/resolution without extra probe
  - Replace `get_frame_count` probe call with the ffmpeg-output population
  - Update resume detection to use `CHUNK_NAME_PATTERN` from `constants`
  - Update `tracker.update_chunk_metadata` call to pass `ChunkMetadata`
  - _Requirements: 3.1, 3.2, 3.3, 2.8_

- [x] 4.3 Remove stateless chunking path and make tracker required

  - Remove `_chunk_video_stateless` function
  - Remove `tracker: ProgressTracker | None = None` optional; make `tracker: ProgressTracker` required in `chunk_video` and `split_chunks_from_state`
  - Remove the stateless fallback branch in `chunk_video`
  - Update `_chunk_video_tracked` to return `ChunkingResult` with `list[ChunkMetadata]` directly (no `ChunkInfo` conversion)
  - _Requirements: 3.5_

- [x] 5. Update encoding phase to use `AttemptMetadata`, CRF-only filenames, and filesystem-based recovery





- [x] 5.1 Remove `ChunkInfo` dataclass from `encoding.py` and update function signatures


  - Delete `ChunkInfo` dataclass from `encoding.py`
  - Import `ChunkMetadata` from `models` and use it everywhere `ChunkInfo` was used
  - Update `encode_all_chunks` and `find_optimal_strategy` signatures to accept `list[ChunkMetadata]`
  - _Requirements: 1.1, 1.3_

- [x] 5.2 Implement CRF-only attempt filename and temp-file rename protocol


  - Update `_get_attempt_path` to produce `<chunk_id>.<resolution>.crf<CRF>.mkv` with no attempt number
  - In `_encode_with_ffmpeg`, write to `<final_stem>.tmp` first; add `-f matroska` to ffmpeg command; rename to final name only on success (exit code 0 and non-empty file)
  - At the start of `encode_all_chunks`, scan all strategy directories for `*.tmp` files, log a warning for each, and delete them
  - _Requirements: 4.1, 4.2, 4.5_

- [x] 5.3 Replace tracker-based existence check with filesystem scan

  - Rewrite `_check_existing_encoding` to scan the strategy output directory using `ENCODED_ATTEMPT_NAME_PATTERN` for a file matching `chunk_id`, `resolution`, and `crf`
  - Return `AttemptMetadata` on match (parse all fields from filename + `path.stat().st_size`)
  - Remove all `progress_tracker.get_chunk_state` / `get_successful_crf_average` calls from encoding
  - Derive CRF seed by scanning existing sidecar `.metrics.json` files in the strategy directory instead
  - Update `ChunkEncodingResult.encoded_file` to hold `AttemptMetadata | None`
  - _Requirements: 4.3, 4.4, 10.1_

- [x] 5.4 Write metrics sidecar and implement sidecar-based recovery with info-level reuse messages

  - After `quality_evaluator.evaluate_chunk` returns, write `<stem>.metrics.json` atomically (write to `.tmp`, rename) containing `targets_met`, `crf`, and the metrics dict keyed by `<metric>_<statistic>`
  - On recovery: if sidecar exists and contains all required metric keys, read it and log an info-level message identifying the reused sidecar by filename and CRF — do NOT re-run quality evaluation
  - On recovery: if attempt `.mkv` exists but sidecar is missing or incomplete, re-run quality evaluation and write/overwrite the sidecar — do NOT re-encode
  - When reusing an existing attempt `.mkv` (before sidecar check), log an info-level message identifying the file by name and CRF value
  - Use the sidecar `crf` field (not the filename) as the authoritative CRF when reconstructing attempt history
  - _Requirements: 4.4, 10.1, 10.2, 10.3, 10.4, 10.5_

- [x] 6. Update `progress.py` to remove attempt-history models and rename chunk metadata





- [x] 6.1 Remove `ChunkState`, `StrategyState`, `AttemptInfo`, `ChunkUpdate` models and related tracker methods


  - Remove `ChunkState`, `StrategyState`, `AttemptInfo`, `ChunkUpdate` from `models.py`
  - Remove `update_chunk`, `get_chunk_state`, `get_successful_crf_average` methods from `ProgressTracker`
  - Remove `PipelineState.chunks: dict[str, ChunkState]` field
  - _Requirements: 1.1_

- [x] 6.2 Rename `chunks_metadata` to `chunks` in `PipelineState` and update serialisation



  - Rename `PipelineState.chunks_metadata: dict[str, ChunkVideoMetadata]` → `chunks: dict[str, ChunkMetadata]`
  - Rename `update_chunk_metadata` → `update_chunk`; rename `get_chunk_metadata` → `get_chunk`; update return types to `ChunkMetadata`
  - Update `_serialize_state` and `_deserialize_state` to use `chunks` key and `ChunkMetadata` (including `start_timestamp`, `end_timestamp`, `crop_params`)
  - Update `_serialize_state` to include `crop_params` in `source_video` serialisation via `model_dump_full`
  - Remove `PhaseMetadata.crop_params` field; update `_deserialize_state` to no longer read it
  - _Requirements: 2.2, 1.1_

- [x] 7. Update orchestrator to read `crop_params` from `source_video` and pass `optimal_strategy` to merge





- [x] 7.1 Replace `_resolve_crop_params` with direct read from `source_video.crop_params`


  - Replace `_resolve_crop_params` method with `_get_crop_params` that reads `tracker._state.source_video.crop_params`
  - In `_execute_extraction`: store `result.video` as `state.source_video` (which carries `crop_params`); remove the separate `PhaseMetadata(crop_params=...)` update
  - Update `_execute_encoding` and `_execute_optimization` to call `self._get_crop_params()`
  - _Requirements: 2.3_

- [x] 7.2 Pass `optimal_strategy` to merge phase and update chunk collection


  - In `_execute_merge`, pass `optimal_strategy=self._optimal_strategy` to `merge_final_video`
  - Update chunk collection in `_execute_encoding` to use `ChunkMetadata` objects from `tracker._state.chunks` instead of bare `ChunkInfo`
  - Update audio file collection to use `list[AudioMetadata]` from extraction result stored in state
  - _Requirements: 5.1, 5.2_

- [x] 7.3 Implement work-directory source-file binding check (Req 8)


  - On `load_state`, compare `state.source_video` filename, `file_size_bytes`, and `resolution` against `config.source_video`
  - In dry-run mode + mismatch: log warning describing each differing field; return without further action
  - In execute mode + mismatch + no `--force`: log critical; abort pipeline
  - In execute mode + `--force`: log warning; delete all artifacts in work dir; reset state; continue
  - Add `--force` CLI flag in `cli.py` (distinct from `-y`); pass it through `PipelineConfig`
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

- [x] 8. Update merge phase to respect `optimal_strategy` and sort chunks correctly




  - Add `optimal_strategy: str | None = None` parameter to `merge_final_video`
  - When `optimal_strategy` is set, filter `strategies_to_merge` to only that strategy
  - Sort chunks by filename (lexicographic, which encodes start timestamp) before concatenation
  - Log chunk count and total frame count at info level before each strategy merge
  - Change frame count mismatch from abort to warning-and-continue
  - Pass `source_video: VideoMetadata` (not `Path`) and use `source_video.crop_params` as `crop_reference` in `QualityEvaluator.evaluate_chunk`
  - Save final metrics output to `final_metrics_<safe_strategy>/` subdirectory; make metrics failure non-fatal
  - Use `config.subsample_factor` for final metrics measurement
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 9. Add `alive_bar` progress display to `QualityEvaluator.evaluate_chunk` (Req 9)





  - Launch ffmpeg with `-progress pipe:1`; read `out_time_ms` asynchronously to advance an `alive_bar`
  - Set bar total to `num_metric_passes * duration_seconds`; show indeterminate spinner when `duration_seconds` is unavailable
  - Add shared `_drain_stderr` async coroutine that reads stderr in raw chunks (splitting on `\r` and `\n`) to prevent pipe blocking; reuse for all ffmpeg subprocess calls
  - Force bar to completion on process exit regardless of whether estimated total was reached
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 10. Wire `CropParams` change detection through orchestrator (Req 2.7)





  - When loading state, compare `state.source_video.crop_params` against the crop that would be applied (from config or detection)
  - If changed: log warning; clear `state.phases["chunking"].metadata.scene_boundaries`; mark all chunk encoding states as stale (delete sidecar files or flag in state)
  - _Requirements: 2.7_

- [x] 11. Write unit tests for core refactored logic





  - Test `ChunkMetadata` serialisation round-trip (including `start_timestamp`, `end_timestamp`, `crop_params`)
  - Test `_chunk_name_duration` output matches `CHUNK_NAME_PATTERN`
  - Test `ENCODED_ATTEMPT_NAME_PATTERN` correctly parses filenames produced by the new naming scheme
  - Test `CropParams.to_ffmpeg_filter()` for zero and non-zero values
  - Test work-directory source-file mismatch detection (dry-run, execute, force modes)
  - Test merge strategy selection: optimal-only vs all-strategies
  - _Requirements: 1.1, 3.1, 4.1, 2.4, 8.2, 5.1_
