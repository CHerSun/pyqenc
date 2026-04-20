# Design: All-in-One Metrics Pass
<!-- markdownlint-disable MD024 -->

- Created: 2026-04-11
- Completed: 2026-04-11

## Cross-Spec Notes

Compared against **vif-metric-support** (created 2025-07-22, completed 2026-04-10):

- **`run_metric` → `run_metrics`**: `vif-metric-support` ran 4 separate ffmpeg processes via `asyncio.gather`. This spec replaces that with a single `run_metrics` call using `split[]` filter graph — ~40% wall-clock saving at factor=3.
- **VIF filter changed**: `vif-metric-support` used the standalone `vif` lavfi filter (plain-text log, `n:0 vif_scale0:… vif:…` format). This spec embeds VIF inside the VMAF pass via `feature=name=vif`; `parse_vif_file` now reads from the VMAF JSON (`integer_vif_scale0`–`integer_vif_scale3`), not a separate log file.
- **`MetricData` removed**: The `MetricData(df, column)` wrapper introduced in `vif-metric-support` is gone. All pipeline functions now work with `pd.DataFrame` directly.
- **`complexity` field removed from `MetricInfo`**: Was used for progress bar weighting across N processes. With a single process, `_total_complexity = duration_seconds` and weight = `1.0`.
- **`_extract_key_stats` → `extract_key_stats`**: Made public as part of the composable API: `parse_metrics` → `normalize_metrics` → `compute_metric_stats` → `create_unified_plot`.
- **`create_unified_plot` signature**: Changed from `dict[MetricType, MetricData]` to `df_norm: pd.DataFrame`.

## Overview

Replace 3 separate ffmpeg runs with a single pass using `split[]`. PSNR and SSIM use `select` at the same factor as VMAF's `n_subsample`; VIF is always embedded in VMAF. Saves ~40% wall-clock time at factor=3 (production default).

## Architecture

### Filter graph structure

```
[0:v]<crop><scale>,split=N[d1]...[dN];
[d1]select='not(mod(n,F))',setpts=PTS-STARTPTS[main_psnr];
[d2]select='not(mod(n,F))',setpts=PTS-STARTPTS[main_ssim];
[dN]setpts=PTS-STARTPTS[main_vmaf];
[1:v]<crop><scale>,split=N[r1]...[rN];
[r1]select='not(mod(n,F))',setpts=PTS-STARTPTS[ref_psnr];
[r2]select='not(mod(n,F))',setpts=PTS-STARTPTS[ref_ssim];
[rN]setpts=PTS-STARTPTS[ref_vmaf];
[main_psnr][ref_psnr]psnr=stats_file=<prefix>psnr<ext>
[main_ssim][ref_ssim]ssim=stats_file=<prefix>ssim<ext>
[main_vmaf][ref_vmaf]libvmaf=n_threads=4:n_subsample=F:log_path=<prefix>vmaf<ext>:log_fmt=json:feature=name=vif
```

- `N` = number of independent branches (PSNR + SSIM + VMAF; VIF is not a branch)
- When `N == 1`, `split` is omitted entirely
- PSNR/SSIM branches: `select='not(mod(n,F))'` when `F > 1` (same factor as VMAF)
- VMAF branch: `n_subsample=F` when `F > 1`; always includes `feature=name=vif`

Benchmark confirmed all-in-one+select is strictly better than all-in-one without select at all factors (58% saving at f=10 vs 32%). See `metric-complexity-benchmark.md`.

### `MetricInfo` extension

Add `subsample_via_filter: bool` field:
- `True` for PSNR, SSIM — historically used stream-level `select`; now unused in the combined pass but kept for documentation and potential future single-metric use
- `False` for VMAF, VIF — subsampling is internal (`n_subsample`) or not applicable

### `run_metrics` function

```python
async def run_metrics(
    metrics:           Iterable[MetricType],
    distorted:         Path,
    reference:         Path,
    crop_distorted:    CropParams,
    crop_reference:    CropParams,
    duration:          int,
    width:             int,
    use_gpu:           bool,
    subsample:         int,
    output_prefix:     str,
    cwd:               Path | None             = None,
    progress_callback: ProgressCallback | None = None,
    output_extension:  str | None              = None,
) -> FFmpegRunResult:
```

Key logic:
1. Deduplicate and sort `metrics` into a stable set. Raise `ValueError` if empty.
2. If `MetricType.VIF` is requested but `MetricType.VMAF` is not, add `MetricType.VMAF` to the active set (VIF requires it).
3. Determine independent branches: `{PSNR, SSIM, VMAF} ∩ active_set` (VIF is not a branch).
4. Build shared input filters (crop + scale + setpts), with `split=N` when `N > 1`.
5. Build per-branch filter strings.
6. Assemble full `-filter_complex` string.
7. Call `run_ffmpeg_async`.

### `_generate_metrics` update

Replace `asyncio.gather(*[_run_one(m) for m in [...]])` with a single `await run_metrics(...)` call. Progress callback is simplified: total = `duration_seconds`, weight = `1.0` — ffmpeg reports `out_time_s` linearly from 0 to duration, no complexity weighting needed.

### Progress bar total

```python
_total_complexity = duration_seconds or 0.0  # one ffmpeg run, linear time reporting
```

The old `1.0 + sum(M/f)` formula was needed when N separate ffmpeg processes each reported their own time. With a single process it's just the clip duration.

### `analyze_chunk_quality` — no change needed

All parsers continue to be called with the same `factor` value. PSNR/SSIM use `select` at the same rate as VMAF's `n_subsample`, so frame indices are consistent across all metrics.

### `create_unified_plot` — mixed frame densities

The existing aggregation logic already operates per-metric independently:
```python
n_points = len(plot_values)
if n_points >= _TARGET_PLOT_POINTS * _MIN_POINTS_PER_BIN:
    # rolling aggregate
else:
    # raw points
```
No change needed here — each metric's `n_points` naturally differs. The x-axis range already uses `max(idx.max() for idx in frame_index.values())`.

## Components changed

| Component | Change |
|---|---|
| `pyqenc/quality.py` — `MetricInfo` | Add `subsample_via_filter: bool` field |
| `pyqenc/quality.py` — `run_metric` | Remove; replace with `run_metrics` |
| `pyqenc/utils/visualization.py` — `_generate_metrics` | Single `await run_metrics(...)` call |
| `pyqenc/utils/visualization.py` — `_total_complexity` | `duration_seconds` (linear, single run) ✓ done |
| `pyqenc/utils/visualization.py` — `_make_progress_callback` | Weight = `1.0` (linear) ✓ done |
| `pyqenc/utils/visualization.py` — `analyze_chunk_quality` | No change needed — all metrics use same factor |
| `pyqenc/utils/visualization.py` — `_finish_evaluation` | No change needed |
| `pyqenc/utils/visualization.py` — `create_unified_plot` | No change needed (already per-metric) |

## Error handling

| Scenario | Handling |
|---|---|
| `metrics` empty | `ValueError` raised before ffmpeg |
| ffmpeg exits non-zero | `FFmpegRunResult(success=False)` returned; `_generate_metrics` logs warning per missing file |
| One metric's output file missing | `_finish_evaluation` passes `None` for that metric; `analyze_chunk_quality` skips it |
| VIF requested without VMAF | VMAF silently added to active set |

## Clean Metrics API

### Data flow

```
run_metrics(...)          → FFmpegRunResult + .tmp files on disk
parse_metrics(artifacts)  → pd.DataFrame  (frameNum index, raw metric columns)
normalize_metrics(df_raw) → pd.DataFrame  (same shape, 0–100 scale)
compute_metric_stats(df)  → ChunkQualityStats
create_unified_plot(df, …)→ PNG file
```

### `parse_metrics`

```python
def parse_metrics(artifacts: QualityArtifacts, factor: int = 1) -> pd.DataFrame:
```

Dispatches to individual parsers for each non-`None` existing path in `artifacts`.
Joins results via `pd.concat(..., axis=1)` on the shared `frameNum` index.

Frame number alignment at factor=F:
- PSNR/SSIM: raw `n` is 1-based sequential (1…N); parser maps to `(n-1)*F` → `{0, F, 2F, …}`
- VMAF/VIF: raw `frameNum` is already the true video frame number → `{0, F, 2F, …}`

All four metrics produce the same index at the same factor — inner join is lossless, no NaN.

Result: `DataFrame(index=frameNum, columns=["psnr","ssim","vmaf","vif"])` — only columns for metrics that were successfully parsed.

### `normalize_metrics`

```python
def normalize_metrics(df_raw: pd.DataFrame) -> pd.DataFrame:
```

Applies `MetricType(col).info.normalize(series)` to each column whose name matches a `MetricType.value`. Returns a new DataFrame on the 0–100 display scale. Unknown columns pass through unchanged.

### `compute_metric_stats`

```python
def compute_metric_stats(df_norm: pd.DataFrame) -> ChunkQualityStats:
```

For each column matching a `MetricType.value`: calls `compute_statistics(series)` then `extract_key_stats(full_stats, metric_type)`. Returns `ChunkQualityStats`.

`_extract_key_stats` is renamed to `extract_key_stats` (public).

### `create_unified_plot` — new signature

```python
def create_unified_plot(
    df_norm:             pd.DataFrame,
    factor:              int,
    output_path:         Path,
    title:               str = "Video Quality Metrics Analysis",
    styles:              dict[MetricType, MetricVisualStyle] | None = None,
    fps:                 float | None = None,
    chunk_start_seconds: float = 0.0,
) -> dict[MetricType, _MetricStatistics]:
```

Derives per-metric Series from `df_norm[metric.value]` for each column. All existing plot behaviour preserved. `MetricData` wrapper is removed — no longer needed.

### `_finish_evaluation` — updated pipeline

```python
df_raw  = parse_metrics(artifacts, factor=subsample_factor)
df_norm = normalize_metrics(df_raw)
stats   = compute_metric_stats(df_norm)
plot    = create_unified_plot(df_norm, subsample_factor, plot_path, ...)
```

`analyze_chunk_quality` is refactored internally to use the same pipeline but its public signature is unchanged.

### `MetricData` removal

`MetricData` (a thin `(df, column)` wrapper) is removed. All internal code that built `dict[MetricType, MetricData]` is replaced with the single DataFrame approach.

## Components changed (full list)

| Component | Change |
|---|---|
| `pyqenc/quality.py` — `MetricInfo` | Add `subsample_via_filter: bool` field ✓ done |
| `pyqenc/quality.py` — `run_metric` | Remove; replace with `run_metrics` ✓ done |
| `pyqenc/utils/visualization.py` — `_generate_metrics` | Single `await run_metrics(...)` ✓ done |
| `pyqenc/utils/visualization.py` — `_total_complexity` | `1.0 + sum(M/f)` ✓ done |
| `pyqenc/utils/visualization.py` — `parse_metrics` | New public function |
| `pyqenc/utils/visualization.py` — `normalize_metrics` | New public function |
| `pyqenc/utils/visualization.py` — `compute_metric_stats` | New public function; `_extract_key_stats` → `extract_key_stats` |
| `pyqenc/utils/visualization.py` — `create_unified_plot` | New signature: `df_norm: pd.DataFrame` instead of `dict[MetricType, MetricData]` |
| `pyqenc/utils/visualization.py` — `analyze_chunk_quality` | Refactored internally; public signature unchanged |
| `pyqenc/utils/visualization.py` — `_finish_evaluation` | Uses new pipeline |
| `pyqenc/quality.py` — `MetricData` | Remove |
