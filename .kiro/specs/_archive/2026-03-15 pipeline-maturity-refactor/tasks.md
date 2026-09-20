# Implementation Plan

- [x] 1. Add pydantic dependency and convert all models to Pydantic BaseModel

  - Add `pydantic>=2.0` to `pyproject.toml` dependencies
  - Convert every `@dataclass` in `pyqenc/models.py` to `pydantic.BaseModel`: `PhaseStatus` stays as `Enum`, all others become `BaseModel`
  - Replace `dict[str, Any]` fields with explicit typed models: introduce `SceneBoundary(frame, timestamp_seconds)` and `PhaseMetadata(crop_params, scene_boundaries)` Pydantic models
  - Introduce `PhaseUpdate(phase, status, metadata)` and `ChunkUpdate(chunk_id, strategy, attempt)` Pydantic models
  - Remove `from typing import Any` usages where replaced by typed models
  - _Requirements: 1.1, 1.2, 2.1, 2.2_

- [x] 2. Implement VideoMetadata with lazy-loading and ChunkVideoMetadata

- [x] 2.1 Implement VideoMetadata class with lazy properties

  - Replace the existing partial `VideoMetadata` class in `pyqenc/models.py` with a full Pydantic `BaseModel` implementation
  - Use `PrivateAttr` for backing fields `_duration_seconds`, `_frame_count`, `_fps`, `_resolution`
  - Implement `start_frame: int = 0` as a regular field
  - Implement `duration_seconds`, `fps`, `resolution` as `@property` methods backed by a single fast `_probe_metadata()` call (`ffprobe -show_streams -show_format`, ~175ms) that populates all three at once
  - Implement `frame_count` as a `@property` backed by `_probe_frame_count()` (`ffmpeg -c copy -f null`, ~2-3s); this run also fills duration/fps/resolution from stderr if still `None`
  - Implement `populate_from_ffprobe(data: dict)` to fill backing fields from a pre-parsed ffprobe JSON dict without triggering a probe
  - Implement `populate_from_ffmpeg_output(stderr_lines: list[str])` to parse duration/fps/resolution from ffmpeg stderr
  - Implement `model_dump_full() -> dict` that includes cached private fields for round-trip persistence
  - If a field cannot be determined, store `None` and log a warning
  - _Requirements: 1.1, 1.3, 1.4, 1.9_

- [x] 2.2 Implement ChunkVideoMetadata class

  - Define `ChunkVideoMetadata(VideoMetadata)` with `chunk_id: str` field
  - `chunk_id` is derived from the frame-range name (e.g. `chunk.000000-000319`)
  - _Requirements: 1.2_

- [x] 2.3 Remove SourceVideoMetadata and ChunkMetadata

  - Delete `SourceVideoMetadata` and `ChunkMetadata` dataclasses from `pyqenc/models.py`
  - Update `PipelineState` to use `source_video: VideoMetadata` and `chunks_metadata: dict[str, ChunkVideoMetadata]`
  - _Requirements: 1.7, 1.8_

- [x] 2.4 Write unit tests for VideoMetadata lazy-loading

  - Verify `_probe_metadata()` is called exactly once even when duration, fps, and resolution are all accessed
  - Verify `_probe_frame_count()` is called exactly once even when frame_count is accessed multiple times
  - Verify accessing only `fps` does NOT trigger `_probe_frame_count()`
  - Verify `populate_from_ffprobe` fills fields without triggering any probe
  - Verify `model_dump_full()` / `model_validate()` round-trip preserves all cached fields
  - _Requirements: 1.1, 1.3, 1.4_

- [x] 3. Update ProgressTracker to use typed models and opportunistic population

- [x] 3.1 Update ProgressTracker serialization to use VideoMetadata

  - Replace `_serialize_state` / `_deserialize_state` to use `PipelineState.model_dump()` / `PipelineState.model_validate()`
  - Use `VideoMetadata.model_dump_full()` for source_video serialization so cached probe fields survive round-trips
  - On deserialize, pre-fill `VideoMetadata` backing fields from the persisted dict (bypassing lazy-load)
  - _Requirements: 1.8_

- [x] 3.2 Add source file change detection on resume

  - After loading existing `progress.json`, construct a fresh `VideoMetadata` for the source file and compare persisted fields
  - Log a warning for any field that differs; flag affected chunks as NOT_STARTED
  - _Requirements: 1.6_

- [x] 3.3 Update update_phase and update_chunk to accept typed objects

  - Change `update_phase` signature to accept `PhaseUpdate` as primary argument
  - Change `update_chunk` signature to accept `ChunkUpdate` as primary argument
  - Update all call sites in `pyqenc/orchestrator.py`, `pyqenc/phases/chunking.py`, `pyqenc/phases/encoding.py`, `pyqenc/phases/extraction.py`
  - _Requirements: 2.3, 2.4, 2.5_

- [x] 3.4 Update ffmpeg helpers to opportunistically populate VideoMetadata

  - In `pyqenc/utils/ffmpeg.py` `get_frame_count`, call `video_meta.populate_from_ffprobe(data)` after the ffprobe run
  - In `pyqenc/utils/ffmpeg_wrapper.py` `run_ffmpeg_with_progress`, call `video_meta.populate_from_ffmpeg_output(stderr_lines)` if a `VideoMetadata` instance is passed
  - _Requirements: 1.4, 1.5_

- [x] 3.5 Write unit tests for ProgressTracker typed updates

  - Verify `PhaseUpdate` and `ChunkUpdate` round-trip through serialization without data loss
  - Verify source file change detection logs a warning when a field differs
  - _Requirements: 2.5, 1.6_

- [x] 4. Implement crash-safe ProgressTracker flushing

  - In `ProgressTracker.__init__`, call `_register_flush_handlers()`
  - `_register_flush_handlers` registers `atexit.register(self.flush)`, `signal.signal(SIGINT, ...)`, `signal.signal(SIGTERM, ...)`
  - On Windows, also handle `signal.CTRL_C_EVENT` and `signal.CTRL_BREAK_EVENT`
  - Signal handler calls `self.flush()`, logs a warning, then re-raises / exits
  - In `pyqenc/cli.py` top-level entry point, wrap the pipeline call in `try/finally` that calls `tracker.flush()`
  - Ensure `flush()` is idempotent: returns immediately if `_pending_updates == 0`
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 5. Implement frame-range chunk naming

  - In `pyqenc/phases/chunking.py`, implement `_chunk_name(start_frame: int, end_frame: int) -> str` returning `chunk.{start:06d}-{end:06d}`
  - Update chunk file creation to use `_chunk_name` instead of sequential counters
  - Update `ChunkVideoMetadata.chunk_id` to be set from the chunk file stem
  - Update encoded attempt file naming to `chunk.<start>-<end>.<width>x<height>.attempt_<N>.crf<CRF>.mkv` in `pyqenc/phases/encoding.py`
  - Update resumption logic to match existing files by frame-range pattern rather than sequential index
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

- [x] 6. Split chunking into two independently resumable sub-phases

- [x] 6.1 Implement detect_scenes_to_state

  - Extract scene detection logic from `chunk_video` into `detect_scenes_to_state(video_meta, tracker, scene_threshold, min_scene_length) -> list[SceneBoundary]`
  - Use PySceneDetect `ContentDetector`; consult `ref/Av1an/` if detection produces incorrect results
  - On zero scenes detected, log a warning and return a single `SceneBoundary(frame=0, timestamp_seconds=0.0)`
  - Persist the boundary list into `PipelineState.phases["chunking"].metadata.scene_boundaries` via `tracker.update_phase(PhaseUpdate(...))`
  - Log scene count at info level
  - _Requirements: 4.1, 4.2, 5.1, 5.5_

- [x] 6.2 Implement split_chunks_from_state

  - Extract splitting logic from `chunk_video` into `split_chunks_from_state(video_meta, output_dir, tracker, crop_params) -> list[ChunkVideoMetadata]`
  - Read `scene_boundaries` from `PipelineState.phases["chunking"].metadata`
  - For each boundary pair: derive frame-range name, skip if already in `chunks_metadata`, call ffmpeg with exact timestamps
  - Verify each output file exists and is non-empty; log critical and mark FAILED if not
  - Call `tracker.update_chunk_metadata(chunk_meta)` immediately after each successful split
  - When crop is applied, pass as `-vf` argument; log output dimensions
  - Log chunk count at info level
  - _Requirements: 4.1, 4.4, 4.5, 5.2, 5.3, 5.4, 5.5_

- [x] 6.3 Update chunk_video orchestrator entry point
  - Rewrite `chunk_video` to call `detect_scenes_to_state` only if `scene_boundaries` not already in state, then call `split_chunks_from_state`
  - _Requirements: 4.3, 4.5_

- [x] 6.4 Write unit tests for two-phase chunking

  - Test `detect_scenes_to_state` with a mock detector returning zero scenes: verify single-boundary fallback
  - Test `split_chunks_from_state` resumption: verify already-recorded chunks are skipped
  - Test frame-range naming: verify 6-digit padding and correct inclusive range
  - _Requirements: 4.2, 4.5, 5.1, 7.1, 7.2_

- [x] 7. Migrate legacy/pymkvextract into extraction.py

  - Copy `StreamBase`, `VideoStream`, `AudioStream`, `SubtitleStream`, `AttachmentStream`, `ChaptersStream`, `TagsStream`, `HeadersStream`, `StreamFactory`, `MKVTrackExtractor`, `streams_filter_plain_regex`, `sanitize_filename` from `pyqenc/legacy/pymkvextract/main.py` directly into `pyqenc/phases/extraction.py`
  - Remove the `from pyqenc.legacy.pymkvextract.main import ...` import
  - Verify `extract_streams` still works end-to-end
  - _Requirements: 6.1_

- [x] 8. Migrate legacy/pymkvcompare into quality.py

  - Copy `MetricType`, `run_metric` from `pyqenc/legacy/pymkvcompare/metrics.py` and supporting helpers from `processes.py` / `video.py` into `pyqenc/quality.py`
  - Remove `from pyqenc.legacy.pymkvcompare.*` imports from `pyqenc/quality.py`
  - Remove the `PyMKVCompareCropParams` alias; use `pyqenc.models.CropParams` directly
  - _Requirements: 6.2_

- [x] 9. Migrate legacy/metrics_visualization into utils/visualization.py

  - Create `pyqenc/utils/visualization.py`
  - Copy `analyze_chunk_quality`, `ChunkQualityStats`, `MetricStats`, parsers (`parse_psnr_file`, `parse_ssim_file`, `parse_vmaf_file`), `compute_statistics`, `create_unified_plot` from the legacy module into the new file
  - Remove `from pyqenc.legacy.metrics_visualization.*` imports from `pyqenc/quality.py`
  - _Requirements: 6.4_

- [x] 10. Migrate legacy/pymkva2 into phases/audio.py

  - Copy `AudioEngine`, `BaseStrategy`, `ConversionStrategy`, `DownmixStrategy`, `TrueNormalizeStrategy`, `Task`, `SynchronousRunner` from `pyqenc/legacy/pymkva2/` into `pyqenc/phases/audio.py`
  - Remove `from pyqenc.legacy.pymkva2 import ...` imports
  - _Requirements: 6.3_

- [x] 11. Delete legacy directory and verify no remaining imports

  - Delete the entire `pyqenc/legacy/` directory tree
  - Run a codebase-wide search for any remaining `pyqenc.legacy` import strings and fix them
  - _Requirements: 6.5, 6.6_

- [x] 11.1 Import smoke test

  - Add a test that imports every public module in `pyqenc` and asserts no `pyqenc.legacy` reference exists in `sys.modules`
  - _Requirements: 6.6_
