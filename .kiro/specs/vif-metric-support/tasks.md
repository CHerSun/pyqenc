# Implementation Plan: VIF Metric Support
<!-- markdownlint-disable MD024 -->

## Overview

Implement VIF as a fourth quality metric alongside PSNR, SSIM, and VMAF. The work is ordered so each step builds on the previous: data model changes first, then the ffmpeg command branch, then the parser, then integration into `analyze_chunk_quality`, then the plot, then the evaluator pipeline, then `run_measure` cleanup, then the CLI help constant. Property-based tests are placed immediately after the code they validate.

Requirements 1 (ACs 1–6) and 2 (ACs 1–3, 7–10) are already implemented — tasks below start from the remaining ACs.

## Tasks

- [x] 1. Update `MetricInfo` docstring and `QualityArtifacts` dataclass
  - [x] 1.1 Update `MetricInfo` docstring in `pyqenc/quality.py` to replace `scale_factor` → `_scale_factor`, `clip_lower` → `_clip_lower`, `clip_upper` → `_clip_upper`, and document `_offset`
    - No code changes — fields and `normalize()` are already correct
    - _Requirements: 1.8_
  - [x] 1.2 Update `QualityArtifacts` in `pyqenc/quality.py`: add `vif_log: Path | None = None` field; remove `stats_files: list[Path]` field
    - _Requirements: 6.4, 12.6_

- [x] 2. Add VIF branch to `run_metric` and update `QualityTarget.parse`
  - [x] 2.1 Add `elif metric == MetricType.VIF:` branch in `run_metric` (`pyqenc/quality.py`) before the `assert_never(metric)` line
    - Use `vif=stats_file={output_prefix}{metric.value}{vif_ext}` filter
    - Default extension `.log`; respect `output_extension` override (same pattern as PSNR/SSIM)
    - Frame subsampling already covered by the existing `if metric != MetricType.VMAF and subsample > 1:` guard
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_
  - [x] 2.2 Replace hardcoded `valid_metrics = {"vmaf", "ssim", "psnr"}` in `QualityTarget.parse` (`pyqenc/models.py`) with `{m.value for m in MetricType}`
    - _Requirements: 7.1, 7.2, 7.3_
  - [x] 2.3 Write property test for `QualityTarget.parse` VIF acceptance
    - **Property 12: `QualityTarget.parse` accepts VIF**
    - **Validates: Requirements 7.1, 7.3**
    - Place in `tests/test_vif_properties.py`

- [x] 3. Implement VIF log parser in `pyqenc/utils/visualization.py`
  - [x] 3.1 Add `_parse_vif_line(line: str) -> dict[str, float] | None` helper
    - Extract `n` (int) and `vif:` (float) fields; ignore per-scale fields; return `None` on malformed lines
    - Use `MetricType.VIF.value` for the key — no bare `"vif"` string
    - _Requirements: 4.5, 4.6, 10.1_
  - [x] 3.2 Add `parse_vif_file(file_path: Path, factor: int = 1) -> pd.DataFrame`
    - VIF logs are zero-based (`n:0` for first frame): `frameNum = n * factor` (no `- 1` adjustment, unlike PSNR/SSIM)
    - Index name: `_KEY_FRAME_NUM`; single column: `MetricType.VIF.value`
    - Raise `ValueError` when no parseable lines found
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.7_
  - [x] 3.3 Write property test: `parse_vif_file` DataFrame structure
    - **Property 6: `parse_vif_file` DataFrame structure**
    - **Validates: Requirements 4.1, 4.2**
  - [x] 3.4 Write property test: `parse_vif_file` zero-based frame indexing
    - **Property 7: `parse_vif_file` zero-based frame indexing**
    - **Validates: Requirements 4.3**
  - [x] 3.5 Write property test: `parse_vif_file` finite values
    - **Property 8: `parse_vif_file` finite values**
    - **Validates: Requirements 4.6**
  - [x] 3.6 Write property test: `parse_vif_file` skips malformed lines
    - **Property 9: `parse_vif_file` skips malformed lines**
    - **Validates: Requirements 4.5**

- [x] 4. Checkpoint — ensure all tests pass
  - Run `uv run python -m pytest tests/test_vif_properties.py` and fix any failures before continuing.

- [x] 5. Integrate VIF into `analyze_chunk_quality`
  - [x] 5.1 Add `vif_log: Path | None = None` and `delete_after_parse: bool = True` parameters to `analyze_chunk_quality` in `pyqenc/utils/visualization.py`
    - Add VIF parsing block mirroring the PSNR/SSIM pattern: parse → compute stats → extract key stats → store in `result`
    - After each successful parse, when `delete_after_parse=True`, call `metric_file.unlink(missing_ok=True)` (best-effort; log warning on failure)
    - Apply `delete_after_parse` to all metric files (PSNR, SSIM, VMAF, VIF), not just VIF
    - Remove `metric_files: dict[MetricType, Path]` tracking dict and all assignments to it
    - Remove the `_save_stats_file(...)` call loop
    - Remove the `_cleanup_raw_metric_files(metric_files)` call
    - Remove the `KEEP_RAW_METRICS_FILES` import
    - Update `_auto_output_path` to accept `QualityArtifacts` and pick the first non-`None` log path from it
    - Update docstring to remove `.stats` side-effect references
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 12.3, 12.4, 12.5, 12.11_
  - [x] 5.2 Write property test: `analyze_chunk_quality` VIF normalization
    - **Property 10: `analyze_chunk_quality` VIF integration and normalization**
    - **Validates: Requirements 5.2, 5.3**
  - [x] 5.3 Write property test: `analyze_chunk_quality` backward compatibility
    - **Property 11: `analyze_chunk_quality` backward compatibility**
    - **Validates: Requirements 5.5**

- [x] 6. Add VIF to `DEFAULT_METRIC_STYLES` and update `create_unified_plot`
  - [x] 6.1 Add `_VIF_COLOR: str = "#7B2D8B"` and `_VIF_FILL_COLOR: str = "#C084D4"` constants in the constants section of `pyqenc/utils/visualization.py`
    - Fix the existing PSNR `lossless_threshold` in `DEFAULT_METRIC_STYLES`: change `MetricType.PSNR.info._clip_upper` to `MetricType.PSNR.info.lossless_value`
    - Add `MetricType.VIF` entry to `DEFAULT_METRIC_STYLES` using `_VIF_COLOR`, `MetricType.VIF.info.display_unit`, `y_axis="right"`, `lossless_threshold=MetricType.VIF.info.lossless_value`, `lossless_label=MetricType.VIF.info.lossless_raw_repr`
    - _Requirements: 8.2, 10.2_
  - [x] 6.2 Update `create_unified_plot` in `pyqenc/utils/visualization.py`
    - Update `has_percentage_metrics` to also check `MetricType.VIF in metrics`
    - Update `_configure_pct_axis` label from `"SSIM / VMAF (%)"` to `"SSIM / VMAF / VIF"` (no `%` since VIF `display_unit = ""`)
    - Replace the hardcoded `for metric_type in [MetricType.PSNR, MetricType.SSIM, MetricType.VMAF]:` summary box loop with `for metric_type in metrics:`
    - Replace the hardcoded `for metric_type in [MetricType.PSNR, MetricType.SSIM, MetricType.VMAF]:` bar subplot loop with `for metric_type in metrics:`
    - _Requirements: 8.1, 8.3, 8.4, 8.5, 8.6_

- [x] 7. Update `QualityEvaluator` — `_generate_metrics`, `_finish_evaluation`, `evaluate_chunk_async` / `evaluate_chunk`
  - [x] 7.1 Update `QualityEvaluator._generate_metrics` in `pyqenc/utils/visualization.py`
    - Add VIF to the `asyncio.gather` call alongside PSNR, SSIM, VMAF
    - Remove the rename loop entirely — return `QualityArtifacts` with `.tmp` paths directly
    - Return `QualityArtifacts(psnr_log=..., ssim_log=..., vmaf_json=..., vif_log=..., plot=None)`
    - Log a warning for any path that does not exist after gather completes
    - Change return type annotation from `tuple[Path, Path, Path]` to `QualityArtifacts`
    - _Requirements: 6.1, 6.2, 6.3, 12.1, 12.2_
  - [x] 7.2 Update `QualityEvaluator._finish_evaluation` in `pyqenc/utils/visualization.py`
    - Accept `artifacts: QualityArtifacts` instead of individual path parameters
    - Pass each log path from `artifacts` to `analyze_chunk_quality`, checking `.exists()` for each
    - Set `artifacts.plot` to the resolved plot path
    - Fix target evaluation: replace `if actual_value < target.value:` with `if not MetricType(target.metric).info.passes(actual_value, target.value):`
    - _Requirements: 6.5, 6.6, 7.4_
  - [x] 7.3 Update `QualityEvaluator.evaluate_chunk_async` and `evaluate_chunk` in `pyqenc/utils/visualization.py`
    - Add `metrics_output_dir: Path | None = None` parameter to both methods
    - When provided, pass `metrics_output_dir` as `cwd` to `_generate_metrics`; when `None`, use `output_dir`
    - Receive `QualityArtifacts` from `_generate_metrics` and pass it directly to `_finish_evaluation`
    - Include VIF `complexity` in the progress bar total (already covered if `_METRIC_INFO[MetricType.VIF]` is populated)
    - _Requirements: 6.7, 12.7, 12.8_
  - [x] 7.4 Write property test: `_generate_metrics` returns `.tmp` paths
    - **Property 15: `_generate_metrics` returns `.tmp` paths directly**
    - **Validates: Requirements 6.2, 12.1, 12.2**

- [x] 8. Update `run_measure` in `pyqenc/phases/measure.py`
  - [x] 8.1 Remove `metrics_dirs` dict and all references; remove `METRICS_SUBDIR_SUFFIX` import from inside `run_measure`
    - Pass `measure_dir` directly to `_run_metrics` as the metrics output directory (replacing `metrics_dirs[target_video]`)
    - Add startup `.tmp` glob cleanup before any work begins: `for tmp_file in measure_dir.glob("*.tmp"): tmp_file.unlink()` (best-effort; log warning on failure)
    - _Requirements: 12.7, 12.9_

- [x] 9. Update `pyqenc/cli.py` — `_QUALITY_TARGET_HELP` constant
  - [x] 9.1 Define `_QUALITY_TARGET_HELP: str` constant before `_add_quality_arguments`
    - Content: brief note on 0–100 normalized scale, per-metric landmarks (VMAF 95+, SSIM 98+, PSNR 40–60 typical, VIF 95+), target format examples
    - Replace the inline `help=` string in `_add_quality_arguments` with `help=_QUALITY_TARGET_HELP`
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

- [x] 10. Checkpoint — ensure all tests pass
  - Run `uv run python -m pytest` and fix any failures before continuing.

- [x] 11. Write remaining property-based tests in `tests/test_vif_properties.py`
  - [x] 11.1 Write property test: normalization formula correctness
    - **Property 1: Normalization formula correctness**
    - **Validates: Requirements 1.3, 1.7**
    - Generate random `_offset`, `_scale_factor`, `_clip_lower`, `_clip_upper`, and `raw`; verify `normalize(raw) == clip(_offset + raw * _scale_factor)`
  - [x] 11.2 Write property test: VIF lossless normalization
    - **Property 2: VIF lossless normalization**
    - **Validates: Requirements 2.4**
    - Assert `MetricType.VIF.info.normalize(0.0) == 100.0`
  - [x] 11.3 Write property test: VIF clip lower
    - **Property 3: VIF clip lower**
    - **Validates: Requirements 2.5**
    - Generate `raw > 100.0`; assert `MetricType.VIF.info.normalize(raw) == 0.0`
  - [x] 11.4 Write property test: normalize idempotence
    - **Property 4: Normalize idempotence on already-normalized values**
    - **Validates: Requirements 2.6**
    - Generate `raw` in `[0.0, 100.0]`; normalize once, normalize again; assert equal
  - [x] 11.5 Write property test: `passes()` direction correctness
    - **Property 5: `passes()` direction correctness**
    - **Validates: Requirements 1.7, 2.7, 2.8**
    - For `higher_is_better=True` metrics: assert `passes(actual, target) == (actual >= target)`
  - [x] 11.6 Write property test: target evaluation direction-awareness
    - **Property 13: Target evaluation direction-awareness**
    - **Validates: Requirements 7.4**
    - Generate `actual` and `target` floats; assert VIF target met iff `actual >= target`
  - [x] 11.7 Write property test: sidecar YAML VIF key generation
    - **Property 14: Sidecar YAML VIF key generation**
    - **Validates: Requirements 9.1**
    - Generate a `ChunkQualityStats` containing `MetricType.VIF`; run the flattening loop; assert all 8 `vif_*` keys present with correct values

- [x] 12. Final checkpoint — ensure all tests pass
  - Run `uv run python -m pytest` and fix any failures.

- [x] 13. Cross-spec review
  - Review this spec against other specs in `.kiro/specs/` (particularly `unified-metrics-visualization`, `standalone-measure`, `pipeline-correctness-refactor`) to identify anything superseded or changed between specs
  - Add a brief summary comment at the top of both this spec's `requirements.md` and `design.md` noting any differences
  - Update `- Completed:` date in `requirements.md` and `design.md`

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- All property tests go in `tests/test_vif_properties.py` (new file, consistent with `test_measure_properties.py` / `test_metrics_properties.py` naming)
- No bare `"vif"` string literals — always use `MetricType.VIF.value`
- `_save_stats_file` and `_cleanup_raw_metric_files` are deleted entirely (not just unused)
- The `evaluate_chunk` (sync) path needs the same `metrics_output_dir` fix as `evaluate_chunk_async`
- Run `uv run python -m pytest` to verify tests pass after implementation
