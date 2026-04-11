# Implementation Plan: All-in-One Metrics Pass
<!-- markdownlint-disable MD024 -->

## Tasks

- [x] 1. Add `subsample_via_filter` to `MetricInfo` and update `_METRIC_INFO`
  - _Requirements: 4.1, 4.2_

- [x] 2. Implement `run_metrics` in `pyqenc/quality.py`
  - _Requirements: 1.1–1.5, 2.1–2.8, 3.1–3.2, 4.3_

- [x] 3. Update `_generate_metrics` and progress bar total
  - Progress bar total = `duration_seconds` (linear, single run); weight = `1.0`
  - _Requirements: 5.1–5.3, 6.1–6.2_

- [x] 4. Remove `complexity` field from `MetricInfo`
  - Remove `complexity` field from `MetricInfo` dataclass, all `_METRIC_INFO` entries, docstring, and test fixture
  - _Requirements: 6.2_

- [ ] 5. Add `parse_metrics` to `pyqenc/utils/visualization.py`
  - New public function: `parse_metrics(artifacts: QualityArtifacts, factor: int = 1) -> pd.DataFrame`
  - Dispatch to `parse_psnr_file`, `parse_ssim_file`, `parse_vmaf_file`, `parse_vif_file` for each non-`None` existing path in `artifacts`
  - Join via `pd.concat(..., axis=1)` on shared `frameNum` index
  - Catch individual parse failures: log warning, omit that metric, continue
  - Raise `ValueError` if no metrics could be parsed
  - _Requirements: 7.1–7.6_

- [ ] 6. Add `normalize_metrics` to `pyqenc/utils/visualization.py`
  - New public function: `normalize_metrics(df_raw: pd.DataFrame) -> pd.DataFrame`
  - For each column matching a `MetricType.value`, apply `MetricType(col).info.normalize(series)`
  - Unknown columns pass through unchanged
  - _Requirements: 8.1–8.4_

- [ ] 7. Add `compute_metric_stats` and make `extract_key_stats` public
  - Rename `_extract_key_stats` → `extract_key_stats` (public)
  - New public function: `compute_metric_stats(df_norm: pd.DataFrame) -> ChunkQualityStats`
  - For each column matching a `MetricType.value`: `compute_statistics(series)` → `extract_key_stats(...)` → store in result
  - _Requirements: 9.1–9.2_

- [ ] 8. Refactor `create_unified_plot` to accept `pd.DataFrame`
  - Change signature: replace `metrics: dict[MetricType, MetricData]` with `df_norm: pd.DataFrame`
  - Derive per-metric Series from `df_norm[metric.value]` for each column matching a `MetricType.value`
  - Remove all `MetricData` references inside the function
  - Preserve all existing plot behaviour (dual axes, summary boxes, bar subplots, aggregation)
  - _Requirements: 10.1–10.4_

- [ ] 9. Refactor `analyze_chunk_quality` and `_finish_evaluation` to use new pipeline
  - `_finish_evaluation`: replace manual parse+stats block with `parse_metrics` → `normalize_metrics` → `compute_metric_stats` → `create_unified_plot`
  - `analyze_chunk_quality`: refactor internals to use same pipeline; public signature unchanged
  - Remove `MetricData` dataclass from `pyqenc/quality.py` (no longer needed)
  - _Requirements: 9.3, 10.5, 11.1_

- [ ] 10. Checkpoint — run tests and fix failures
  - `uv run python -m pytest tests/`

- [ ] 11. Cross-spec review and completion
  - Review against `vif-metric-support` spec
  - Add summary notes to top of both specs
  - Update `- Completed:` date
