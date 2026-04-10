# Design: VIF Metric Support
<!-- markdownlint-disable MD024 -->

- Created: 2025-07-22
- Completed:

## Overview

This feature adds VIF (Video Information Fidelity) as a fully supported quality metric alongside PSNR, SSIM, and VMAF. The implementation is deliberately minimal: VIF reuses every existing pipeline mechanism — `MetricInfo`, `MetricType`, `QualityTarget`, `QualityEvaluator`, `analyze_chunk_quality`, unified plot, and sidecar YAML. The only genuinely new code is the ffmpeg filter command branch in `run_metric` and the VIF log parser (`parse_vif_file` / `_parse_vif_line`).

The feature also ships two pipeline simplifications (Requirement 12):

1. Raw metric log files are no longer renamed from `<uuid>.<metric>.tmp` to canonical names — they stay as `.tmp` throughout their lifetime and are returned directly from `_generate_metrics`.
2. The `.stats` sidecar writing (`_save_stats_file`) and the post-plot cleanup function (`_cleanup_raw_metric_files`) are removed. After the sidecar YAML is written, the pipeline deletes the raw `.tmp` metric files (best-effort). The startup `.tmp` glob at the beginning of each phase is the safety net for any files left behind by interrupted runs.

### What is reused unchanged
- `MetricInfo.normalize()`, `passes()`, `deficit()` — already direction-aware
- `_METRIC_INFO[MetricType.VIF]` — already populated
- `MetricType` enum — `VIF` already present
- `compute_statistics()`, `_extract_key_stats()` — metric-agnostic
- `create_unified_plot()` — already iterates `metrics` dict dynamically for line rendering; only the hardcoded loops for summary boxes and bar subplots need updating
- `_write_sidecar()` in `measure.py` — already flattens `ChunkQualityStats` generically

### What changes
- `run_metric` — add `elif metric == MetricType.VIF:` branch
- `parse_vif_file` / `_parse_vif_line` — new parser in `visualization.py`
- `analyze_chunk_quality` — add `vif_log` parameter and VIF parsing block; remove `_save_stats_file` calls and `_cleanup_raw_metric_files` call; remove `metric_files` dict
- `QualityArtifacts` — add `vif_log: Path | None = None`; remove `stats_files`
- `DEFAULT_METRIC_STYLES` — add VIF entry; fix `lossless_threshold` for PSNR
- `create_unified_plot` — extend summary box loop and bar subplot loop to include `MetricType.VIF`; update `_configure_pct_axis` label
- `QualityEvaluator._generate_metrics` — remove rename step; return `QualityArtifacts` directly (with `.tmp` paths, `plot=None`)
- `QualityEvaluator.evaluate_chunk_async` / `evaluate_chunk` — receive `QualityArtifacts` from `_generate_metrics`; add `metrics_output_dir` param
- `QualityEvaluator._finish_evaluation` — accept `QualityArtifacts`; fill in `plot`; fix target evaluation to use `MetricType(target.metric).info.passes()`
- `QualityTarget.parse` — add `"vif"` to `valid_metrics`
- `run_measure` in `measure.py` — remove `metrics_dirs` subdirectory creation; add startup `.tmp` glob cleanup; remove `METRICS_SUBDIR_SUFFIX` import
- `pyqenc/cli.py` — replace inline `--targets` help string with `_QUALITY_TARGET_HELP` constant
- `MetricInfo` docstring — update field name references (Req 1 AC8)

## Architecture

The pipeline data flow is unchanged. VIF slots in as a fourth metric alongside PSNR, SSIM, and VMAF at every layer:

```
ffmpeg vif filter
      │
      ▼
<uuid>.vif.tmp   (written by run_metric, stays as .tmp)
      │
      ▼
parse_vif_file()
      │  → pd.DataFrame [frameNum → vif_raw]
      ▼
MetricType.VIF.info.normalize()
      │  → 100.0 - raw, clipped to [0, ∞)
      ▼
compute_statistics() → _MetricStatistics
      │
      ▼
_extract_key_stats() → MetricStats
      │
      ├──→ ChunkQualityStats[MetricType.VIF]
      │         │
      │         ├──→ _write_sidecar() → vif_min, vif_median, … in YAML
      │         └──→ target evaluation via MetricType.VIF.info.passes()
      │
      └──→ create_unified_plot() → VIF line on right Y-axis
```

### Pipeline simplification (Req 12) — before vs after

**Before:**
```
run_metric → <uuid>.psnr.tmp
           → rename → <stem>.psnr.log
           → parse
           → _save_stats_file → <stem>.psnr.stats
           → create_unified_plot
           → _cleanup_raw_metric_files (deletes .log + .stats)
```

**After:**
```
run_metric → <uuid>.psnr.tmp  (stays as .tmp, returned directly)
           → parse → delete .tmp immediately after parsing
           → create_unified_plot
           (startup glob deletes any *.tmp left by interrupted runs)
```

The `.tmp` files are deleted as soon as they have been parsed — there is no reason to keep them longer. The startup glob is purely a safety net for interrupted runs where parsing never happened.

## Components and Interfaces

### `pyqenc/quality.py`

#### `MetricInfo` docstring (Req 1 AC8)
Update the docstring to replace `scale_factor` → `_scale_factor`, `clip_lower` → `_clip_lower`, `clip_upper` → `_clip_upper`, and document `_offset`. No code changes — the fields and `normalize()` implementation are already correct.

#### `QualityArtifacts`
```python
@dataclass
class QualityArtifacts:
    psnr_log:  Path | None = None
    ssim_log:  Path | None = None
    vmaf_json: Path | None = None
    vif_log:   Path | None = None   # NEW
    plot:      Path | None = None
    # stats_files removed (Req 12 AC6)
```

#### `run_metric` — VIF branch
Add before the `assert_never(metric)` line:

```python
elif metric == MetricType.VIF:
    vif_ext = output_extension if output_extension is not None else ".log"
    filter_metric = f"vif=stats_file={output_prefix}{metric.value}{vif_ext}"
```

Frame subsampling for VIF uses the same `select='not(mod(n,N))'` path already applied to PSNR/SSIM (the `if metric != MetricType.VMAF and subsample > 1:` guard already covers VIF since VIF is not VMAF).

The full filter graph for VIF becomes:
```
[0:v]<crop><scale>,select='not(mod(n,N))',setpts=PTS-STARTPTS[main];
[1:v]<crop><scale>,select='not(mod(n,N))',setpts=PTS-STARTPTS[ref];
[main][ref]vif=stats_file=<uuid>.vif.tmp
```

#### `QualityTarget.parse`
Replace the hardcoded `valid_metrics` set with a derivation from the enum:
```python
valid_metrics = {m.value for m in MetricType}
```
This means any future `MetricType` member is automatically accepted — no manual update needed.

---

### `pyqenc/utils/visualization.py`

#### `_parse_vif_line(line: str) -> dict[str, float] | None`
Internal helper. VIF log line format (one per frame):
```
n:0 vif_scale0:0.99 vif_scale1:0.98 vif_scale2:0.97 vif_scale3:0.96 vif:0.9850
```
Only `n` (frame index, 0-based) and `vif:` (combined score) are extracted. Per-scale fields are ignored.

```python
def _parse_vif_line(line: str) -> dict[str, float] | None:
    try:
        parts = line.split()
        parsed: dict[str, int | float] = {}
        for part in parts:
            if ":" in part:
                key, value = part.split(":", 1)
                parsed[key] = int(value) if key == "n" else float(value)
        if "n" in parsed and MetricType.VIF.value in parsed:
            return {"n": parsed["n"], MetricType.VIF.value: parsed[MetricType.VIF.value]}
        return None
    except (ValueError, IndexError):
        return None
```

#### `parse_vif_file(file_path: Path, factor: int = 1) -> pd.DataFrame`
VIF logs are **zero-based** (`n:0` for the first frame), unlike PSNR/SSIM which are one-based. Therefore `frameNum = n * factor` (no `- 1` adjustment).

```python
def parse_vif_file(file_path: Path, factor: int = 1) -> pd.DataFrame:
    data: list[dict[str, float]] = []
    with file_path.open("r") as fh:
        for line in fh:
            if parsed := _parse_vif_line(line):
                data.append({
                    _KEY_FRAME_NUM:       int(parsed["n"]) * factor,
                    MetricType.VIF.value: parsed[MetricType.VIF.value],
                })
    if not data:
        raise ValueError(f"Not a VIF log file: {file_path}")
    df = pd.DataFrame(data)
    df.set_index(_KEY_FRAME_NUM, inplace=True)
    return df
```

#### `DEFAULT_METRIC_STYLES` — VIF entry
```python
MetricType.VIF: MetricVisualStyle(
    label              = "VIF",
    color              = _VIF_COLOR,        # "#7B2D8B"  (named constant)
    unit               = MetricType.VIF.info.display_unit,
    y_axis             = "right",
    linestyle          = "-",
    linewidth          = _LINE_WIDTH_DEFAULT,
    lossless_threshold = MetricType.VIF.info.lossless_value,
    lossless_label     = MetricType.VIF.info.lossless_raw_repr,
),
```

Add constants at the top of the constants section:
```python
_VIF_COLOR:       str = "#7B2D8B"   # purple
_VIF_FILL_COLOR:  str = "#C084D4"   # light purple (used for range fill)
```

Also fix the existing PSNR entry — `lossless_threshold` currently uses `MetricType.PSNR.info._clip_upper` (private field). Change to `MetricType.PSNR.info.lossless_value`.

#### `_configure_pct_axis` label update
Change `"SSIM / VMAF (%)"` to `"SSIM / VMAF / VIF"` (no `%` since `display_unit = ""`).

#### `create_unified_plot` — extend metric loops
Two hardcoded loops currently iterate `[MetricType.PSNR, MetricType.SSIM, MetricType.VMAF]`. Both should be replaced with iteration over the `metrics` dict (keyed by `MetricType`) so any present metric is included automatically — no hardcoded list needed:

```python
# Summary boxes
for metric_type in metrics:
    ...

# Bar subplots
for metric_type in metrics:
    ...
```

This means adding a new `MetricType` in future requires no changes to the plot code.

The `has_percentage_metrics` flag should be updated to also check for VIF:
```python
has_percentage_metrics: bool = has_ssim or has_vmaf or (MetricType.VIF in metrics)
```

#### `analyze_chunk_quality` — VIF block + pipeline simplification
New signature:
```python
def analyze_chunk_quality(
    psnr_log:            Path | None = None,
    ssim_log:            Path | None = None,
    vmaf_json:           Path | None = None,
    vif_log:             Path | None = None,   # NEW
    factor:              int         = 1,
    output_path:         Path | None = None,
    title:               str | None  = None,
    generate_plot:       bool        = True,
    fps:                 float | None = None,
    chunk_start_seconds: float       = 0.0,
) -> ChunkQualityStats:
```

Add VIF parsing block (mirrors PSNR/SSIM pattern):
```python
# --- VIF ---
if vif_log is not None:
    try:
        logger.debug("Parsing VIF log: %s", vif_log)
        df = parse_vif_file(vif_log, factor)
        parsed_metrics[MetricType.VIF] = MetricData(df=df, column=MetricType.VIF.value)
        fs = compute_statistics(df[MetricType.VIF.value])
        full_stats[MetricType.VIF]     = fs
        result[MetricType.VIF]         = _extract_key_stats(fs, MetricType.VIF)
        logger.debug("Parsed VIF: %d frames", len(df))
    except Exception as exc:
        logger.warning("Failed to parse VIF from %s: %s", vif_log, exc)
```

Remove:
- `metric_files: dict[MetricType, Path]` tracking dict and all assignments to it
- The `_save_stats_file(...)` call loop at the bottom
- The `_cleanup_raw_metric_files(metric_files)` call
- The `KEEP_RAW_METRICS_FILES` import (no longer needed)

Add a `delete_after_parse: bool = True` parameter to `analyze_chunk_quality`. When `True` (the default), each raw `.tmp` file is deleted immediately after it has been successfully parsed into a DataFrame — there is no reason to keep it longer. Deletion is best-effort: log a warning on failure, do not raise. When `False`, files are left in place (useful for testing or debugging).

```python
# immediately after successful parse of each metric:
if delete_after_parse:
    try:
        metric_file.unlink(missing_ok=True)
        logger.debug("Deleted raw metric tmp file: %s", metric_file.name)
    except Exception as exc:
        logger.warning("Could not delete metric tmp file %s: %s", metric_file.name, exc)
```

Update the `_auto_output_path` helper to also accept `vif_log` — or simplify it to take a `QualityArtifacts` and pick the first non-`None` log path from it:
```python
def _auto_output_path(artifacts: QualityArtifacts) -> Path:
    first = artifacts.psnr_log or artifacts.ssim_log or artifacts.vmaf_json or artifacts.vif_log
    ...
```

Update docstring to remove `.stats` side-effect references.

#### `QualityEvaluator._generate_metrics` — remove rename, add VIF, return `QualityArtifacts`
Change return type from `tuple[Path, Path, Path]` to `QualityArtifacts`. The artifacts object is populated with the `.tmp` paths directly (no rename). `plot` is left `None` — it is filled in by `_finish_evaluation`.

```python
return QualityArtifacts(
    psnr_log  = cwd / f"{tmp_prefix}{MetricType.PSNR.value}.tmp",
    ssim_log  = cwd / f"{tmp_prefix}{MetricType.SSIM.value}.tmp",
    vmaf_json = cwd / f"{tmp_prefix}{MetricType.VMAF.value}.tmp",
    vif_log   = cwd / f"{tmp_prefix}{MetricType.VIF.value}.tmp",
)
```

Log a warning for any path that does not exist after `asyncio.gather` completes. Remove the entire rename loop.

This means adding a future metric only requires updating `QualityArtifacts` — `_generate_metrics` signature stays the same.

#### `QualityEvaluator.evaluate_chunk_async` and `evaluate_chunk`
- Add `metrics_output_dir: Path | None = None` parameter. When provided, pass it as `cwd` to `_generate_metrics` instead of deriving from `output_prefix`. When `None`, use `output_dir` (existing behaviour).
- Receive `QualityArtifacts` from `_generate_metrics` and pass it directly to `_finish_evaluation`.

#### `QualityEvaluator._finish_evaluation`
- Accept `artifacts: QualityArtifacts` (returned by `_generate_metrics`) instead of individual path parameters.
- Pass each log path from `artifacts` to `analyze_chunk_quality`, checking `.exists()` for each.
- Set `artifacts.plot` to the resolved plot path.
- Fix target evaluation: replace `if actual_value < target.value:` with `if not MetricType(target.metric).info.passes(actual_value, target.value):`.

---

### `pyqenc/phases/measure.py`

#### `run_measure` — remove metric subdirs, add startup cleanup
Remove:
- `metrics_dirs` dict and all references
- `metrics_dirs[target_video].mkdir(...)` call
- `METRICS_SUBDIR_SUFFIX` from the import inside `run_measure`

Add startup `.tmp` cleanup before any work begins:
```python
measure_dir = work_dir / MEASURE_DIR
for tmp_file in measure_dir.glob("*.tmp"):
    try:
        tmp_file.unlink()
        logger.debug("Cleaned up stale tmp file: %s", tmp_file.name)
    except Exception as exc:
        logger.warning("Could not delete stale tmp file %s: %s", tmp_file.name, exc)
```

Pass `measure_dir` directly to `_run_metrics` as the output directory (replacing `metrics_dirs[target_video]`).

---

### `pyqenc/cli.py`

#### `_QUALITY_TARGET_HELP` constant
Define before `_add_quality_arguments`:
```python
_QUALITY_TARGET_HELP: str = (
    "Quality targets as comma-separated metric-stat:value pairs "
    "(e.g. 'vmaf-min:95,ssim-med:98,vif-min:95'). "
    "All metrics are normalized to 0–100 where 100 = lossless. "
    "Landmarks: VMAF 95+ good, SSIM 98+ good, PSNR 40–60 typical, VIF 95+ good. "
    "If not specified, uses default from config file."
)
```

Replace the inline `help=` string in `_add_quality_arguments` with `help=_QUALITY_TARGET_HELP`.

## Data Models

### `MetricInfo` for VIF (already implemented)
```python
MetricType.VIF: MetricInfo(
    name              = "VIF",
    id                = "vif",
    higher_is_better  = True,
    _offset           = 100.0,
    _scale_factor     = -1.0,
    _clip_upper       = None,
    _clip_lower       = 0.0,
    lossless_value    = 100.0,
    lossless_raw_repr = "100.0",
    display_unit      = "",
    plot_y_min        = 0.0,
    plot_y_max        = 103.0,
    complexity        = 1.0,
    comparison_range  = 10.0,
    acceptance_delta  = 0.2,
)
```

Normalization: `normalized = 100.0 + raw * (-1.0) = 100.0 - raw`, clipped to `[0.0, ∞)`.
- Raw 0.0 (lossless) → normalized 100.0
- Raw 5.0 → normalized 95.0
- Raw 100.0 → normalized 0.0
- Raw 110.0 → normalized 0.0 (clipped)

### VIF log line format
```
n:0 vif_scale0:0.9912 vif_scale1:0.9876 vif_scale2:0.9834 vif_scale3:0.9801 vif:0.9856
```
- `n` — zero-based frame index (unlike PSNR/SSIM which use 1-based `n`)
- `vif_scale0`–`vif_scale3` — per-scale component values (ignored)
- `vif` — combined VIF score (the only field extracted)

### `QualityArtifacts` (updated)
```python
@dataclass
class QualityArtifacts:
    psnr_log:  Path | None = None
    ssim_log:  Path | None = None
    vmaf_json: Path | None = None
    vif_log:   Path | None = None
    plot:      Path | None = None
```

### Sidecar YAML VIF keys
When `ChunkQualityStats` contains `MetricType.VIF`, the existing flattening loop in `_write_sidecar` automatically produces:
```yaml
vif_min: 94.2
vif_p05: 93.1
vif_p25: 94.8
vif_median: 95.3
vif_p75: 96.1
vif_p95: 97.2
vif_max: 98.4
vif_std: 1.1
```
No VIF-specific code is needed in `_write_sidecar`.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Normalization formula correctness

*For any* `MetricInfo` instance and any finite raw float value, `normalize(raw)` must equal `clip(_offset + raw * _scale_factor, lower=_clip_lower, upper=_clip_upper)`, where `clip` applies each bound only when it is not `None`.

**Validates: Requirements 1.3, 1.7**

### Property 2: VIF lossless normalization

*For any* call to `MetricType.VIF.info.normalize(0.0)`, the result must equal `100.0`.

**Validates: Requirements 2.4**

### Property 3: VIF clip lower

*For any* raw VIF value greater than `100.0` (which would produce a negative result before clipping), `MetricType.VIF.info.normalize(raw)` must equal `0.0`.

**Validates: Requirements 2.5**

### Property 4: Normalize idempotence on already-normalized values

*For any* metric type and any value already on the normalized scale (i.e. produced by a prior `normalize()` call), applying `normalize()` again must return the same value — the normalized scale is a fixed point of the normalization function.

**Validates: Requirements 2.6**

### Property 5: `passes()` direction correctness

*For any* `MetricInfo` with `higher_is_better = True` and any pair `(actual, target)`, `passes(actual, target)` must return `True` if and only if `actual >= target`. For `higher_is_better = False`, it must return `True` if and only if `actual <= target`.

**Validates: Requirements 1.7, 2.7, 2.8**

### Property 6: `parse_vif_file` DataFrame structure

*For any* valid VIF log file, `parse_vif_file(path, factor)` must return a `pd.DataFrame` with index name `"frameNum"` and exactly one column named `"vif"`.

**Validates: Requirements 4.1, 4.2**

### Property 7: `parse_vif_file` zero-based frame indexing

*For any* valid VIF log file and any `factor >= 1`, the `frameNum` index value for the i-th parsed line (0-based) must equal `i * factor`.

**Validates: Requirements 4.3**

### Property 8: `parse_vif_file` finite values

*For any* valid VIF log file, every value in the `"vif"` column of the returned DataFrame must be a finite float (no `NaN`, no `inf`).

**Validates: Requirements 4.6**

### Property 9: `parse_vif_file` skips malformed lines

*For any* VIF log file containing a mix of valid and malformed lines, `parse_vif_file` must return a DataFrame containing exactly the rows corresponding to valid lines, with malformed lines silently skipped.

**Validates: Requirements 4.5**

### Property 10: `analyze_chunk_quality` VIF integration and normalization

*For any* valid VIF log file passed as `vif_log`, the returned `ChunkQualityStats` must contain `MetricType.VIF` and all stat values (min, p05, p25, median, p75, p95, max, std) must be on the normalized 0–100 scale (i.e. equal to `MetricType.VIF.info.normalize(raw_stat)`).

**Validates: Requirements 5.2, 5.3**

### Property 11: `analyze_chunk_quality` backward compatibility

*For any* call to `analyze_chunk_quality` without a `vif_log` argument (or with `vif_log=None`), the returned `ChunkQualityStats` must not contain `MetricType.VIF` and must be identical to what the pre-VIF implementation would have returned.

**Validates: Requirements 5.5**

### Property 12: `QualityTarget.parse` accepts VIF

*For any* target string of the form `"vif-<stat>:<value>"` with a valid statistic and numeric value, `QualityTarget.parse` must return a `QualityTarget` with `metric == "vif"`, the correct statistic, and the correct value.

**Validates: Requirements 7.1, 7.3**

### Property 13: Target evaluation direction-awareness

*For any* `QualityTarget` with `metric == "vif"` and any `ChunkQualityStats` result, the target is considered met if and only if `MetricType.VIF.info.passes(actual, target.value)` returns `True` — i.e. `actual >= target.value` (since VIF is `higher_is_better = True` after normalization).

**Validates: Requirements 7.4**

### Property 14: Sidecar YAML VIF key generation

*For any* `ChunkQualityStats` containing `MetricType.VIF`, the flat dict produced by the sidecar flattening loop must contain keys `vif_min`, `vif_p05`, `vif_p25`, `vif_median`, `vif_p75`, `vif_p95`, `vif_max`, and `vif_std` with the correct values.

**Validates: Requirements 9.1**

### Property 15: `_generate_metrics` returns `.tmp` paths directly

*For any* successful `_generate_metrics` call, all four returned paths must end with `.tmp` — no rename to a canonical extension is performed.

**Validates: Requirements 6.2, 12.1, 12.2**

## Error Handling

| Scenario | Handling |
|---|---|
| VIF ffmpeg process exits non-zero | `run_metric` returns `FFmpegRunResult(success=False)`; `_run_one` logs warning; `_generate_metrics` returns the (non-existent) `.tmp` path; `_finish_evaluation` passes `None` to `analyze_chunk_quality` |
| VIF `.tmp` file missing after ffmpeg | `_generate_metrics` logs `warning`; downstream receives `None` for `vif_log` |
| VIF log file unparseable | `analyze_chunk_quality` catches exception, logs `warning`, continues without VIF |
| VIF log file empty (no parseable lines) | `parse_vif_file` raises `ValueError`; caught by `analyze_chunk_quality` |
| `QualityTarget.parse` with `"vif"` metric | Accepted; `ValueError` raised only for metrics not in `valid_metrics` |
| Stale `.tmp` files from interrupted run | Deleted by startup glob in `run_measure` before any work begins; in normal runs files are deleted immediately after parsing |
| `metrics_output_dir` not provided | Falls back to `output_dir` (existing behaviour) |

All error paths follow the existing pattern: log at `warning` level, continue without the affected metric. The pipeline never raises on a single metric failure.

## Testing Strategy

### Unit tests (specific examples and edge cases)

- `test_parse_vif_line_valid` — parse a known-good line, verify `n` and `vif` fields
- `test_parse_vif_line_malformed` — returns `None` for lines missing `vif:` field
- `test_parse_vif_file_empty` — raises `ValueError`
- `test_parse_vif_file_factor` — `factor=3` produces `frameNum` values 0, 3, 6, …
- `test_analyze_chunk_quality_vif_none` — `vif_log=None` produces no VIF in result
- `test_analyze_chunk_quality_vif_unparseable` — bad file logs warning, result has no VIF
- `test_quality_target_parse_vif` — `"vif-min:85.0"` parses correctly
- `test_quality_target_parse_invalid_metric` — `"xyz-min:50"` raises `ValueError`
- `test_quality_artifacts_has_vif_log` — `QualityArtifacts` accepts `vif_log` field
- `test_quality_artifacts_no_stats_files` — `QualityArtifacts` has no `stats_files` field
- `test_run_measure_no_metrics_subdir` — `run_measure` does not create `<stem>.metrics/` subdirs
- `test_vif_style_in_default_metric_styles` — `DEFAULT_METRIC_STYLES` contains `MetricType.VIF`

### Property-based tests (Hypothesis)

Each property test runs a minimum of 100 iterations. Tag format: `# Feature: vif-metric-support, Property N: <text>`

**Property 1 — Normalization formula correctness**
Generate random `_offset`, `_scale_factor`, `_clip_lower`, `_clip_upper`, and `raw` values. Verify `normalize(raw) == clip(_offset + raw * _scale_factor)`.
```python
# Feature: vif-metric-support, Property 1: normalize formula
@given(offset=floats(...), scale=floats(...), raw=floats(...), ...)
def test_normalize_formula(offset, scale, raw, clip_lower, clip_upper): ...
```

**Property 2 — VIF lossless normalization**
Fixed input `raw=0.0`; assert result is `100.0`. (Example, but expressed as a property for clarity.)
```python
# Feature: vif-metric-support, Property 2: VIF lossless
def test_vif_normalize_lossless(): assert MetricType.VIF.info.normalize(0.0) == 100.0
```

**Property 3 — VIF clip lower**
Generate `raw > 100.0`; assert `MetricType.VIF.info.normalize(raw) == 0.0`.
```python
# Feature: vif-metric-support, Property 3: VIF clip lower
@given(raw=floats(min_value=100.001, max_value=1e6))
def test_vif_normalize_clip_lower(raw): assert MetricType.VIF.info.normalize(raw) == 0.0
```

**Property 4 — Normalize idempotence**
Generate a raw value, normalize it, normalize the result again; assert both results are equal.
```python
# Feature: vif-metric-support, Property 4: normalize idempotence
@given(raw=floats(min_value=0.0, max_value=100.0))
def test_vif_normalize_idempotent(raw):
    once = MetricType.VIF.info.normalize(raw)
    twice = MetricType.VIF.info.normalize(once)
    assert once == twice
```

**Property 5 — `passes()` direction**
Generate `actual` and `target` floats; assert `passes(actual, target) == (actual >= target)` for `higher_is_better=True` metrics.
```python
# Feature: vif-metric-support, Property 5: passes direction
@given(actual=floats(...), target=floats(...))
def test_passes_direction(actual, target): ...
```

**Property 6 — `parse_vif_file` structure**
Generate synthetic VIF log content with random frame counts and values; assert returned DataFrame has correct index name and column.
```python
# Feature: vif-metric-support, Property 6: parse_vif_file structure
@given(frames=lists(floats(min_value=0.0, max_value=1.0), min_size=1))
def test_parse_vif_file_structure(frames, tmp_path): ...
```

**Property 7 — `parse_vif_file` zero-based indexing**
Generate synthetic VIF log with N frames and `factor=F`; assert `frameNum[i] == i * F`.
```python
# Feature: vif-metric-support, Property 7: parse_vif_file indexing
@given(frames=lists(...), factor=integers(min_value=1, max_value=10))
def test_parse_vif_file_indexing(frames, factor, tmp_path): ...
```

**Property 8 — `parse_vif_file` finite values**
Generate synthetic VIF log; assert all values in `"vif"` column are finite.
```python
# Feature: vif-metric-support, Property 8: parse_vif_file finite values
@given(frames=lists(floats(min_value=0.0, max_value=1.0, allow_nan=False), min_size=1))
def test_parse_vif_file_finite(frames, tmp_path): ...
```

**Property 9 — `parse_vif_file` skips malformed lines**
Generate a mix of valid and malformed lines; assert result row count equals valid line count.
```python
# Feature: vif-metric-support, Property 9: parse_vif_file skips malformed
@given(valid=lists(...), malformed=lists(text(), min_size=1))
def test_parse_vif_file_skips_malformed(valid, malformed, tmp_path): ...
```

**Property 10 — `analyze_chunk_quality` VIF normalization**
Generate synthetic VIF log; call `analyze_chunk_quality(vif_log=path, generate_plot=False)`; assert all stat values equal `MetricType.VIF.info.normalize(raw_stat)`.
```python
# Feature: vif-metric-support, Property 10: analyze_chunk_quality VIF normalization
@given(frames=lists(floats(min_value=0.0, max_value=1.0), min_size=1))
def test_analyze_chunk_quality_vif_normalized(frames, tmp_path): ...
```

**Property 11 — `analyze_chunk_quality` backward compat**
Generate any combination of PSNR/SSIM/VMAF logs; assert result without `vif_log` equals result with `vif_log=None`.
```python
# Feature: vif-metric-support, Property 11: analyze_chunk_quality backward compat
@given(...)
def test_analyze_chunk_quality_no_vif_unchanged(...): ...
```

**Property 13 — Target evaluation direction**
Generate `actual` and `target` floats; assert VIF target is met iff `actual >= target`.
```python
# Feature: vif-metric-support, Property 13: target evaluation direction
@given(actual=floats(0.0, 100.0), target=floats(0.0, 100.0))
def test_vif_target_evaluation_direction(actual, target): ...
```

**Property 14 — Sidecar YAML VIF keys**
Generate a `ChunkQualityStats` containing VIF; run the flattening loop; assert all 8 `vif_*` keys are present with correct values.
```python
# Feature: vif-metric-support, Property 14: sidecar YAML VIF keys
@given(stats=fixed_dictionaries({...}))
def test_sidecar_vif_keys(stats): ...
```

**Property 15 — `_generate_metrics` returns `.tmp` paths**
Mock `run_metric` to create empty files; call `_generate_metrics`; assert all returned paths end with `.tmp`.
```python
# Feature: vif-metric-support, Property 15: _generate_metrics returns .tmp paths
def test_generate_metrics_returns_tmp_paths(tmp_path): ...
```

The property-based tests use [Hypothesis](https://hypothesis.readthedocs.io/) which is already present in the project's test dependencies (`.hypothesis/` directory exists in the repo).
