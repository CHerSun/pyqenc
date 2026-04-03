# Requirements: Standalone Measure Command
<!-- markdownlint-disable MD024 -->

- Created: 2025-07-17
- Completed:

## Introduction

The `measure` subcommand provides a standalone quality measurement capability for pyqenc. Given a source video and zero or more target (encoded) videos, it computes all supported quality metrics (currently VMAF, SSIM, PSNR — extensible as new metrics are added), writes a metrics sidecar YAML per target, generates a metrics graph per target, and captures N screenshots from the source and each target at native resolution. This mirrors the quality measurement performed during chunk encoding attempts and the final merge phase, but operates independently of any pipeline job state — no phase state, no CRF graph (since CRF data is not available in this mode). The command CAN read crop parameters from `job.yaml` if present in the working directory.

When no targets are provided, the command operates in screenshots-only mode: it captures screenshots from the source video (with crop applied) and skips all metric computation, graph generation, and sidecar writing. This is useful for quickly previewing crop settings or screenshot intervals without a full quality measurement run.

All outputs are isolated in a `measure/` subdirectory of the working directory to avoid mixing with pipeline phase artifacts. Raw metric log files and screenshots are placed in per-video subfolders (one per target); the graph and metrics sidecar are placed directly in `measure/` (one per target).

## Glossary

- **Measure_Command**: The `measure` CLI subcommand and its backing API function.
- **Source_Video**: The original reference video file (first positional CLI argument).
- **Target_Videos**: The list of encoded/distorted video files to evaluate (zero or more positional CLI arguments after `source`).
- **Measure_Dir**: The output directory `<work_dir>/measure/` where all measure artifacts are written.
- **Metrics_Subdir**: A per-target subdirectory inside `Measure_Dir` for raw metric log files, named `<target_stem>.metrics/` (one per target video, named after each target's stem).
- **Screenshots_Subdir**: Per-video subdirectories inside `Measure_Dir` for screenshot PNG files. Target screenshots go into `<target_stem>.screenshots/` (one per target); source screenshots go into `<source_stem>.screenshots/`.
- **Metrics_Sidecar**: A YAML file written directly into `Measure_Dir` summarising computed metric statistics, named `<target_stem>.yaml` (one per target video, named after each target's stem).
- **Metrics_Graph**: A PNG plot of quality metrics over time written directly into `Measure_Dir`, named `<target_stem>.png` (one per target video, named after each target's stem).
- **Screenshot**: A single PNG frame captured at native resolution from a video at a specific timestamp.
- **Screenshot_Count**: The maximum number of screenshots to capture from each video (CLI argument `--screenshots`, default 20). In count mode this is the exact target; in interval mode this is an optional cap.
- **Screenshot_Interval**: The interval in seconds between screenshots when using interval mode (CLI argument `--every`). The first screenshot is taken at `1 × interval` (not at 0), skipping the typically-black first frame.
- **Screenshot_Mode**: Either count-based (no `--every`) or interval-based (`--every` provided). `--screenshots` applies in both modes — as the count in count mode, as a cap in interval mode.
- **Crop_Params**: Optional crop parameters applied to the source video during metric computation, matching the convention used by other pipeline phases.
- **Scale_Width**: Optional target width in pixels for metric computation (CLI argument `--width`). When provided, both source and target are scaled to this width (preserving aspect ratio) after cropping, before metric computation. Screenshots are NOT scaled — only cropped. Crop is always applied before scaling.
- **Subsample_Factor**: Frame subsampling factor for metric computation (CLI argument `--metrics-sampling`, default 10, matching the pipeline default).
- **Effective_Duration**: The lesser of the source and target video durations, computed per target as `min(source_duration, target_duration)`, used as the common time window for all operations (metric computation, screenshot timestamps) for that target.
- **QualityEvaluator**: The existing `pyqenc.utils.visualization.QualityEvaluator` class used for metric generation and plotting.
- **JobState**: The existing `pyqenc.state.JobState` model that stores crop parameters in `job.yaml`.

## Requirements

### Requirement 1: CLI Subcommand Registration

**User Story:** As a developer, I want a `measure` subcommand in the pyqenc CLI, so that I can invoke quality measurement directly without running the full pipeline.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL be registered as a subparser named `measure` in the pyqenc CLI alongside the existing subcommands (`auto`, `extract`, `chunk`, `encode`, `audio`, `merge`).
2. THE `Measure_Command` SHALL accept `source` as the first positional argument (path to the reference video file). The CLI help text SHALL warn that argument order is critical — swapping source and target will produce incorrect metric results (VMAF in particular is not symmetric). This warning applies when at least one target is provided.
3. THE `Measure_Command` SHALL accept `targets` as zero or more positional arguments (`nargs='*'`, paths to encoded/distorted video files to evaluate against the source). The CLI help text SHALL be: "Zero or more encoded/distorted video files to evaluate against the source. Omit all to run in screenshots-only mode."
4. THE `Measure_Command` SHALL accept `--work-dir` and `--log-level` base arguments. It SHALL NOT accept `--force`, `--cleanup`, `--no-metrics`, or `-y`/`--execute` — those are pipeline-phase-only arguments and would be misleading on `measure`. The CLI argument helpers SHALL be refactored to split `_add_common_arguments` into `_add_base_arguments` (universal) and `_add_pipeline_arguments` (phases only). `-y`/`--execute` moves to `_add_pipeline_arguments`.
5. THE `Measure_Command` SHALL accept `--metrics-sampling N` (integer, default 10) to control the frame subsampling factor for metric computation.
6. THE `Measure_Command` SHALL accept `--screenshots N` (integer, default 20, minimum 1). In count mode (no `--every`), this sets the number of equally-spaced screenshots. In interval mode (`--every` provided), this acts as an optional cap — at most N screenshots are taken even if more intervals fit within `Effective_Duration`.
7. THE `Measure_Command` SHALL accept `--every DURATION` to switch to interval mode: one screenshot per interval, starting at `1 × interval`. `DURATION` SHALL accept a plain integer or float (seconds) or a human-friendly string such as `30s`, `5m`, `1h`, `1h30m`.
8. WHEN `--every` is not provided, THE `Measure_Command` SHALL operate in count mode: `--screenshots N` equally-spaced screenshots across `Effective_Duration`.
9. WHEN `--every` is provided, THE `Measure_Command` SHALL operate in interval mode: one screenshot per interval, optionally capped at `--screenshots N`. When neither is provided, the default is count mode with `Screenshot_Count = 20`.
7. THE `Measure_Command` SHALL accept `--crop PARAMS` to specify manual crop parameters in the same format as other subcommands (`"top bottom"` or `"top bottom left right"`).
8. THE `Measure_Command` SHALL accept `--no-crop` to explicitly disable cropping even when a `job.yaml` with crop data exists in the working directory.
9. IF `--crop` and `--no-crop` are both provided, THEN THE `Measure_Command` SHALL reject the invocation with a clear error message, consistent with the mutually exclusive crop group used in other subcommands.
10. THE `Measure_Command` SHALL accept `--width N` (positive integer) to scale both source and target to width N (preserving aspect ratio) during metric computation. Crop is applied first, then scaling. Screenshots are never scaled.
11. THE `Measure_Command` SHALL always overwrite existing output files — there is no skip-if-exists or dry-run mode. The command always executes immediately when invoked.

### Requirement 1b: Screenshots-Only Mode

**User Story:** As a user, I want to run the measure command with only a source video to capture screenshots without running metric computation, so that I can quickly preview crop settings or screenshot intervals without a full quality measurement run.

#### Acceptance Criteria

1. WHEN no targets are provided (empty list), THE `Measure_Command` SHALL operate in screenshots-only mode.
2. IN screenshots-only mode, THE `Measure_Command` SHALL capture screenshots from the source video only, applying crop but no scaling.
3. IN screenshots-only mode, THE `Measure_Command` SHALL skip all metric computation, graph generation, and sidecar writing.
4. IN screenshots-only mode, THE `Measure_Command` SHALL write source screenshots into `<source_stem>.screenshots/` inside `Measure_Dir`.
5. IN screenshots-only mode, THE `Measure_Command` SHALL log an info message indicating it is running in screenshots-only mode (no target videos provided).
6. WHEN one or more targets are provided, THE `Measure_Command` SHALL operate in full mode: metrics, graph, sidecar, and screenshots from both source and each target.
7. Arguments that are only meaningful for metric computation (`--metrics-sampling`, `--width`) SHALL be accepted but ignored (with a debug-level log) when running in screenshots-only mode.

### Requirement 2: Crop Parameter Resolution

**User Story:** As a user, I want the measure command to automatically reuse detected crop parameters from a prior pipeline run, so that I don't have to re-specify them manually.

#### Acceptance Criteria

1. WHEN `--crop PARAMS` is provided, THE `Measure_Command` SHALL parse and use those crop parameters for the source video during metric computation.
2. WHEN `--no-crop` is provided, THE `Measure_Command` SHALL use empty (no-op) crop parameters regardless of any `job.yaml` content.
3. WHEN neither `--crop` nor `--no-crop` is provided AND a `job.yaml` exists in the working directory AND the source video path in `job.yaml` matches the `source` argument, THE `Measure_Command` SHALL load and use the crop parameters from `job.yaml`.
4. WHEN neither `--crop` nor `--no-crop` is provided AND no `job.yaml` exists, OR `job.yaml` exists but its source video does not match the `source` argument, OR `job.yaml` contains no crop data, THE `Measure_Command` SHALL proceed with empty (no-op) crop parameters and log an info-level message indicating no crop was applied.
5. THE `Measure_Command` SHALL NEVER write to or modify `job.yaml` under any circumstances. It is strictly read-only from the perspective of this command.
6. THE `Measure_Command` SHALL log the resolved crop parameters at debug level before beginning metric computation.

### Requirement 3: Output Directory Layout

**User Story:** As a user, I want measure outputs isolated in a dedicated folder, so that they don't interfere with pipeline phase artifacts.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL write all outputs under `<work_dir>/measure/` (`Measure_Dir`).
2. THE `Measure_Command` SHALL write the `Metrics_Graph` PNG directly into `Measure_Dir` as `<target_stem>.png`.
3. THE `Measure_Command` SHALL write the `Metrics_Sidecar` YAML directly into `Measure_Dir` as `<target_stem>.yaml`.
4. THE `Measure_Command` SHALL write raw metric log files (PSNR `.log`, SSIM `.log`, VMAF `.json`) into `Metrics_Subdir` (`<target_stem>.metrics/` inside `Measure_Dir`).
5. THE `Measure_Command` SHALL write screenshot PNG files into `Screenshots_Subdir` (`<target_stem>.screenshots/` inside `Measure_Dir`).
6. THE `Measure_Command` SHALL create all required directories (including `Measure_Dir` and its subdirectories) before writing any output files.

### Requirement 3b: Duration Alignment

**User Story:** As a user, I want to compare a source and targets that may have different durations (e.g. due to encoding trim, container differences, or an early-stopped encoding with no embedded timestamps), so that screenshots and metrics are taken from the same temporal window in both videos.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL probe the duration of `Source_Video` and each `Target_Video` before any screenshot capture.
2. WHEN both durations are available for a given target, THE `Measure_Command` SHALL use `Effective_Duration = min(source_duration, target_duration)` as the common time window for that target's screenshot timestamp computation and metric computation.
3. WHEN the durations differ by more than 1 second for a given target, THE `Measure_Command` SHALL log a warning at warning level indicating both durations and the effective duration being used for that target.
4. WHEN duration is unavailable for one or both videos (e.g. ffprobe reports N/A — common with early-stopped encodings that have no embedded timestamps), THE `Measure_Command` SHALL:
   - Log a warning indicating which video has no duration information.
   - If `--every` mode is active: proceed with screenshot capture using the interval; ffmpeg will naturally stop at EOF. Log an info message that screenshots will be taken until EOF.
   - If count mode is active (`--screenshots N` without `--every`): log an error and skip screenshot capture entirely, since equally-spaced timestamps cannot be computed without a known duration. Metric computation continues unaffected.
5. THE `Measure_Command` SHALL pass `Effective_Duration` to metric computation when available, so that only the overlapping portion of both videos is evaluated. When duration is unavailable, metric computation proceeds without a duration limit and ffmpeg handles EOF naturally.
6. THE `Effective_Duration` SHALL be recorded in the `Metrics_Sidecar` alongside the individual source and target durations. When a duration is unavailable, it SHALL be recorded as `null`.

### Requirement 3c: Resolution Validation

**User Story:** As a user, I want the measure command to detect resolution mismatches before running a long metric computation, so that I get a clear error with actionable guidance rather than silently wrong results.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL probe the resolution of `Source_Video` and ALL `Target_Videos` before beginning any metric computation. ALL resolution checks MUST pass before any processing begins. If any target fails the resolution check, THE `Measure_Command` SHALL log a critical error for that target and stop entirely (no processing for any target).
2. THE resolution check SHALL be performed after crop and scale parameters are resolved, so the check reflects the effective post-crop, post-scale dimensions.
3. WHEN the effective resolutions differ, THE `Measure_Command` SHALL log a critical error and stop without running any metric computation or screenshot capture.
4. THE critical error message SHALL include both effective resolutions and a specific actionable suggestion computed as follows:
   - Crop is source-only and vertical only (top/bottom), applied at native source resolution.
   - Compute the vertical crop to align source height to target height: `top = (source_height - target_height) // 2`, `bottom = source_height - target_height - top`.
   - After applying that vertical crop, the cropped source has dimensions `source_width × target_height`.
   - If `source_width == target_width`: crop alone resolves the mismatch. Suggest `--crop TOP BOTTOM`.
   - If `source_width != target_width`: suggest `--width target_width` to scale both cropped source and target to `target_width` (preserving aspect ratio). Since the cropped source is now `source_width × target_height` and target is `target_width × target_height`, scaling both to `target_width` yields `target_width × (target_height * target_width / source_width)` for source — this only works if the aspect ratios match after crop. The suggestion SHALL note the final effective resolution both videos will be scaled to.
   - If `source_height < target_height` (target is taller than source after crop): note that vertical crop cannot fix this and suggest re-encoding the target at the correct resolution.
   - Format: `"Did you mean: --crop TOP BOTTOM [--width W]? This would bring both videos to WxH for metric computation."`
5. THE `Measure_Command` SHALL pass `Scale_Width` to metric computation so that `run_metric` applies `scale=N:-1` to both inputs after cropping.
6. Screenshots SHALL NOT be scaled — `Scale_Width` applies only to metric computation, not to screenshot capture.
7. Crop is applied only to the source video; scaling (`--width`) is applied to both source and target after cropping.

### Requirement 4: Quality Metric Computation

**User Story:** As a user, I want the measure command to compute the same quality metrics as the pipeline phases, so that I get consistent, comparable results.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL compute all supported quality metrics for each target sequentially against the source, by reusing the existing `QualityEvaluator.evaluate_chunk` method, so that any metrics added to the pipeline in future are automatically included.
2. THE `Measure_Command` SHALL pass the resolved `Crop_Params` as the reference crop to `QualityEvaluator.evaluate_chunk`, consistent with how other phases apply cropping.
3. THE `Measure_Command` SHALL use the `Subsample_Factor` from `--metrics-sampling` (or its default of 10) for metric computation.
4. THE `Measure_Command` SHALL display a duration-based progress bar during metric computation using the existing `ProgressBar` utility, consistent with how the final merge phase displays progress. The `Effective_Duration` (pre-computed per target from both video probes) SHALL be used as the progress total so the bar is accurate even when source and target durations differ.
5. THE `Measure_Command` SHALL compute and report all metrics without pass/fail evaluation.
6. THE `Measure_Command` SHALL NOT produce a CRF graph, as CRF data is not available in standalone measurement mode.

### Requirement 5: Metrics Sidecar

**User Story:** As a user, I want a YAML sidecar with metric statistics written alongside the graph, so that I can programmatically inspect results without parsing the graph.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL write a `Metrics_Sidecar` YAML file to `Measure_Dir/<target_stem>.yaml` after metric computation completes for each target (one sidecar per target, named `<target_stem>.yaml`).
2. THE `Metrics_Sidecar` SHALL contain all computed metric statistics for every metric type returned by `QualityEvaluator.evaluate_chunk` (currently VMAF, SSIM, PSNR; extensible without spec changes).
3. THE `Metrics_Sidecar` SHALL record the `Subsample_Factor` used during computation.
4. THE `Metrics_Sidecar` SHALL record the resolved crop parameters (or indicate none) used during computation.
5. THE `Metrics_Sidecar` SHALL record the source and target video file paths (as strings).
6. THE `Measure_Command` SHALL write the `Metrics_Sidecar` using the `.tmp`-then-rename atomic write protocol.
7. IF writing the `Metrics_Sidecar` fails, THEN THE `Measure_Command` SHALL log a warning and continue without raising an exception, so that the graph and screenshots are not lost.

### Requirement 6: Metrics Graph

**User Story:** As a user, I want a quality metrics graph over time, so that I can visually inspect where quality drops occur in the encoded video.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL generate a `Metrics_Graph` PNG by reusing the existing `create_unified_plot` function (via `QualityEvaluator.evaluate_chunk`).
2. THE `Metrics_Graph` SHALL be written to `Measure_Dir/<target_stem>.png` (one graph per target, named `<target_stem>.png`).
3. THE `Metrics_Graph` SHALL NOT include a CRF graph, as CRF data is unavailable in standalone measurement mode. The CRF graph is a separate output produced by a separate function and SHALL NOT be called here.
4. THE `Metrics_Graph` title SHALL include the target video stem for identification.

### Requirement 7: Screenshot Capture

**User Story:** As a user, I want N screenshots from both source and target videos placed side-by-side in a folder, so that I can visually compare quality at representative moments.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL capture screenshots from `Source_Video` (once) and from each `Target_Video` at the same set of timestamps. Source screenshots are taken once using the shared timestamp set derived from the minimum effective duration across all targets. In count mode, the target is exactly `Screenshot_Count` equally-spaced screenshots per video. In interval mode, one screenshot per interval is taken, optionally capped at `Screenshot_Count`.
2. THE `Measure_Command` SHALL select screenshot timestamps according to the active mode: equally-spaced across the shared effective duration in count mode; at multiples of `Screenshot_Interval` starting at `1 × interval` in interval mode. In both modes timestamps are strictly interior to the shared effective duration.
3. THE `Measure_Command` SHALL capture screenshots with crop applied (using the resolved `Crop_Params`) but WITHOUT any scaling. The `--width` scaling parameter applies only to metric computation, not to screenshots.
4. THE `Measure_Command` SHALL save screenshots as PNG files using the `.tmp`-then-rename atomic write protocol.
5. THE `Measure_Command` SHALL name each screenshot file as `<HH꞉MM꞉SS․mmm>_<video_stem>.png`, where the timestamp prefix uses `TIME_SEPARATOR_SAFE` (`꞉`) and `TIME_SEPARATOR_MS` (`․`) from `constants.py` — the same format used for chunk filenames — and all components are zero-padded so that files sort consistently across the full video duration.
6. THE `Measure_Command` SHALL write target screenshot PNG files into `<target_stem>.screenshots/` inside `Measure_Dir`, and source screenshot PNG files into `<source_stem>.screenshots/` inside `Measure_Dir`.
7. THE `Measure_Command` SHALL compute a single shared set of screenshot timestamps using `min(effective_duration_per_target)` across all targets, so all screenshots (source and all targets) are taken at the same positions.
8. THE `Measure_Command` SHALL use the ffmpeg unified runner for screenshot extraction, capturing all screenshots for a single video in one ffmpeg pass using the `select` filter for frame-perfect extraction. The primary mode is timestamp-based (`eq(t,T)`); the fallback for containers without embedded timestamps is frame-number-based (`eq(n,F)`, requires fps). Fast-seek (`-ss` before `-i`) SHALL NOT be used as it snaps to I-frames.
9. WHEN a screenshot capture fails for a specific frame, THE `Measure_Command` SHALL log a warning and continue capturing remaining screenshots rather than aborting the entire operation.
10. WHEN the computed timestamp set is empty (i.e. the shared effective duration is too short to fit even one screenshot at the requested interval or count), THE `Measure_Command` SHALL log an error and skip all screenshot capture without aborting metric computation.
11. WHEN the number of timestamps that fit within the shared effective duration is less than the requested `Screenshot_Count` (count mode) or fewer intervals fit than expected (interval mode), THE `Measure_Command` SHALL log a warning stating how many screenshots will be taken versus how many were requested, then proceed with the reduced set.
12. THE `Measure_Command` SHALL log the number of successfully captured screenshots at info level upon completion.

### Requirement 8: Public API Function

**User Story:** As a developer, I want a `measure_quality` function in `pyqenc/api.py`, so that the measure capability is accessible programmatically without going through the CLI.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL expose a `measure_quality` function in `pyqenc/api.py` as part of the public API surface.
2. THE `measure_quality` function SHALL accept `source_video: Path`, `target_videos: list[Path]`, `work_dir: Path`, `crop_params: CropParams | None`, `metrics_sampling: int`, and `screenshot_count: int` as parameters.
3. THE `measure_quality` function SHALL return a `MeasureResult` containing: the source screenshots directory path, and per-target results (graph path, sidecar path, screenshots directory path, and in-memory metric statistics for each target).
4. THE `measure_quality` function SHALL have a complete docstring describing all parameters, return value, and raised exceptions.
5. IF `source_video` does not exist, THEN THE `measure_quality` function SHALL raise `FileNotFoundError`.
6. IF any path in `target_videos` does not exist, THEN THE `measure_quality` function SHALL raise `FileNotFoundError`.

### Requirement 11: Run Parameters Summary

**User Story:** As a user, I want to see a clear summary of the key parameters before measurement starts, so that I can confirm the command is running with the intended settings.

#### Acceptance Criteria

1. THE `Measure_Command` SHALL log a key-value table at info level before beginning metric computation, using the existing `fmt_key_value_table` function from `pyqenc/utils/log_format.py`.
2. THE table SHALL include at minimum: source path, number of targets, list of target stems, resolved crop parameters, width scaling (or "none"), and screenshot mode (count N or every DURATION, with cap if set).
3. THE table SHALL be logged after crop resolution and resolution validation, so it reflects the final effective parameters.

**User Story:** As a user, I want clear error messages when I provide invalid inputs, so that I can quickly correct mistakes.

#### Acceptance Criteria

1. IF `source` video path does not exist, THEN THE `Measure_Command` SHALL log a critical error and exit with a non-zero exit code.
2. IF any `targets` video path does not exist, THEN THE `Measure_Command` SHALL log a critical error and exit with a non-zero exit code.
3. IF `--screenshots` is provided with a value less than 1, THEN THE `Measure_Command` SHALL log a critical error and exit with a non-zero exit code.
4. IF `--metrics-sampling` is provided with a value less than 1, THEN THE `Measure_Command` SHALL log a critical error and exit with a non-zero exit code.
5. IF `--crop` is provided with an invalid format, THEN THE `Measure_Command` SHALL log a critical error and exit with a non-zero exit code.
6. WHEN metric computation fails entirely (e.g. ffmpeg error on all metrics), THE `Measure_Command` SHALL log a critical error and exit with a non-zero exit code.
