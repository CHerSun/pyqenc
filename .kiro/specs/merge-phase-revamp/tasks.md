# Implementation Plan

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-19
- Completed: 2026-03-19

- [x] 1. Rename `StrategyTestResult.avg_file_size` to `total_file_size` and update all usages





  - Rename the field in `pyqenc/phases/optimization.py` and update every assignment that sets it
  - Update `fmt_optimization_summary` in `pyqenc/utils/log_format.py` to use `total_file_size`, label the column `"Total size (MB)"`, and format values with space-thousands separator (e.g. `4 231.4`)
  - Update the orchestrator's `_execute_optimization` log that references `avg_file_size`
  - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 2. Fix metrics extraction temp-file protocol in `QualityEvaluator._generate_metrics`





  - Change ffmpeg output filenames to `<uuid>.<metric>.tmp` (e.g. `<uuid>.psnr.tmp`) so they carry the `.tmp` extension while ffmpeg is running
  - After all ffmpeg processes complete, perform a single rename per file: `<uuid>.<metric>.tmp` → final path
  - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 3. Apply metrics normalization immediately after parsing in `analyze_chunk_quality`




  - Import and apply `normalize_metric` to every stat value in `ChunkQualityStats` after `_extract_key_stats` returns, before the function returns
  - Remove the `* 100` SSIM scaling from `create_unified_plot` (values are now pre-normalized)
  - Remove the `* 100` SSIM scaling from `_save_stats_file` (values are now pre-normalized)
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6_

- [x] 4. Add dual-label (timestamp + frame number) x-axis to `create_unified_plot`





  - Add `fps: float | None = None` parameter to `create_unified_plot` and `analyze_chunk_quality`
  - When `fps` is provided, configure the x-axis tick formatter to produce two-line labels: `HH:MM:SS` on top, adjusted frame number below
  - Pass `fps` from `QualityEvaluator.evaluate_chunk` (already probes `VideoMetadata`) through `analyze_chunk_quality` to `create_unified_plot`
  - _Requirements: 5.4_

- [x] 5. Update `merge_final_video` — mode banner, chunk count scoping, and output filename





  - Add required `source_stem: str` parameter to `merge_final_video`
  - Emit the mode banner as the very first `logger.info` call: `"Mode: optimal strategy — {display_name}"` or `"Mode: all strategies — {N} strategy(ies): {list}"`
  - Remove the legacy `"Merging optimal strategy: ..."` and `"Merging N strategy(ies): ..."` messages
  - After strategy resolution, log chunk count scoped to the resolved strategies only
  - Change output filename to `f"{source_stem} {safe}.mkv"`
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 3.1, 3.2_

- [x] 6. Fix metrics artifact placement in `_measure_quality` and `merge_final_video`





  - Update `_measure_quality` to accept `final_result: Path` and derive: intermediate dir as `output_dir / final_result.stem`, plot path as `output_dir / f"{final_result.stem}.png"`
  - Pass the explicit `plot_path` to `evaluate_chunk`
  - Update the call site in `merge_final_video` to pass `final_result=output_file`
  - _Requirements: 4.1, 4.2, 4.3_

- [x] 7. Remove misleading chunk count log from orchestrator and pass `source_stem` to merge




  - Remove the `"Found N chunks across M strategies"` log from `_execute_merge` in `pyqenc/orchestrator.py`
  - Pass `source_stem=job.source.path.stem` to `merge_final_video`; abort with `critical` log if `job` is `None`
  - _Requirements: 2.3, 3.1_

- [x] 8. Add merge summary formatting functions to `log_format.py`





  - Implement `fmt_merge_summary_optimal(output_file, size_bytes, reference_size_bytes, quality_targets, metrics, targets_met)` — key-value block format
  - Implement `fmt_merge_summary_all(output_files, sizes_bytes, reference_size_bytes, quality_targets, final_metrics, targets_met)` — table format with right-aligned space-thousands MB values
  - Both functions return `list[str]`; size values formatted with space-thousands separator
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

- [x] 9. Emit post-merge summary from orchestrator after merge phase completes





  - After `merge_final_video` returns with `COMPLETED` or `REUSED`, determine reference size (extracted video stream size from `work_dir/extracted/`, fallback to source size)
  - Determine mode (`optimal` if `self._optimal_strategy` is set, else `all`)
  - Call the appropriate `fmt_merge_summary_*` function and emit each line at `info` level
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

- [x] 10. Add `metrics_sampling` to config file and CLI





  - Add `metrics.sampling: 10` to `pyqenc/default_config.yaml`
  - Add `get_metrics_sampling() -> int` to `ConfigManager` in `pyqenc/config.py`, reading from `config["metrics"]["sampling"]` with fallback `10`
  - Rename `PipelineConfig.subsample_factor` to `metrics_sampling` in `pyqenc/models.py` and update all usages across the codebase
  - Add `--metrics-sampling` argument to `_add_quality_arguments` in `pyqenc/cli.py` (type `int`, default `None`, min 1, help text noting default 10 and recommended max 30)
  - In `_cmd_auto` (and other handlers building `PipelineConfig`), resolve the value: CLI arg if provided, else `config_manager.get_metrics_sampling()`
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
