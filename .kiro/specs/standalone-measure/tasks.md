# Implementation Plan: Standalone Measure Command

<!-- markdownlint-disable MD024 -->

- Created: 2025-07-17
- Completed:

## Overview

Implement the `measure` subcommand end-to-end. New module `pyqenc/phases/measure.py` contains all logic. `cli.py` is refactored to split `_add_common_arguments` and gains `_create_measure_subcommand` + `_cmd_measure`. `api.py` gains `measure_quality`. `constants.py` gains measure-specific constants. `fmt_key_value_table` gains list-value support.

## Tasks

- [x] 1. Add measure constants to `pyqenc/constants.py`
  - Add `MEASURE_DIR`, `METRICS_SUBDIR_SUFFIX`, `SCREENSHOTS_SUBDIR_SUFFIX`, `SCREENSHOT_TIMESTAMP_FMT`, `DEFAULT_SCREENSHOT_COUNT`, `DEFAULT_METRICS_SAMPLING`
  - No imports from the module (constants.py must remain import-free from the project)
  - _Requirements: 3.1, 7.5_

- [x] 2. Update `fmt_key_value_table` in `pyqenc/utils/log_format.py`
  - Update signature and body to support `list` values with vertical alignment
  - Check `isinstance(value, str)` first (before list check) to avoid str-as-iterable bug
  - Continuation lines use blank key column aligned to value column
  - All existing callers passing `str` values remain unaffected
  - _Requirements: 11.1, 11.2_

- [x] 3. Refactor CLI argument helpers in `pyqenc/cli.py`
  - [x] 3.1 Split `_add_common_arguments` into `_add_base_arguments` (universal: `--work-dir`, `--log-level`) and `_add_pipeline_arguments` (phases only: `-y`/`--execute`, `--cleanup`, `--force`, `--no-metrics`)
    - All existing phase subcommands call both helpers to preserve current behaviour
    - _Requirements: 1.4_
  - [x] 3.2 Extract `_resolve_crop_params(args) -> CropParams | None` helper from inline logic in `_cmd_auto`
    - Returns explicit `CropParams` when `--crop` or `--no-crop` given; `None` as sentinel for auto-resolve
    - Raises `ValueError` on bad `--crop` format
    - Update `_cmd_auto` and `_cmd_extract` to use this helper
    - _Requirements: 2.1, 2.2_

- [x] 4. Add `_create_measure_subcommand` and `_cmd_measure` to `pyqenc/cli.py`
  - [x] 4.1 Implement `_create_measure_subcommand(subparsers)` registering the `measure` subparser
    - Positional `source` (Path) with help warning about argument order
    - Positional `targets` (Path, `nargs='*'`, default `[]`) with screenshots-only mode hint
    - Calls `_add_base_arguments` only (no `_add_pipeline_arguments`)
    - Calls `_add_crop_arguments` for `--crop`/`--no-crop` mutually exclusive group
    - `--metrics-sampling N` (int, default `DEFAULT_METRICS_SAMPLING`)
    - `--width W` (int, optional)
    - `--screenshots N` (int, default `DEFAULT_SCREENSHOT_COUNT`, min 1)
    - `--every DURATION` (str, optional)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 1.11_
  - [x] 4.2 Implement `_cmd_measure(args) -> int`
    - Calls `_resolve_crop_params(args)`, then `measure_quality(...)` from `pyqenc.api`
    - Catches `FileNotFoundError`, `ValueError`, and generic `Exception`; logs critical and returns 1
    - _Requirements: 1.1, 2.1, 2.2, 11.3, 11.4, 11.5, 11.6_

- [x] 5. Create `pyqenc/phases/measure.py` — data models and helpers
  - [x] 5.1 Define `TargetMeasureResult` and `MeasureResult` dataclasses
    - `TargetMeasureResult`: `target_video`, `graph`, `sidecar`, `screenshots_dir`, `metrics`
    - `MeasureResult`: `source_screenshots_dir`, `targets: list[TargetMeasureResult]`
    - _Requirements: 8.3_
  - [x] 5.2 Implement `_parse_duration(value: str) -> float`
    - Accepts plain int/float and human-friendly strings: `30s`, `5m`, `1h`, `1h30m`, `1h30m45s`
    - Raises `ValueError` on invalid input
    - _Requirements: 1.7_
  - [x] 5.3 Implement `_screenshot_timestamps_count(duration: float, count: int) -> list[float]`
    - `step = duration / (count + 1)`; returns `[step, 2*step, ..., count*step]`
    - Filters out any timestamp `>= duration`
    - _Requirements: 7.2_
  - [x] 5.4 Write property test for `_screenshot_timestamps_count` (Property 1)
    - **Property 1: Screenshot timestamp distribution**
    - **Validates: Requirements 7.2**
    - For any `duration > 0` and `count >= 1`: result has exactly `count` values, all in `(0, duration)`, evenly spaced with step `duration / (count + 1)`
    - _Requirements: 7.2_
  - [x] 5.5 Implement `_screenshot_timestamps_interval(duration: float, interval_s: float) -> list[float]`
    - Returns `[interval_s, 2*interval_s, ...]` up to (exclusive) `duration`
    - Returns empty list if `interval_s >= duration`
    - _Requirements: 7.2_
  - [x] 5.6 Implement `_screenshot_filename(timestamp_s: float, video_stem: str) -> str`
    - Zero-padded `HH꞉MM꞉SS․mmm_stem.png` using `TIME_SEPARATOR_SAFE` and `TIME_SEPARATOR_MS`
    - _Requirements: 7.5_
  - [x] 5.7 Write property test for `_screenshot_filename` sort order (Property 2)
    - **Property 2: Screenshot filename sort order**
    - **Validates: Requirements 7.5**
    - For any list of timestamps, lexicographic sort of filenames matches numeric sort of timestamps
    - _Requirements: 7.5_
  - [x] 5.8 Write unit tests for `_parse_duration`, `_screenshot_timestamps_count`, `_screenshot_timestamps_interval`, `_screenshot_filename`
    - `_parse_duration`: plain int/float, all human-friendly formats, invalid input raises `ValueError`
    - `_screenshot_filename`: correct zero-padding, correct separators, example `3723.456 → 01꞉02꞉03․456_stem.png`
    - _Requirements: 1.7, 7.2, 7.5_

- [x] 6. Implement crop resolution and resolution validation helpers in `pyqenc/phases/measure.py`
  - [x] 6.1 Implement `_resolve_crop(crop_params: CropParams | None, work_dir: Path, source_video: Path) -> CropParams`
    - If `crop_params` is a `CropParams` instance: return it directly
    - If `None`: attempt `JobState.load(work_dir / "job.yaml")`; use crop if source matches; otherwise return empty `CropParams` and log info
    - Never writes or modifies `job.yaml`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - [x] 6.2 Write unit tests for `_resolve_crop`
    - Explicit `CropParams` returned unchanged
    - `None` with no `job.yaml` returns empty `CropParams` and logs info
    - `None` with matching `job.yaml` returns crop from file
    - `None` with non-matching source in `job.yaml` returns empty `CropParams`
    - _Requirements: 2.1, 2.2, 2.3, 2.4_
  - [x] 6.3 Implement `_check_resolution_match(source_meta, target_meta, crop_params, width) -> None`
    - Computes effective post-crop, post-scale dimensions for both videos
    - Raises `ValueError` with actionable suggestion (`--crop TOP BOTTOM [--width W]`) on mismatch
    - Suggestion includes final effective resolution both videos would be scaled to
    - _Requirements: 3c.1, 3c.2, 3c.3, 3c.4, 3c.5_

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement metric computation and sidecar writing in `pyqenc/phases/measure.py`
  - [x] 8.1 Implement `_run_metrics(source_video, target_video, crop_params, width, metrics_dir, graph_path, subsample_factor) -> ChunkQualityStats`
    - Delegates to `QualityEvaluator(measure_dir).evaluate_chunk(...)` with `targets=[]`
    - Passes `width` through to `run_metric` via existing `width` parameter
    - Returns `evaluation.metrics`
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 4.6, 6.1, 6.2, 6.3_
  - [x] 8.2 Implement `_write_sidecar(path, source_video, target_video, subsample_factor, crop_params, metrics, source_duration_seconds, target_duration_seconds, effective_duration_seconds) -> None`
    - Writes YAML to `path` using `.tmp`-then-rename atomic protocol via `write_yaml_atomic`
    - Includes all fields from design: paths, durations, `subsample_factor`, `crop_params` dict, `metrics` stats
    - On write failure: log warning, do not raise
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 3b.6_
  - [x] 8.3 Write unit test for `_write_sidecar` failure handling
    - Mock `write_yaml_atomic` to raise `OSError`; assert warning is logged and no exception propagates
    - _Requirements: 5.7_

- [ ] 9. Implement screenshot capture in `pyqenc/phases/measure.py`
  - [ ] 9.1 Implement `_capture_screenshots(video_path, timestamps_s, screenshots_dir, crop_params, fps, has_timestamps) -> list[Path]`
    - Single ffmpeg pass using `select` filter (no fast-seek)
    - Primary mode: timestamp-based `select='eq(t,T1)+eq(t,T2)+...'`
    - Fallback mode: frame-number-based `select='eq(n,F1)+eq(n,F2)+...'` when `has_timestamps=False` and `fps` known
    - Crop applied in filter chain; no scaling
    - Uses `-vsync 0`; output as `%04d.png` then renamed to `<HH꞉MM꞉SS․mmm>_<stem>.png`
    - `.tmp`-then-rename protocol for final named files
    - All ffmpeg calls via `run_ffmpeg_async` from `pyqenc/utils/ffmpeg_runner.py`
    - On individual frame failure: log warning, continue
    - _Requirements: 7.3, 7.4, 7.5, 7.6, 7.8, 7.9_

- [ ] 10. Implement `run_measure` top-level async entry point in `pyqenc/phases/measure.py`
  - [ ] 10.1 Implement input validation and crop resolution
    - Raise `FileNotFoundError` if `source_video` missing or any path in `target_videos` missing
    - Raise `ValueError` if `metrics_sampling < 1` or `screenshot_count < 1`
    - Call `_resolve_crop(crop_params, work_dir, source_video)`
    - _Requirements: 8.5, 8.6, 11.1, 11.2, 11.3, 11.4, 11.5_
  - [ ] 10.2 Implement resolution validation and parameter summary logging
    - Probe resolutions of source and all targets upfront
    - Call `_check_resolution_match` for each target; stop entirely if any fails
    - Log key-value parameter summary via `fmt_key_value_table`: source, target count, target stems (list), crop, width, screenshot mode
    - _Requirements: 3c.1, 3c.2, 3c.3, 11.1, 11.2, 11.3_
  - [ ] 10.3 Implement directory creation and duration probing
    - Create `measure_dir`, per-target `metrics_dir`, per-target `target_screenshots_dir`, `source_screenshots_dir`
    - Probe durations of source and all targets; compute `effective_duration[t] = min(source_duration, target_duration)` per target
    - Compute `shared_duration = min(effective_duration[t] for all t)` for screenshot timestamps
    - Log warning when durations differ by >1s; handle unavailable durations per design error table
    - _Requirements: 3.1, 3.6, 3b.1, 3b.2, 3b.3, 3b.4, 3b.5_
  - [ ] 10.4 Implement screenshot timestamp computation and source screenshot capture
    - Compute shared timestamps using `_screenshot_timestamps_count` or `_screenshot_timestamps_interval` from `shared_duration`
    - Log warning when fewer timestamps fit than requested; log error and skip if timestamp set is empty
    - Capture source screenshots once using shared timestamps → `source_screenshots_dir`
    - Log info when running in screenshots-only mode (no targets)
    - _Requirements: 7.1, 7.2, 7.7, 7.10, 7.11, 7.12, 1b.1, 1b.2, 1b.3, 1b.4, 1b.5_
  - [ ] 10.5 Implement per-target loop: metrics, sidecar, screenshots
    - For each target sequentially: run metrics via `_run_metrics`, write sidecar via `_write_sidecar`, capture target screenshots via `_capture_screenshots`
    - Pass `Effective_Duration` to metric computation when available
    - Log summary at info level: metrics per target, total screenshot count
    - Return `MeasureResult`
    - _Requirements: 4.1, 4.4, 5.1, 7.1, 7.6, 7.12, 3b.5, 1b.6, 1b.7_

- [ ] 11. Add `measure_quality` to `pyqenc/api.py`
  - Implement `measure_quality(source_video, target_videos, work_dir, crop_params, metrics_sampling, screenshot_count, screenshot_interval) -> MeasureResult`
  - Delegates to `asyncio.run(run_measure(...))` from `pyqenc.phases.measure`
  - Complete docstring with all parameters, return value, and raised exceptions
  - Raises `FileNotFoundError` if source or any target does not exist
  - Raises `ValueError` if `metrics_sampling < 1` or `screenshot_count < 1`
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

- [ ] 12. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Review spec against other specs and update cross-spec summaries
  - Compare `standalone-measure` spec dates and content against other specs in `.kiro/specs/` (especially `pipeline-metrics-report` and `project-cleanup`)
  - Add a summary section to the top of this spec and any related specs noting what was superseded or changed
  - _Requirements: (agent-specs.md rule)_

- [ ] 14. Update completed date
  - Set `- Completed: <ISO date>` in the header of this spec once all tasks are done
  - _Requirements: (agent-specs.md rule)_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Property tests use `hypothesis` (already in project test deps)
- Property tests live in `tests/test_measure_properties.py`; unit tests in `tests/test_measure.py`
- All ffmpeg calls go through `run_ffmpeg_async` — never raw subprocess
- `_add_common_arguments` callers must be updated to call both `_add_base_arguments` + `_add_pipeline_arguments`
- Screenshots-only mode (no targets): skips metrics, graph, sidecar; captures source screenshots only
- `--width` applies only to metric computation, never to screenshots
- No dry-run, no skip-if-exists — always overwrites
