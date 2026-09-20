# Implementation Plan: Measure Phase UX Overhaul
<!-- markdownlint-disable MD024 -->

## Overview

Refactor `pyqenc/phases/measure.py` to use exact-integer frame arithmetic for screenshot positions, replace the single-pass `select` filter with Strategy C (N fast-seek calls) + A2/A4 fallback chain, restructure `run_measure` to capture all screenshots before metrics, add a summary table, and clean up the public API surface.

## Tasks

- [x] 1. Add `fps_fraction` property to `VideoMetadata` in `pyqenc/models.py`
  - Add `_fps_fraction: Fraction | None = PrivateAttr(default=None)` backing field
  - Add `fps_fraction` lazy property that calls `_probe_metadata()` on first access
  - Extend `populate_from_ffprobe` to parse `avg_frame_rate` (e.g. `"24000/1001"`) into `_fps_fraction` using `Fraction(num, den)` — exact integer arithmetic, no float conversion; guard against zero denominator
  - Add `from fractions import Fraction` import
  - Extend `model_dump_full` / `model_validate_full` to round-trip `_fps_fraction` (serialize as `[num, den]` pair or skip if `None`)
  - _Requirements: 3.1, 3.6, 6.6_

- [x] 2. Add `ScreenshotPositions` dataclass to `pyqenc/phases/measure.py`
  - Add `@dataclass(frozen=True) class ScreenshotPositions` with fields `frame_nums: list[int]`, `fps: Fraction`, `step: int`
  - Implement `seek_ts(frame_num: int) -> str`: `(frame_num * 4 - 1) / (4 * fps)` serialized to 9 decimal places (guarantees seek lands before target frame)
  - Implement `filename_ts(frame_num: int) -> str`: `frame_num / fps` as `Fraction`, formatted to `HH꞉MM꞉SS․mmm` using `TIME_SEPARATOR_SAFE` and `TIME_SEPARATOR_MS`
  - Add `from fractions import Fraction` import
  - _Requirements: 3.6, 6.6, 6.7_

  - [ ]* 2.1 Write property test for `seek_ts` always-before guarantee
    - **Property 1 (partial): `seek_ts` result is always strictly before `frame_num / fps`**
    - **Validates: Requirements 6.1, 6.6**
    - Place in `tests/test_measure_properties.py`

  - [ ]* 2.2 Write property test for `filename_ts` format
    - **Property 3: Screenshot filename format**
    - **Validates: Requirements 6.7**
    - Assert output matches `r'^\d{2}꞉\d{2}꞉\d{2}․\d{3}$'`

- [x] 3. Implement `compute_screenshot_positions` in `pyqenc/phases/measure.py`
  - Signature: `def compute_screenshot_positions(total_frames: int, fps: Fraction, count: int, include_edges: bool = False) -> ScreenshotPositions`
  - `step = total_frames // (count + 1)`; interior positions `[step, 2*step, ..., count*step]`
  - When `include_edges=True`: prepend `0`, append `total_frames - 1`
  - Return `ScreenshotPositions(frame_nums=..., fps=fps, step=step)`
  - _Requirements: 3.1, 3.2, 3.3, 3.6, 6.6_

  - [ ]* 3.1 Write property test for position formula
    - **Property 1: Screenshot position formula**
    - **Validates: Requirements 3.2, 6.6**
    - `@given(total_frames, count, fps)` — assert `frame_nums == [i * step for i in range(1, count+1)]`

  - [ ]* 3.2 Write property test for position determinism
    - **Property 2: Position determinism**
    - **Validates: Requirements 3.6**
    - Call twice with same args, assert equal

- [x] 4. Implement `make_screenshots` public function in `pyqenc/phases/measure.py`
  - Signature: `async def make_screenshots(video_path: Path, positions: ScreenshotPositions, screenshots_dir: Path, crop_params: CropParams | None = None) -> list[Path]`
  - **Strategy C (primary)**: for each `frame_num` in `positions.frame_nums`, run `ffmpeg -ss <seek_ts> -i <video> [-vf crop] -frames:v 1 <filename_ts>_<stem>.png` via `run_ffmpeg_async`; collect output paths; use `.tmp`-then-rename protocol for each file
  - **Strategy A2 (fallback)**: if Strategy C yields zero files, log WARNING and run single-pass `ffmpeg -i <video> -vf select='eq(n,F1)+eq(n,F2)+...'[-,crop] -vsync 0 %04d.png` into a temp dir, then rename using `filename_ts`
  - **Strategy A4 (fallback)**: if Strategy A2 yields zero files, log WARNING and run single-pass `ffmpeg -i <video> -vf select='not(mod(n,step))*gt(n,0)'[,crop] -vsync 0 %04d.png` into a temp dir, then rename
  - If all strategies yield zero files: log ERROR and return `[]`
  - Partial results (fewer files than positions) are acceptable — do NOT trigger fallback
  - All ffmpeg calls via `run_ffmpeg_async`; no bare subprocess calls
  - Write complete docstring describing all parameters and return value
  - Remove (or keep private) the old `_capture_screenshots` function
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 6.1, 6.2, 6.3, 6.4_

  - [ ]* 4.1 Write property test for screenshot folder naming
    - **Property 4: Screenshot folder naming**
    - **Validates: Requirements 5.1, 5.4**
    - Assert `screenshots_dir.name == video_path.stem` (no suffix) for any video path

- [x] 5. Refactor `run_measure` orchestration in `pyqenc/phases/measure.py`
  - Add `screenshot_include_edges: bool = False` parameter to `run_measure`
  - Replace `SCREENSHOTS_SUBDIR_SUFFIX`-based dir naming with `{stem}` directly (no suffix)
  - Execution order: (1) `compute_screenshot_positions(source_meta.frame_count, source_meta.fps_fraction, count, include_edges)`, (2) `logger.info("Taking %d screenshots of %s...", N, source.stem)` → `make_screenshots(source, positions, source_dir, crop)`, (3) for each target: `logger.info(...)` → `make_screenshots(target, positions, target_dir)`, (4) `logger.info("Screenshots taken: %d out of %d %s", taken, expected, symbol)`, (5) for each target: ProgressBar `Measuring target N <stem>` → `_run_metrics(...)`
  - Remove all references to `SCREENSHOTS_SUBDIR_SUFFIX` inside `run_measure`
  - Remove old `_screenshot_timestamps_count` / `_screenshot_timestamps_interval` / `_capture_screenshots` call sites
  - Update `TargetMeasureResult.screenshots_dir` and `MeasureResult.source_screenshots_dir` to use the new `{stem}` naming
  - _Requirements: 1.1, 1.2, 1.3, 3.1, 3.4, 5.1, 7.1, 7.2_

- [x] 6. Implement `_log_measure_summary` in `pyqenc/phases/measure.py`
  - Signature: `def _log_measure_summary(targets: list[TargetMeasureResult]) -> None`
  - Emit header + separator + one data row per target via `logger.info`
  - Columns: target stem (truncated to 30 chars), file size via `_fmt_size_mb`, per-metric median via `fmt_metric_value` for each `MetricType` that appears in any target's `metrics`; `N/A` for absent metrics
  - Omit pass/fail column when no quality targets specified (summary is always called without targets in this context)
  - Reuse `_fmt_size_mb` and `fmt_metric_value` from `pyqenc/utils/log_format.py`
  - Call `_log_measure_summary` at the end of `run_measure` (after all metrics)
  - _Requirements: 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [ ]* 6.1 Write property test for summary table completeness
    - **Property 5: Summary table completeness**
    - **Validates: Requirements 2.1, 2.6**
    - `@given(targets list)` — assert one data row per target; `N/A` for missing metrics

  - [ ]* 6.2 Write property test for no individual median INFO lines
    - **Property 6: No individual median INFO lines**
    - **Validates: Requirements 1.5**
    - Run `run_measure` with mocked sub-operations; assert no INFO record matches `r'\w+ median: \d'`

- [x] 7. Remove `SCREENSHOTS_SUBDIR_SUFFIX` from `constants.py` and all usages
  - Delete the `SCREENSHOTS_SUBDIR_SUFFIX = ".screenshots"` line from `pyqenc/constants.py`
  - Search and remove all remaining imports and usages across the codebase (`pyqenc/phases/measure.py`, any other files)
  - Verify no references remain with a grep pass
  - _Requirements: 5.2, 5.3_

- [x] 8. Update `measure_quality` in `pyqenc/api.py`
  - Add `screenshot_include_edges: bool = False` parameter to `measure_quality`
  - Pass `screenshot_include_edges` through to `run_measure`
  - Update the docstring to document the new parameter
  - _Requirements: 9.1, 9.2, 9.4_

- [x] 9. Checkpoint — ensure all tests pass
  - Run `uv run python -m pytest tests/` and fix any failures before continuing.

- [ ] 10. Write property-based tests in `tests/test_measure_properties.py`
  - [ ]* 10.1 Write property test for screenshot logging completeness
    - **Property 7: Screenshot logging completeness**
    - **Validates: Requirements 1.1, 1.2**
    - Mock `make_screenshots`; assert `"Taking N screenshots of"` INFO line emitted before capture and `"Screenshots taken:"` INFO line emitted after all videos

- [x] 11. Cross-spec review
  - Compare this spec against `all-in-one-metrics` and `vif-metric-support` specs (check created/completed dates and file timestamps to establish timeline)
  - Note any interfaces changed by those specs that affect `run_measure`, `_run_metrics`, `QualityArtifacts`, or `analyze_chunk_quality`
  - Add a brief summary comment at the top of this spec's `requirements.md` and `design.md` noting differences
  - Update `- Completed:` date in `requirements.md` and `design.md`

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- All property tests go in `tests/test_measure_properties.py`
- `ScreenshotPositions.seek_ts` uses `(frame_num * 4 - 1) / (4 * fps)` — equivalent to `(frame_num - 0.25) / fps` but avoids float arithmetic
- Strategy C fallback is triggered only by zero output, never by partial results
- `SCREENSHOTS_SUBDIR_SUFFIX` removal (task 7) must be done after task 5 updates all usages
- All ffmpeg calls must go through `run_ffmpeg_async` from `pyqenc/utils/ffmpeg_runner.py`
