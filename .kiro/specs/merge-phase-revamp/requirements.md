# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-19
- Completed: 2026-03-19

## Introduction

This spec covers a set of targeted improvements to the merge phase and the metrics pipeline:

1. **Work-mode banner** — the very first log line of the merge phase must clearly state the operating mode (optimal-strategy or all-strategies) and the target strategy name.
2. **Chunk count scoping** — in optimal-strategy mode the chunk count message must only count chunks belonging to the target strategy, not all strategies.
3. **Output filename** — the final concatenated video must be named after the source stem and the strategy, not a generic `output_<safe>` name.
4. **Metrics artifact placement** — intermediate metric artifacts (logs, stats) go into a subfolder named after the final result stem; the graph and sidecar go next to the video, named after the final result stem.
5. **Metrics normalization** — all metric values must be normalized to the 0–100 scale immediately after extraction from raw log files, before any downstream use (graphs, sidecars, target evaluation, log messages).
6. **Metrics extraction temp-file protocol** — metric extraction must write to `.tmp`-suffixed files and rename on success, consistent with the rest of the pipeline.
7. **Post-merge summary** — after the merge phase completes, the pipeline must display a human-readable summary of the resulting files, their sizes, and the savings vs. the extracted video.
8. **Optimization summary size fix** — the optimization summary table must show the total size of successful test chunks, not an average.

## Glossary

- **Merge phase**: The pipeline phase that concatenates encoded chunks into a final MKV output file and measures final quality metrics.
- **Optimal-strategy mode**: Pipeline run where only the single best-performing strategy is merged (selected by the optimization phase).
- **All-strategies mode**: Pipeline run where every complete strategy is merged into its own output file.
- **Strategy**: An encoding configuration identified by a preset+profile key (e.g. `slow+h265`).
- **Safe strategy name**: A filesystem-safe version of the strategy name with `+` and `:` replaced by `_`.
- **Source stem**: The `Path.stem` of the source video file (filename without extension).
- **Final result**: The merged MKV output file for a given strategy.
- **Metrics intermediate artifacts**: Raw metric log files (PSNR `.log`, SSIM `.log`, VMAF `.json`) and derived `.stats` files produced during quality measurement.
- **Metrics final artifacts**: The quality plot PNG and the sidecar YAML placed alongside the final result video.
- **Normalization**: Mapping raw metric values to the 0–100 scale: SSIM × 100, PSNR capped at 100, VMAF unchanged.
- **Temp-file protocol**: Writing output to a `.tmp`-suffixed sibling file and atomically renaming to the final name on success, so interrupted runs leave no corrupt files.
- **Extracted video**: The lossless or remuxed video stream produced by the extraction phase, used as the reference for quality measurement.
- **Metrics sampling factor**: An integer `N` (minimum 1, default 10, recommended maximum 30) controlling which frames are measured; a value of `N` means every `N`-th frame is measured. A value of 1 disables sampling (every frame is measured). Higher values are faster but introduce measurement volatility without affecting actual encoding results.
- **QualityEvaluator**: The class in `pyqenc/utils/visualization.py` responsible for running ffmpeg metric passes and parsing results.
- **MetricsSidecar**: The per-attempt YAML sidecar storing CRF and measured metric values.
- **StrategyTestResult**: Dataclass in `pyqenc/phases/optimization.py` holding per-strategy optimization results including file size.

### Requirement 9

**User Story:** As a pipeline operator, I want to control the metrics sampling factor via config file and CLI argument, so that I can tune measurement granularity without modifying source code.

#### Acceptance Criteria

1. THE Configuration System SHALL read a `sampling` integer value from the `metrics` section of the YAML config file and use it as the default metrics sampling factor when no CLI override is provided.
2. THE Default Config SHALL define a `metrics` section with `sampling: 10` so that the pipeline behaves identically to the current hardcoded default when no custom config is present.
3. WHEN the user provides `--metrics-sampling N` on the CLI for the `auto`, `optimize`, `encode`, or `merge` subcommands, THE Pipeline SHALL use `N` as the metrics sampling factor for that run, overriding the config-file value.
4. THE Pipeline SHALL accept any integer greater than or equal to 1 as a valid `--metrics-sampling` value, where 1 means every frame is measured (no sampling), and SHALL NOT validate the value against any previously used value from prior runs.
5. THE CLI argument `--metrics-sampling` SHALL display a help string stating that the minimum value is 1 (every frame measured), the default is 10 (recommended balance of speed and precision), and values above 30 are not recommended due to measurement volatility.

---

## Requirements

### Requirement 1

**User Story:** As a pipeline operator, I want the very first log line of the merge phase to clearly state the operating mode and target strategy, so that I immediately know what the run is doing.

#### Acceptance Criteria

1. WHEN the merge phase starts in optimal-strategy mode, THE Merge Phase SHALL emit as its first `info`-level log line a message of the form `"Mode: optimal strategy — <display_strategy_name>"` before any other merge-phase log output.
2. WHEN the merge phase starts in all-strategies mode, THE Merge Phase SHALL emit as its first `info`-level log line a message of the form `"Mode: all strategies — <N> strategy(ies): <comma-separated list>"` before any other merge-phase log output.
3. THE Merge Phase SHALL NOT emit the legacy `"Merging optimal strategy: ..."` or `"Merging N strategy(ies): ..."` messages as the first line.

---

### Requirement 2

**User Story:** As a pipeline operator, I want chunk count messages to reflect only the chunks relevant to the current mode, so that the numbers are accurate and not misleading.

#### Acceptance Criteria

1. WHEN the merge phase runs in optimal-strategy mode, THE Merge Phase SHALL count and report only the chunks that belong to the target strategy.
2. WHEN the merge phase runs in all-strategies mode, THE Merge Phase SHALL count and report the total chunks across all complete strategies being merged.
3. THE Merge Phase SHALL NOT log a chunk count that includes chunks from strategies not being merged in the current run.

---

### Requirement 3

**User Story:** As a pipeline operator, I want the final output video to be named after the source file and the strategy, so that the filename is self-describing and unique.

#### Acceptance Criteria

1. THE Merge Phase SHALL name each final output file `f"{source.stem} {safe_strategy_name}.mkv"` where `source.stem` is the stem of the source video path and `safe_strategy_name` is the strategy name with `+` and `:` replaced by `_`.
2. THE Merge Phase SHALL place the output file in the configured output directory.
3. WHEN the source stem contains characters that are valid in filenames, THE Merge Phase SHALL preserve them verbatim in the output filename.

---

### Requirement 4

**User Story:** As a pipeline operator, I want metrics artifacts to be placed consistently relative to the final result video, so that I can find them predictably.

#### Acceptance Criteria

1. THE Merge Phase SHALL place metrics intermediate artifacts (raw log files, `.stats` files) into a subdirectory named `f"{final_result.stem}"` inside the output directory.
2. THE Merge Phase SHALL place the quality plot PNG at `f"{output_dir}/{final_result.stem}.png"`.
3. THE Merge Phase SHALL place the metrics sidecar YAML at `f"{output_dir}/{final_result.stem}.yaml"`.
4. THE QualityEvaluator SHALL accept an explicit `plot_path` parameter so callers can control where the plot is written (this already exists; the merge phase must pass the correct path).

---

### Requirement 5

**User Story:** As a pipeline operator, I want all metric values to be normalized to the 0–100 scale immediately after extraction, so that targets, graphs, sidecars, and log messages all use a consistent scale.

#### Acceptance Criteria

1. THE Pipeline SHALL apply `normalize_metric(metric_type, value)` to every per-frame metric value immediately after parsing raw log files, before computing statistics.
2. THE Pipeline SHALL store only normalized values in `MetricsSidecar.metrics` and in the merge sidecar `metrics` dict.
3. THE Pipeline SHALL evaluate quality targets against normalized values only.
4. THE Pipeline SHALL display normalized values in all log messages and graph annotations.
5. WHEN a user specifies a quality target (e.g. `ssim-min:95`), THE Pipeline SHALL interpret the target value as already on the 0–100 scale and SHALL NOT apply any additional scaling to the target value.
6. THE Pipeline SHALL NOT modify the raw metric log files themselves; normalization applies only to in-memory values after parsing.

---

### Requirement 6

**User Story:** As a pipeline operator, I want metric extraction to follow the temp-file protocol, so that interrupted runs leave no corrupt metric files.

#### Acceptance Criteria

1. WHEN the QualityEvaluator writes a metric log file (PSNR `.log`, SSIM `.log`, VMAF `.json`), THE QualityEvaluator SHALL first write to a temp file with a unique name and the `.tmp` extension in the same directory, where the unique name avoids collisions between concurrent processes and avoids problems with lack o proper ffmpeg support of symbols like space.
2. WHEN the metric extraction subprocess completes successfully, THE QualityEvaluator SHALL atomically rename the `.tmp` file to the final filename derived from the encoded video stem (e.g. `<encoded_stem>.psnr.log`).
3. IF the metric extraction subprocess fails or is interrupted, THE QualityEvaluator SHALL leave the `.tmp` file in place (to be cleaned up by the standard `.tmp` cleanup routine) and SHALL NOT produce a partial final metric file.
4. THE standard `.tmp` cleanup routine SHALL remove any leftover `.tmp` metric files on the next pipeline run.

---

### Requirement 7

**User Story:** As a pipeline operator, I want a summary displayed after the merge phase completes, so that I can immediately see what I got and how much space was saved.

#### Acceptance Criteria

1. WHEN the merge phase completes in optimal-strategy mode, THE Pipeline SHALL display a summary showing the output file path, its size in MB, the extracted video size in MB, the space saved as a percentage, and the final measured metric values for each quality target alongside the target threshold.
2. WHEN the merge phase completes in all-strategies mode, THE Pipeline SHALL display a summary table with one row per strategy showing strategy name, output file path, size in MB, space saved vs. extracted video, and the final measured metric values for each quality target alongside the target threshold.
3. THE Pipeline SHALL compute space saved as `(1 - output_size / extracted_size) * 100` percent.
4. IF the extracted video size is unavailable, THE Pipeline SHALL omit the savings column and log a warning.
5. IF final quality metrics are unavailable for a strategy (e.g. measurement was skipped), THE Pipeline SHALL display `"N/A"` for the metric columns.
6. THE summary SHALL be emitted at `info` level and formatted as a readable table using fixed-width columns.

---

### Requirement 8

**User Story:** As a pipeline operator, I want the optimization summary table to show the total size of successful test chunks, so that the size comparison is meaningful.

#### Acceptance Criteria

1. THE Optimization Summary SHALL display the total size of all successfully encoded test chunks for each strategy, computed as `sum(chunk.file_size_bytes for chunk in successful_chunks)`.
2. THE Optimization Summary SHALL NOT display an average file size per chunk.
3. THE `StrategyTestResult` dataclass SHALL rename `avg_file_size` to `total_file_size` to reflect the correct semantics, and all usages SHALL be updated accordingly.
4. THE `fmt_optimization_summary` function SHALL label the size column as `"Size (MB)"` and display the total size in MB.
