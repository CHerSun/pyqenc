# Requirements: Measure Phase UX Overhaul
<!-- markdownlint-disable MD024 -->

- Created: 2026-05-01

## Glossary

- **Measure_Phase**: The `run_measure` orchestration function and its helpers in `pyqenc/phases/measure.py`.
- **Source**: The reference (original/lossless) video file passed as the first positional argument to `measure`.
- **Target**: An encoded/distorted video file to evaluate against the Source.
- **ScreenshotPosition**: A dataclass holding a single screenshot location as an exact `frame_num: int` and the source `fps` as a `fractions.Fraction`. Timestamps and filename prefixes are derived from these without floating-point drift.
- **Screenshot_Positions**: The `list[ScreenshotPosition]` computed from the Source video only, shared across all videos in a single run.
- **make_screenshots**: The public intermediate API function that captures screenshots for one video at the given Screenshot_Positions.
- **Metrics_Runner**: The internal helper that runs quality metric computation for one source/target pair.
- **Summary_Table**: The tabular log output at the end of a measure run, analogous to the merge-phase summary table.
- **ProgressBar**: The `alive_progress`-backed context manager in `pyqenc/utils/alive.py`.
- **SCREENSHOTS_SUBDIR_SUFFIX**: The constant `".screenshots"` in `constants.py` (currently used; to be replaced — see Requirement 5).
- **Sidecar**: A YAML file written alongside each target's graph PNG, containing metric statistics and run metadata.

---

## Requirements

### Requirement 1: Step-by-step INFO logging

**User Story:** As a developer running `pyqenc measure`, I want clear step-by-step INFO-level log lines so that I can follow progress without enabling debug output.

#### Acceptance Criteria

1. WHEN the Measure_Phase begins taking screenshots of any video (Source or Target), THE Measure_Phase SHALL emit an INFO log line of the form `Taking N screenshots of <video_stem>...` where N is the number of Screenshot_Positions and `<video_stem>` is the file stem of the video being screenshotted.
2. WHEN all screenshot captures are complete, THE Measure_Phase SHALL emit an INFO log line of the form `Screenshots taken: M out of M ✅` where M is the total number of successfully captured screenshots across all videos and the denominator is the total number expected.
3. WHEN the Measure_Phase begins metric computation for a Target, THE Measure_Phase SHALL display a ProgressBar titled `Measuring target <N> <target_stem>` where N is the 1-based index of the target.
4. WHEN all metric computations are complete, THE Measure_Phase SHALL emit the Summary_Table at INFO level.
5. THE Measure_Phase SHALL NOT emit per-metric median lines (e.g. `vmaf median: 94.3`) as individual INFO lines; that detail belongs in the Summary_Table.

---

### Requirement 2: Summary table at end of measure run

**User Story:** As a developer, I want a summary table at the end of `measure` analogous to the merge-phase table, so that I can compare all targets at a glance.

#### Acceptance Criteria

1. THE Summary_Table SHALL include one row per Target, showing: target stem (truncated to 30 chars), file size in MB, and per-metric median value (median only — no min or p05) for all metrics that were measured.
2. THE Summary_Table SHALL include a header row and a separator row.
3. WHEN no quality targets were specified, THE Summary_Table SHALL omit the pass/fail column.
4. THE Summary_Table SHALL be emitted via `logger.info` calls so it appears at INFO level.
5. THE Summary_Table SHALL reuse the existing `_log_merge_summary`-style formatting helpers from `pyqenc/utils/log_format.py` where applicable, rather than duplicating formatting logic.
6. WHEN a metric was not measured for a target (e.g. computation failed), THE Summary_Table SHALL display `N/A` for that metric's cell.

---

### Requirement 3: Screenshot_Positions computed from Source only

**User Story:** As a developer, I want screenshot positions to be stable and independent of targets, so that re-running with the same source and settings always produces screenshots at the same timestamps.

#### Acceptance Criteria

1. THE Measure_Phase SHALL compute Screenshot_Positions based solely on the Source video's frame count and FPS — never on target durations or frame counts.
2. WHEN the `--screenshot-include-edges` flag is absent (default), THE Measure_Phase SHALL use interior-only positions: `step = total_frames // (count + 1)`, positions at frames `step, 2*step, ..., N*step`, converted to timestamps as `frame / fps`.
3. *(Optional)* WHEN the `--screenshot-include-edges` flag is present, THE Measure_Phase SHOULD include frame 0 (timestamp 0) and the last frame (timestamp = duration) in Screenshot_Positions, if supported by the active capture strategy. This is a convenience feature — if the active strategy cannot reliably capture frame 0 or the last frame, this flag MAY be silently ignored.
4. THE Measure_Phase SHALL use the same Screenshot_Positions for the Source and all Targets in a single run.
5. WHEN the Source video is longer than a Target, THE Measure_Phase SHALL still use the Source-derived Screenshot_Positions. For Strategy C, any `-ss` timestamp beyond the target's EOF will produce no output frame for that position — this is expected and acceptable (partial results). The fallback chain is NOT triggered by partial results, only by total failure (zero screenshots). For fallback strategies A2/A4, ffmpeg naturally stops at EOF.
6. THE Screenshot_Positions SHALL be deterministic: given the same Source frame count and FPS and the same screenshot count setting, the positions SHALL be identical across reruns.

---

### Requirement 4: `make_screenshots` as public intermediate API

**User Story:** As a developer integrating the pipeline, I want `make_screenshots` to be a public function so that the merge/final phase can reuse it cleanly without duplicating screenshot logic.

#### Acceptance Criteria

1. THE Measure_Phase module SHALL expose a public function `make_screenshots(video_path, positions, screenshots_dir, crop_params) -> list[Path]` where `positions` is `list[ScreenshotPosition]`.
2. THE `make_screenshots` function SHALL accept `positions: list[ScreenshotPosition]` as the pre-computed screenshot positions. Each `ScreenshotPosition` carries an exact `frame_num: int` and `fps: fractions.Fraction`. The `-ss` seek string for Strategy C is derived as `(frame_num - 0.25) / fps` serialized to 9 decimal places — this guarantees the seek always lands before the target frame so ffmpeg decodes forward to exactly `frame_num`. The `eq(n,F)` frame number for Strategy A2 uses `frame_num` directly. Filename timestamps are derived from `frame_num / fps` as a `Fraction`, formatted to millisecond precision.
3. THE `make_screenshots` function SHALL be importable from `pyqenc.phases.measure` without importing internal helpers.
4. THE `make_screenshots` function SHALL have a complete docstring describing all parameters and return value.
5. THE current internal `_capture_screenshots` function SHALL be renamed to `make_screenshots` (made public) with no change to its core logic.
6. THE `pyqenc/api.py` public `measure_quality` function SHALL NOT expose `make_screenshots` directly — it remains a public intermediate API at the module level, not a top-level pipeline API entry point.

---

### Requirement 5: Screenshot folder naming — `{stem}` without `.screenshots` suffix

**User Story:** As a developer reviewing output, I want screenshot folders named simply `{stem}` (e.g. `(1)`, `(1.1) slow_h265`) so that the folder name matches the video stem cleanly.

#### Acceptance Criteria

1. THE Measure_Phase SHALL place screenshots for each video in a subfolder named `{video.stem}` directly under the measure output directory, with no `.screenshots` suffix.
2. THE `SCREENSHOTS_SUBDIR_SUFFIX` constant SHALL be removed from `constants.py` and all usages updated.
3. WHEN a new constant is needed for the screenshots subfolder naming scheme, THE Measure_Phase SHALL use the video stem directly (no suffix constant required).
4. THE Source screenshots folder SHALL follow the same naming rule: `{source.stem}` under the measure output directory.

---

### Requirement 6: Screenshot performance — exact-location capture within ≤ 1/3 video duration

**User Story:** As a developer, I want screenshots to be taken at exact timestamps within a wall-clock time of at most 1/3 of the video duration, so that screenshot capture is not a bottleneck for long videos.

#### Acceptance Criteria

1. THE `make_screenshots` function SHALL attempt screenshot capture using Strategy C first: N sequential ffmpeg calls with `-ss <timestamp>` before `-i` and `-frames:v 1`. This is the primary strategy — fast and frame-accurate for well-formed files.
2. WHEN Strategy C produces zero screenshots (e.g. ffmpeg exits with an error, or the video has no seekable index), THE `make_screenshots` function SHALL fall back to Strategy A2: a single-pass `select='eq(n,F1)+eq(n,F2)+...'` filter targeting the exact frame numbers. A WARNING SHALL be logged in the form: `Screenshot strategy C failed for <stem> (reason: <reason>) — falling back to A2 (single-pass frame-number select)`.
   <!-- TODO: Identify additional explicit failure conditions for Strategy C beyond zero output (e.g. missing duration, corrupt container) that should also trigger fallback. -->
3. WHEN Strategy A2 also produces zero screenshots, THE `make_screenshots` function SHALL fall back to Strategy A4: a single-pass `select='not(mod(n,step))*gt(n,0)'` filter. A WARNING SHALL be logged in the form: `Screenshot strategy A2 failed for <stem> (reason: <reason>) — falling back to A4 (single-pass mod select)`.
4. WHEN all strategies produce zero screenshots, THE `make_screenshots` function SHALL log an ERROR in the form: `All screenshot strategies failed for <stem> — no screenshots captured` and return an empty list. Partial results (fewer screenshots than expected due to shorter video duration) are acceptable and SHALL NOT trigger a fallback.
5. THE `make_screenshots` function SHALL complete screenshot capture for a 1.5-hour video in ≤ 30 minutes wall-clock time (≤ 1/3 of video duration) under normal conditions (Strategy C). Benchmarking confirmed ~7s for 20 screenshots on a 1.6h movie (~0.1% of duration).
6. THE screenshot positions SHALL be computed as `ScreenshotPosition` instances using exact integer frame arithmetic: given source total frames `F` and count `N`, step `= F // (N + 1)`, positions at frame numbers `step, 2*step, ..., N*step`. The `fps` is stored as a `fractions.Fraction` parsed from ffprobe's `avg_frame_rate` (e.g. `Fraction(24000, 1001)`). Timestamps are derived as `frame_num / fps` using rational arithmetic — no float drift. This ensures positions are stable and deterministic across reruns.
7. THE screenshot filenames SHALL use the timestamp prefix format `HH꞉MM꞉SS․mmm_<stem>.png` (using `TIME_SEPARATOR_SAFE` and `TIME_SEPARATOR_MS` from `constants.py`) derived from the frame-aligned timestamp.
8. THE benchmarking script SHALL be retained at `.kiro/specs/measure-phase-ux-overhaul/benchmark_screenshots.py` as research evidence.
9. THE benchmarking results SHALL be recorded in `.kiro/specs/measure-phase-ux-overhaul/benchmark_results.md`.

---

### Requirement 7: API shape — screenshots first, then metrics

**User Story:** As a developer, I want all screenshots taken before any metric computation begins, so that the user sees all screenshots quickly and metrics run in a predictable second phase.

#### Acceptance Criteria

1. THE Measure_Phase SHALL follow this execution order: (1) compute Screenshot_Positions from Source, (2) capture Source screenshots, (3) capture all Target screenshots, (4) run metrics for each Target sequentially.
2. THE Measure_Phase SHALL NOT interleave screenshot capture and metric computation.
3. WHEN screenshot capture for a Target fails, THE Measure_Phase SHALL log a warning and continue to metric computation for that Target.

---

### Requirement 8: Sidecar files — keep as-is

**User Story:** As a developer, I want the sidecar YAML files to remain unchanged in content and placement so that downstream consumers are not broken.

#### Acceptance Criteria

1. THE Measure_Phase SHALL continue to write one Sidecar YAML per Target at `{measure_dir}/{target_stem}.yaml`.
2. THE Sidecar content format (flat `{metric_stat: value}` dict plus metadata fields) SHALL remain unchanged.
3. THE `.tmp`-then-rename protocol SHALL continue to be used for Sidecar writes.

---

### Requirement 9: `measure_quality` public API — `screenshot_include_edges` parameter

**User Story:** As a developer using the public API, I want to control whether edge frames are included in Screenshot_Positions via a parameter, so that the API is not CLI-only.

#### Acceptance Criteria

1. THE `measure_quality` function in `pyqenc/api.py` SHALL accept a `screenshot_include_edges: bool = False` parameter.
2. WHEN `screenshot_include_edges=True`, THE `measure_quality` function SHALL pass the flag through to `run_measure` so that Screenshot_Positions include the first and last frames.
3. THE CLI `--screenshot-include-edges` flag SHALL map to `screenshot_include_edges=True` in the `measure_quality` call.
4. THE default value of `screenshot_include_edges` SHALL be `False` (edges excluded by default).
