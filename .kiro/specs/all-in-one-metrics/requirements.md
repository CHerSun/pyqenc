# Requirements: All-in-One Metrics Pass
<!-- markdownlint-disable MD024 -->

- Created: 2026-04-11
- Completed: 2026-04-11

## Cross-Spec Notes

Compared against **vif-metric-support** (created 2025-07-22, completed 2026-04-10):

- **`run_metric` → `run_metrics`**: `vif-metric-support` added VIF to the existing `run_metric` + `asyncio.gather` pattern (4 separate ffmpeg processes). This spec replaces all of that with a single `run_metrics` call using `split[]` — one ffmpeg pass for all metrics simultaneously.
- **VIF filter changed**: `vif-metric-support` used the `vif` lavfi filter writing a plain-text log file (`parse_vif_file` read `n:0 vif_scale0:… vif:…` lines). This spec embeds VIF inside the VMAF pass via `feature=name=vif` — VIF data is now parsed from the VMAF JSON by `parse_vif_file`, not from a separate log file.
- **`MetricData` removed**: `vif-metric-support` introduced `MetricData(df, column)` as a thin wrapper. This spec removes it entirely — the pipeline now passes `pd.DataFrame` directly.
- **`complexity` field removed**: `vif-metric-support` kept `MetricInfo.complexity` for progress bar weighting across multiple ffmpeg processes. This spec removes it — a single process reports time linearly, so `_total_complexity = duration_seconds`.
- **`_extract_key_stats` made public**: Renamed to `extract_key_stats` and exposed as part of the clean metrics API alongside `parse_metrics`, `normalize_metrics`, and `compute_metric_stats`.
- **`create_unified_plot` signature changed**: Now accepts `df_norm: pd.DataFrame` instead of `dict[MetricType, MetricData]`.

## Overview

Two goals in one spec:

1. **Single ffmpeg pass** — replace 3 separate ffmpeg runs with one pass using `split[]`, saving ~40% wall-clock time at factor=3. PSNR and SSIM use `select` at the same factor as VMAF's `n_subsample`.
2. **Clean metrics API** — expose a composable, pipeline-friendly API: `run_metrics` → `parse_metrics` → `normalize_metrics` → `compute_metric_stats` → `create_unified_plot`.

## Requirements

### Requirement 1 — `run_metrics` replaces `run_metric`

**1.1** Replace `run_metric(metric, ...)` with `run_metrics(metrics, ...)` that accepts `metrics: Iterable[MetricType]` and runs a single ffmpeg process computing all requested metrics.

**1.2** The function must be `async` and return `FFmpegRunResult`.

**1.3** If `metrics` is empty, raise `ValueError`.

**1.4** `run_metrics` must accept the same common parameters as the old `run_metric`: `distorted`, `reference`, `crop_distorted`, `crop_reference`, `duration`, `width`, `use_gpu`, `subsample`, `output_prefix`, `cwd`, `progress_callback`, `output_extension`.

**1.5** The old `run_metric` function must be removed. All call sites must be updated to `run_metrics`.

### Requirement 2 — Dynamic filter graph construction

**2.1** If `MetricType.VMAF` is in `metrics` (or `MetricType.VIF` is in `metrics`), the VMAF filter must be included. VIF is always embedded in VMAF via `feature=name=vif` — it is never a separate filter.

**2.2** If `MetricType.VIF` is in `metrics` but `MetricType.VMAF` is not, VMAF must still be added to the filter graph (VIF requires it). The VMAF output file is still written.

**2.3** PSNR and SSIM filters are included only when their respective `MetricType` is in `metrics`.

**2.4** The `split` count on each input stream equals the number of independent filter branches (PSNR + SSIM + VMAF, where VMAF covers VIF). When only one branch is needed, `split` is omitted (no `split=1`).

**2.5** PSNR and SSIM use stream-level `select='not(mod(n,subsample))'` on their branches when `subsample > 1`, matching the current per-metric behaviour. This gives the best wall-clock time at all factors (see benchmark).

**2.6** VMAF applies `n_subsample=subsample` when `subsample > 1`.

**2.7** Crop and scale are applied once on the shared input streams before the split.

**2.8** The filter graph must be valid ffmpeg syntax for all combinations of metrics.

### Requirement 3 — Output file naming

**3.1** Each metric's output file is named `{output_prefix}{metric.value}{ext}` — identical to the old per-metric naming so parsers are unchanged.

**3.2** Default extensions: `.log` for PSNR and SSIM, `.json` for VMAF. Overridable via `output_extension`.

### Requirement 4 — `MetricInfo` extension for filter role

**4.1** Add a `subsample_via_filter` boolean field to `MetricInfo`. When `True`, the metric uses stream-level `select` for subsampling on its branch (PSNR, SSIM). When `False`, subsampling is handled internally (VMAF via `n_subsample`) or not applicable (VIF).

**4.2** `subsample_via_filter` must be `False` for VMAF and VIF, `True` for PSNR and SSIM.

**4.3** `run_metrics` uses `subsample_via_filter` to decide which branches get `select` — no hardcoded metric checks.

### Requirement 5 — `_generate_metrics` updated

**5.1** `_generate_metrics` must call `run_metrics` with all four `MetricType` values in a single `await` instead of `asyncio.gather` over separate `run_metric` calls.

**5.2** The progress callback receives `out_time_s` from the single ffmpeg process. Weight = `1.0`, total = `duration_seconds`. No complexity weighting is needed — one process reports time linearly from 0 to duration.

**5.3** `QualityArtifacts` construction is unchanged — paths are derived from `output_prefix` and metric values as before.

### Requirement 6 — Progress bar total

**6.1** `_total_complexity` (the ProgressBar total) must equal `duration_seconds` — one ffmpeg run, linear time reporting.

**6.2** The per-tick advance in the callback is `delta * 1.0` (i.e. just `delta`). The `complexity` field on `MetricInfo` is no longer used for progress bar weighting.

### Requirement 7 — `parse_metrics` — unified parser

**7.1** Add `parse_metrics(artifacts: QualityArtifacts, factor: int = 1) -> pd.DataFrame` in `pyqenc/utils/visualization.py`.

**7.2** The function dispatches to the individual parsers (`parse_psnr_file`, `parse_ssim_file`, `parse_vmaf_file`, `parse_vif_file`) for each non-`None`, existing path in `artifacts`.

**7.3** Results are joined into a single DataFrame via `pd.concat(..., axis=1)` on the shared `frameNum` index. All parsers produce the same frame numbers at the same factor (PSNR/SSIM: `(n-1)*factor`; VMAF/VIF: `n` from JSON — both yield `{0, factor, 2*factor, ...}`), so the join is lossless with no NaN.

**7.4** Column names are `MetricType.value` strings (`"psnr"`, `"ssim"`, `"vmaf"`, `"vif"`).

**7.5** If no parseable files are found, raise `ValueError`.

**7.6** Individual parse failures are caught, logged as warnings, and that metric is omitted from the result (same as current `analyze_chunk_quality` behaviour).

### Requirement 8 — `normalize_metrics` — display-scale conversion

**8.1** Add `normalize_metrics(df_raw: pd.DataFrame) -> pd.DataFrame` in `pyqenc/utils/visualization.py`.

**8.2** For each column whose name matches a `MetricType.value`, apply `MetricType(col).info.normalize(series)`.

**8.3** Returns a new DataFrame with the same index and columns, values on the 0–100 display scale.

**8.4** Columns not matching any `MetricType.value` are passed through unchanged (forward-compatible).

### Requirement 9 — `compute_metric_stats` — public stats function

**9.1** Make `_extract_key_stats` public as `extract_key_stats(full_stats, metric_type) -> MetricStats`.

**9.2** Add `compute_metric_stats(df_norm: pd.DataFrame) -> ChunkQualityStats` that iterates columns, calls `compute_statistics` then `extract_key_stats` for each recognized `MetricType`, and returns the full stats dict.

**9.3** `analyze_chunk_quality` is refactored internally to use `parse_metrics` + `normalize_metrics` + `compute_metric_stats` — its public signature is unchanged.

### Requirement 10 — `create_unified_plot` accepts normalized DataFrame

**10.1** Add an overload / new signature: `create_unified_plot(df_norm: pd.DataFrame, factor: int, output_path: Path, ...)` accepting a normalized DataFrame directly.

**10.2** The existing `dict[MetricType, MetricData]` signature is removed. `MetricData` is no longer needed as a public type.

**10.3** The plot derives per-metric Series from DataFrame columns: `df_norm[metric.value]` for each column that matches a `MetricType.value`.

**10.4** All existing plot behaviour (dual axes, summary boxes, bar subplots, aggregation) is preserved.

**10.5** `_finish_evaluation` is updated to call `parse_metrics` + `normalize_metrics` + `compute_metric_stats` + `create_unified_plot` in sequence.

### Requirement 11 — Backward compatibility

**11.1** `analyze_chunk_quality` public signature is unchanged — existing callers continue to work.

**11.2** All existing tests must continue to pass.
