# Implementation Plan

- [x] 1. Create project structure and extract reusable code from psnr.py

  - Create `metrics_visualization/` package directory with `__init__.py`
  - Extract `compute_statistics()` function from `psnr.py` to `statistics.py`
  - Ensure statistics module is metric-agnostic and works with any pandas Series
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 7.2_

- [x] 2. Implement metric parsers module

- [x] 2.1 Create parsers.py with MetricType enum and base structure

  - Define `MetricType` enum with PSNR, SSIM, VMAF values
  - Create module structure with imports for pandas, pathlib, json
  - _Requirements: 1.1, 2.1_

- [x] 2.2 Implement PSNR parser with frame alignment fix

  - Extract and refactor `parse_psnr_line()` and `parse_psnr_file()` from `psnr.py`
  - Fix frame number calculation: `frameNum = (n - 1) * factor` (PSNR starts from n=1)
  - Return DataFrame indexed by `frameNum` with `psnr_avg` column
  - Handle invalid files with ValueError
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2.3 Implement SSIM parser

  - Parse SSIM log format: `n:X Y:Y U:Y V:Y All:Y (dB)`
  - Extract frame number and All value (overall SSIM)
  - Apply frame alignment: `frameNum = (n - 1) * factor` (SSIM starts from n=1)
  - Return DataFrame indexed by `frameNum` with `ssim_avg` column
  - Handle invalid files with ValueError
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2.4 Implement VMAF parser

  - Parse VMAF JSON format with `frames` array
  - Extract `frameNum` and `metrics.vmaf` from each frame object
  - No frame adjustment needed (VMAF reports actual frame numbers starting from 0)
  - Return DataFrame indexed by `frameNum` with `vmaf` column (use MetricType.VMAF.value)
  - Handle invalid JSON and missing fields with ValueError
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [x] 2.5 Create generic parse_metric_file dispatcher function

  - Accept file path
  - Try to parse file with known MetricTypes parser functions
  - Return standardized DataFrame
  - _Requirements: 7.1_

- [x] 3. Implement unified visualization module

- [x] 3.1 Create visualization.py with MetricData TypedDict and function signature

  - Define `MetricData` TypedDict with df, column, label, color, unit, y_axis fields
  - Create `create_unified_plot()` function signature accepting metrics dict
  - Set up matplotlib style and figure with grid layout (height_ratios=[3, 1])
  - _Requirements: 4.1, 4.2, 4.3, 7.3_

- [x] 3.2 Implement adaptive dual Y-axis configuration

  - Detect which metrics are available from input dict
  - Configure left Y-axis for PSNR (0-103 dB) if present
  - Configure right Y-axis for SSIM/VMAF (0-100%) if present
  - Handle single metric cases (no dual axis needed)

  - _Requirements: 4.1, 4.2_

- [x] 3.3 Plot metric lines on main axes

  - Plot PSNR as solid blue line on left axis
  - Plot SSIM as dashed green line on right axis
  - Plot VMAF as dotted orange line on right axis

  - Mark infinite PSNR values with red star markers at 100 dB
  - Add legend in lower right
  - _Requirements: 4.3, 4.4, 4.5_

- [x] 3.4 Create horizontal summary boxes at bottom of main plot

  - Position frame info box at bottom-left
  - Position metric summary boxes horizontally (PSNR, SSIM, VMAF order)
  - Include lossless frame count for each metric (inf, 1.0, 100.0 respectively)
  - Use monospace font and color-coded backgrounds
  - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 3.5 Create statistics bar subplots with consistent layout

  - Create N subplots (1 row, N columns) where N = number of metrics
  - Use same bar positions (Min, 5%, 10%, 25%, 50%, 75%, 90%, 95%, Max) for all
  - Omit bars with NaN/inf values (leave gaps)
  - Add value labels to the right of each bar
  - Match X-axis range to corresponding main plot Y-axis
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 3.6 Finalize plot and save to file

  - Apply tight layout
  - Save plot to output_path with 100 DPI
  - Return computed statistics dictionary for all metrics
  - _Requirements: 4.1, 4.2, 7.3_

- [x] 4. Implement API module for pipeline integration

- [x] 4.1 Create api.py with ChunkQualityStats TypedDict

  - Define `ChunkQualityStats` with optional psnr, ssim, vmaf dict fields
  - Each metric dict contains: min, median, max, std
  - _Requirements: 9.2_

- [x] 4.2 Implement analyze_chunk_quality function

  - Accept optional paths for psnr_log, ssim_log, vmaf_json
  - Accept factor, output_path, title, generate_plot parameters
  - Parse each provided metric file using parsers module
  - Compute statistics for each metric using statistics module
  - Extract key stats (min, median, max, std) for return value
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 4.3 Generate visualization and save stats files

  - Call create_unified_plot with parsed DataFrames if generate_plot=True
  - Auto-generate output_path in same directory as metric files if not provided
  - Save individual stats text files for each metric in same directory as original metric logs file with .stats extension
  - Return ChunkQualityStats dictionary
  - _Requirements: 9.6, 9.7_

- [x] 4.4 Add error handling for missing or invalid metrics

  - Wrap each parser call in try-except
  - Print warnings for failed parsers but continue with available metrics
  - Raise ValueError if no valid metrics could be parsed
  - _Requirements: 9.3_

- [x] 5. Implement CLI module with file type detection

- [x] 5.1 Create cli.py with detect_metric_type function

  - Use already implemented parse_metric_file
  - Return metric type string or None if unknown
  - _Requirements: 8.1, 8.2_

- [x] 5.2 Create generate_output_paths helper function

  - Extract common directory from metric files
  - Extract common prefix from filenames (e.g., "chunk_001")
  - Generate plot path: `{common_prefix}.png`
  - Generate stats paths: `{each_metric_prefix}.stats` for each metric
  - _Requirements: 8.4, 8.5_

- [x] 5.3 Implement main CLI function with argument parsing

  - Accept 1-3 positional file arguments
  - Require --factor argument
  - Accept optional --title argument
  - Validate file count (max == len(MetricType))
  - Detect metric type for each file
  - Check for duplicate metric types
  - _Requirements: 8.1, 8.2, 8.3_

- [x] 5.4 Call API and display results
  - Generate output paths using helper function
  - Call analyze_chunk_quality with detected metric files
  - Print output file locations
  - Print summary statistics for each metric
  - _Requirements: 8.4, 8.5_
