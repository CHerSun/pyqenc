# Design Document — Merge Phase Revamp

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-19
- Completed: 2026-03-19

## Overview

This document describes the design for eight targeted improvements to the merge phase and the shared metrics pipeline. The changes are surgical — no new abstractions are introduced; existing code is corrected and extended in place.

The eight areas of change are:

1. Work-mode banner (first log line)
2. Chunk count scoping in optimal-strategy mode
3. Output filename derived from source stem + strategy
4. Metrics artifact placement relative to final result
5. Metrics normalization applied immediately after parsing
6. Metrics extraction temp-file protocol
7. Post-merge summary (size, savings, metric snapshot)
8. Optimization summary size fix (`avg_file_size` → `total_file_size`)

---

## Architecture

All changes are confined to the following files:

| File | Changes |
|---|---|
| `pyqenc/phases/merge.py` | Items 1, 2, 3, 4, 7 |
| `pyqenc/orchestrator.py` | Items 1, 2, 7 (orchestrator-side chunk count log + summary call) |
| `pyqenc/utils/visualization.py` | Items 4, 5, 6 |
| `pyqenc/phases/optimization.py` | Item 8 |
| `pyqenc/utils/log_format.py` | Items 7, 8 |
| `pyqenc/default_config.yaml` | Item 9 |
| `pyqenc/config.py` | Item 9 |
| `pyqenc/cli.py` | Item 9 |

No new modules are needed. No public API signatures change in a breaking way (the `merge_final_video` signature gains one new optional parameter; `StrategyTestResult.avg_file_size` is renamed to `total_file_size`).

---

## Components and Interfaces

### 1. Work-mode banner

**Location:** `merge_final_video()` in `pyqenc/phases/merge.py`

The current code emits `"Merging optimal strategy: slow+h265"` after the strategy resolution block. The orchestrator also emits `"Found N chunks across M strategies"` before calling `merge_final_video`.

**Design:**

- Move the mode banner to be the very first `logger.info` call inside `merge_final_video`, before any other logic.
- In optimal-strategy mode: `"Mode: optimal strategy — {display_name}"`
- In all-strategies mode: `"Mode: all strategies — {N} strategy(ies): {comma-separated list}"`
- Remove the legacy `"Merging optimal strategy: ..."` and `"Merging N strategy(ies): ..."` messages.
- Remove the `"Found N chunks across M strategies"` log from `_execute_merge` in the orchestrator (it is misleading in optimal mode and redundant with the new banner).

### 2. Chunk count scoping

**Location:** `_execute_merge()` in `pyqenc/orchestrator.py` and `merge_final_video()` in `pyqenc/phases/merge.py`

The orchestrator currently logs `"Found N chunks across M strategies"` (in `_execute_merge`, just before calling `merge_final_video`) using the full `encoded_chunks` dict, which includes all strategies. In optimal mode this count is misleading because it includes chunks from strategies that won't be merged.

**Design:**

- Remove the `"Found N chunks across M strategies"` log from the orchestrator's `_execute_merge` entirely (the new mode banner in `merge_final_video` covers this with accurate, mode-scoped information).
- Inside `merge_final_video`, after the strategy set is resolved, log the chunk count scoped to the resolved strategies only:
  - Optimal mode: count chunks that have the target strategy key.
  - All-strategies mode: count total chunks across all complete strategies.

### 3. Output filename

**Location:** `merge_final_video()` in `pyqenc/phases/merge.py`

Currently: `output_dir / f"output_{safe}.mkv"`

**Design:**

- Add a `source_stem: str | None = None` parameter to `merge_final_video`. The orchestrator passes `job.source.path.stem`.
- When `source_stem` is provided: `output_file = output_dir / f"{source_stem} {safe}.mkv"`
- When `source_stem` is `None` (backward compat): fall back to `f"output_{safe}.mkv"`
- The `_sidecar_path` helper already derives the sidecar from `output_file.with_suffix(".yaml")`, so it automatically follows the new name.
- The existing-output check and sidecar reuse logic use `output_file` as the key, so they also follow automatically.

### 4. Metrics artifact placement

**Location:** `_measure_quality()` in `pyqenc/phases/merge.py` and `evaluate_chunk()` in `pyqenc/utils/visualization.py`

Currently `_measure_quality` passes `output_dir / f"metrics_{safe_strategy}"` as the metrics directory and lets `evaluate_chunk` derive the plot path as `output_dir / f"{encoded.stem}.png"`.

**Design:**

- `_measure_quality` receives `final_result: Path` (the output MKV).
- Intermediate artifacts dir: `output_dir / final_result.stem` (e.g. `final/О чём говорят мужчины Blu-Ray (1080p) (1) slow_h265/`)
- Plot path: `output_dir / f"{final_result.stem}.png"` — passed explicitly via `plot_path` to `evaluate_chunk`.
- Sidecar path: `output_dir / f"{final_result.stem}.yaml"` — already handled by `_sidecar_path(output_file)` since `output_file` is the final result.
- The `evaluate_chunk` signature already accepts `plot_path: Path | None`; no signature change needed there.

### 5. Metrics normalization

**Location:** `analyze_chunk_quality()` and `QualityEvaluator.evaluate_chunk()` in `pyqenc/utils/visualization.py`; `ChunkEncoder.encode_chunk()` in `pyqenc/phases/encoding.py`

Currently `analyze_chunk_quality` returns raw `ChunkQualityStats` where SSIM is on 0–1 and PSNR is unbounded. The `normalize_metric` function already exists in `pyqenc/quality.py`.

**Design:**

Apply normalization in `analyze_chunk_quality` after computing statistics, before returning `ChunkQualityStats`:

```python
for metric_type, stats in result.items():
    for stat_key in ("min", "median", "max", "std"):
        if stats[stat_key] is not None:
            stats[stat_key] = normalize_metric(metric_type, stats[stat_key])
```

This means:
- `ChunkQualityStats` values are always normalized after `analyze_chunk_quality` returns.
- `MetricsSidecar.metrics` (built from `all_metrics` in `encode_chunk`) will contain normalized values.
- The merge sidecar `metrics` dict (built from `evaluation.metrics` in `_measure_quality`) will contain normalized values.
- Target evaluation in `evaluate_chunk` compares normalized actual values against user-supplied targets (which are already on 0–100 scale per Req 5.5).
- Graph annotations in `create_unified_plot` already scale SSIM × 100 for display — this scaling must be removed since values are now pre-normalized.
- The `_extract_key_stats` helper and `compute_statistics` operate on raw DataFrame values; normalization is applied to the `ChunkQualityStats` result dict, not to the DataFrame itself.

**Impact on `create_unified_plot`:**

The plot currently multiplies SSIM values by 100 for display (`plot_values = plot_values * 100` for SSIM). Since values are now pre-normalized, this multiplication must be removed. The y-axis range and labels remain unchanged (0–100 for all metrics).

The x-axis currently shows raw frame numbers. Since the rest of the pipeline is time-based and the graph is used to validate specific moments in the video, the x-axis should display both the adjusted frame number (accounting for the subsampling factor) and the corresponding timestamp. The dual-label approach uses matplotlib's secondary x-axis (`ax.secondary_xaxis`) or a custom tick formatter that produces two-line tick labels: timestamp (`HH:MM:SS`) on the top line and adjusted frame number on the bottom line. Because raw metric files are frame-based only (no timestamps), timestamps are derived from frame numbers assuming constant framerate: `timestamp = frame_number / fps`. The `fps` value must be passed to `create_unified_plot` (new optional parameter `fps: float | None = None`). When `fps` is provided, tick labels show both timestamp and frame number; when `None`, the axis falls back to raw frame numbers only as before.

The `fps` value is available from `VideoMetadata` of the encoded file, which `QualityEvaluator.evaluate_chunk` already probes for the progress bar duration. It should be passed through to `create_unified_plot` via `analyze_chunk_quality`.

**Impact on `_save_stats_file`:**

Currently multiplies SSIM stats by 100 for display. Remove this scaling since values are pre-normalized.

**Impact on `_log_metrics_summary` in `merge.py`:**

No change needed — it already formats values with `%.2f` and compares against `target.value`.

### 6. Metrics extraction temp-file protocol

**Location:** `QualityEvaluator._generate_metrics()` in `pyqenc/utils/visualization.py`

Currently the method uses a UUID-based `tmp_prefix` for the ffmpeg output filenames (to avoid special characters in filter graphs), then renames to final paths. The UUID-named files do not carry the `.tmp` extension while ffmpeg is running, so the standard cleanup routine won't catch them on interrupted runs.

**Design:**

Change the ffmpeg output filenames to use the `.tmp` extension directly: `<uuid>.psnr.tmp`, `<uuid>.ssim.tmp`, `<uuid>.vmaf.tmp`. This satisfies both requirements simultaneously — the UUID prefix keeps special characters out of the ffmpeg filter graph, and the `.tmp` extension means the standard cleanup routine will remove them on the next run if the process was interrupted.

After all ffmpeg processes complete successfully, perform exactly one rename per file: `<uuid>.<metric>.tmp` → `<final_path>` (the canonical name in the target directory). No intermediate staging step.

```python
# ffmpeg writes to:
tmp_path = cwd / f"{uuid_prefix}.{metric.value}.tmp"

# after success, single rename to final location:
tmp_path.replace(final_path)
```

### 7. Post-merge summary

**Location:** `pyqenc/utils/log_format.py` (new `fmt_merge_summary` function) and `pyqenc/orchestrator.py` (`_execute_merge`)

The summary is emitted by the orchestrator after `merge_final_video` returns successfully, using data from `MergeResult` and the job state.

**Data needed:**
- `result.output_files`: `dict[str, Path]` — strategy → output path
- `result.final_metrics`: `dict[str, dict[str, float]]` — strategy → normalized metrics
- `result.targets_met`: `dict[str, bool]`
- `config.quality_targets`: list of targets (for column headers)
- Extracted video size: `job.source.file_size_bytes` (source video) or size of extracted video stream from `work_dir/extracted/`

For the "savings" column, the most meaningful reference is the extracted video stream (the lossless/remuxed intermediate), not the source. The orchestrator can look for the extracted video file in `work_dir/extracted/` and use its size. If not found, fall back to source size.

**Format (optimal mode):**

```
══════════════════════════════════════════════════════════════════════════
MERGE SUMMARY
══════════════════════════════════════════════════════════════════════════
  Strategy  : slow_h265
  Output    : О чём говорят мужчины Blu-Ray (1080p) (1) slow_h265.mkv
  Size      : 4 231.4 MB  (saved 77.0% vs 18 420.0 MB reference)
  Targets   : vmaf-min≥85 → 91.2 ✔   ssim-min≥95 → 96.1 ✔
══════════════════════════════════════════════════════════════════════════
```

**Format (all-strategies mode, 4 strategies):**

```
══════════════════════════════════════════════════════════════════════════
MERGE SUMMARY  (reference: 18 420.0 MB)
══════════════════════════════════════════════════════════════════════════
  Strategy              Size (MB)   Saved   vmaf-min≥85   ssim-min≥95
  ────────────────────  ─────────   ─────   ───────────   ───────────
  slow_h265              4 231.4    77.0%   91.2 ✔        96.1 ✔
  medium_h265            5 102.8    72.3%   89.4 ✔        95.8 ✔
  slow_h264              6 840.1    62.9%   90.1 ✔        96.3 ✔
  medium_h264            8 215.3    55.4%   88.7 ✔        95.2 ✔
══════════════════════════════════════════════════════════════════════════
```

The filename is shown without the full path (basename only) to keep the table readable. The full path is already logged during the merge step itself.

The `fmt_merge_summary` function in `log_format.py` accepts:
- `mode: Literal["optimal", "all"]`
- `output_files: dict[str, Path]`
- `final_metrics: dict[str, dict[str, float]]`
- `targets_met: dict[str, bool]`
- `quality_targets: list[QualityTarget]`
- `reference_size_bytes: int | None`

Two separate formatting functions are defined:
- `fmt_merge_summary_optimal(...)` — key-value block for single strategy
- `fmt_merge_summary_all(...)` — table format for multiple strategies

A dispatcher `fmt_merge_summary(mode, ...)` calls the appropriate one.

### 8. Optimization summary size fix

**Location:** `pyqenc/phases/optimization.py` and `pyqenc/utils/log_format.py`

`StrategyTestResult.avg_file_size` is populated in the optimization phase. The name implies average but the usage context (comparing strategies) calls for total size of test chunks.

**Design:**

- Rename `StrategyTestResult.avg_file_size` → `total_file_size` with updated docstring.
- Update all assignments in `optimization.py` that set `avg_file_size`.
- Update `fmt_optimization_summary` in `log_format.py` to use `total_file_size` and label the column `"Total size (MB)"`.
- Size values in MB must be right-aligned and formatted with a space as the thousands separator (e.g. `4 231.4`) for easy visual comparison of order of magnitude. This formatting applies to both the optimization summary table and the merge summary.- Update the orchestrator's `_execute_optimization` which logs `test_result.avg_file_size`.

### 9. Metrics sampling factor — config + CLI

**Location:** `pyqenc/default_config.yaml`, `pyqenc/config.py` (`ConfigManager`), `pyqenc/cli.py`, `pyqenc/models.py` (`PipelineConfig`)

`PipelineConfig.metrics_sampling` (renamed from `subsample_factor`) already exists with a hardcoded default of `10`. The goal is to make it readable from the config file and overridable from the CLI. The internal field name is renamed to `metrics_sampling` to match the CLI argument name.

**Design:**

Add a `metrics` section to `default_config.yaml`:

```yaml
metrics:
  sampling: 10
```

Add a `get_metrics_sampling() -> int` method to `ConfigManager` that reads `config["metrics"]["sampling"]` with a fallback of `10` if the section is absent (backward compatibility with user configs that predate this change).

Add `--metrics-sampling` argument to the `_add_quality_arguments` helper in `cli.py` (which is already shared by `auto`, `encode`, and `merge` subcommands). The argument:
- type: `int`
- default: `None` (meaning "use config value")
- metavar: `N`
- help: `"Metrics sampling factor: measure every N-th frame. Min: 1 (every frame). Default: 10. Values above 30 are not recommended (increases measurement volatility)."`

In `_cmd_auto` (and any other command handlers that build `PipelineConfig`), resolve the final value:

```python
metrics_sampling = args.metrics_sampling if args.metrics_sampling is not None \
                   else config_manager.get_metrics_sampling()
```

The resolved value is passed to `PipelineConfig.metrics_sampling`. No validation against prior runs is performed — the user is free to change it between reruns. Changing the value between reruns may cause measurement volatility but does not affect actual encoding results.

---

## Data Models

No new Pydantic models. Changes to existing dataclasses:

### `StrategyTestResult` (optimization.py)

```python
@dataclass
class StrategyTestResult:
    strategy: str
    total_file_size: float = 0.0   # renamed from avg_file_size
    avg_crf: float = 0.0
    ...
```

### `merge_final_video` signature addition

```python
def merge_final_video(
    ...
    source_stem: str,   # required — stem of the source video filename
    ...
) -> MergeResult:
```

---

## Error Handling

- `merge_final_video` requires `source_stem: str` (non-optional). The orchestrator always has `job.source.path.stem` available at merge time. If `job` is `None` (which should not happen in normal execution), the orchestrator logs a `critical` error and aborts the merge phase.
- If the extracted video file is not found for the summary savings calculation, log a `warning` and display `"N/A"` for savings.
- Normalization of `inf` PSNR values: `normalize_metric(PSNR, inf)` returns `min(inf, 100.0) = 100.0` — already handled by the existing implementation.
- If the `.tmp` rename fails during metric extraction (e.g. cross-device), log a `warning` and leave the UUID file in place; the metric is still usable if the UUID file exists.

---

## Testing Strategy

- Unit test: `normalize_metric` applied to SSIM 0.95 → 95.0, PSNR 45.0 → 45.0, PSNR inf → 100.0, VMAF 87.3 → 87.3.
- Unit test: `_strategy_safe_name` and output filename construction.
- Unit test: `fmt_merge_summary` output format for both modes.
- Unit test: `StrategyTestResult.total_file_size` rename — ensure `avg_file_size` attribute no longer exists.
- Integration: run the pipeline on the sample video and verify the output filename matches `f"{source.stem} {safe_strategy}.mkv"`.
