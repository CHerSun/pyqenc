# Design Document: Unified Metrics Visualization

<!-- markdownlint-disable MD024 -->

## Overview

This design refactors the existing `psnr.py` module into a unified metrics visualization system that supports PSNR, SSIM, and VMAF. The architecture emphasizes code reuse through abstraction of parsing, statistics computation, and visualization logic. The system provides both a command-line interface and a programmatic API for integration into the encoding pipeline.

## Architecture

### High-Level Structure

```log
metrics_visualization/
├── parsers.py          # Metric-specific parsing logic
├── statistics.py       # Generic statistics computation
├── visualization.py    # Unified plotting with dual Y-axes
├── api.py             # Public API for pipeline integration
└── cli.py             # Command-line interface
```

### Design Principles

1. **Separation of Concerns**: Parsing, statistics, and visualization are independent modules
2. **Metric Agnosticism**: Statistics and visualization modules work with any metric type
3. **Extensibility**: New metrics can be added by implementing a parser function
4. **Backward Compatibility**: Existing `psnr.py` functionality is preserved

## Components and Interfaces

### 1. Parsers Module (`parsers.py`)

Provides metric-specific parsing functions that convert log files to standardized DataFrames.

#### Interface

```python
from enum import Enum
from pathlib import Path
import pandas as pd

class MetricType(Enum):
    PSNR = "psnr"
    SSIM = "ssim"
    VMAF = "vmaf"

def parse_psnr_file(file_path: Path, factor: int = 1) -> pd.DataFrame:
    """Parse PSNR log file. Returns DataFrame with 'psnr_avg' column."""
    ...

def parse_ssim_file(file_path: Path, factor: int = 1) -> pd.DataFrame:
    """Parse SSIM log file. Returns DataFrame with 'ssim_avg' column."""
    ...

def parse_vmaf_file(file_path: Path, factor: int = 1) -> pd.DataFrame:
    """Parse VMAF JSON file. Returns DataFrame with 'vmaf_score' column."""
    ...

def parse_metric_file(metric_type: MetricType, file_path: Path, factor: int = 1) -> pd.DataFrame:
    """Generic parser dispatcher."""
    ...
```

#### PSNR Parser (Existing Logic)

- Reads line-by-line text format
- Extracts `n:X mse_avg:Y psnr_avg:Z` patterns
- **Frame alignment**: PSNR starts counting from n=1 (not 0)
- Computes actual frame numbers: `frameNum = (n - 1) * factor`
- Returns DataFrame indexed by `frameNum` with `psnr_avg` column

#### SSIM Parser (New)

FFmpeg SSIM log format:

```log
n:1 Y:0.95 U:0.96 V:0.97 All:0.95 (15.23)
```

- Reads line-by-line text format
- Extracts `n:X` and `All:Y` values
- **Frame alignment**: SSIM starts counting from n=1 (not 0)
- Computes actual frame numbers: `frameNum = (n - 1) * factor`
- Returns DataFrame indexed by `frameNum` with `ssim_avg` column (All value)

#### VMAF Parser (New)

FFmpeg VMAF JSON format:

```json
{
  "frames": [
    {"frameNum": 0, "metrics": {"vmaf": 95.5}},
    {"frameNum": 1, "metrics": {"vmaf": 96.2}}
  ]
}
```

- Loads JSON file
- Extracts frame numbers and VMAF scores from `frames` array
- **Frame alignment**: VMAF uses internal subsampling and reports actual frame numbers (starts from 0)
- No adjustment needed: `frameNum = frameNum` (as reported in JSON)
- Returns DataFrame indexed by `frameNum` with `vmaf_score` column

**Note on Frame Alignment**: The `factor` parameter represents the frame sampling used during metric calculation. PSNR and SSIM use external frame selection (via FFmpeg's `select` filter), so they see fewer frames and start counting from 1. VMAF has internal subsampling and reports actual frame numbers starting from 0. The parsers handle this alignment difference to ensure all metrics align correctly on the same frame axis.

### 2. Statistics Module (`statistics.py`)

Generic statistics computation that works with any pandas Series.

#### Interface

```python
from typing import TypedDict
import pandas as pd

class MetricStatistics(TypedDict):
    min: float
    p5: float
    p10: float
    p25: float
    p50: float  # median
    p75: float
    p90: float
    p95: float
    max: float
    std: float

def compute_statistics(
    values: pd.Series,
    std_cutoff_max: float | None = None,
    std_cutoff_min: float | None = None
) -> MetricStatistics:
    """Compute quantile-based statistics with optional outlier filtering for std."""
    ...
```

#### Implementation Details

- Computes quantiles: [0.00, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
- Uses `.max()` instead of 1.0 quantile to handle infinite values
- Applies cutoffs only for standard deviation calculation
- Replaces `np.inf` with `np.nan` before computing std
- Returns typed dictionary for type safety

### 3. Visualization Module (`visualization.py`)

Creates unified plots with dual Y-axes and per-metric statistics bars.

#### Interface

```python
from pathlib import Path
from typing import Dict
import pandas as pd

class MetricData(TypedDict):
    df: pd.DataFrame
    column: str
    label: str
    color: str
    unit: str
    y_axis: str  # 'left' or 'right'

def create_unified_plot(
    metrics: Dict[MetricType, MetricData],
    factor: int,
    output_path: Path,
    title: str = "Video Quality Metrics Analysis"
) -> Dict[MetricType, MetricStatistics]:
    """Create unified visualization with dual Y-axes and return statistics."""
    ...
```

#### Layout Design

```log
┌─────────────────────────────────────────────────────────┐
│                    Title with sampling info              │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Main Plot Area (height_ratio=3)                        │
│  - Left Y-axis: PSNR (0-103 dB) [if PSNR available]    │
│  - Right Y-axis: SSIM/VMAF (0-100%) [if either avail]  │
│  - X-axis: Frame numbers                                │
│  - Legend in lower right                                │
│  - Summary text boxes at bottom (horizontal)            │
│                                                          │
├─────────────────────────────────────────────────────────┤
│  Statistics Area (height_ratio=1)                       │
│  - Horizontal bar subplots (1 row, N columns)           │
│  - One subplot per available metric (N = 1, 2, or 3)   │
│  - Each shows percentile bars with value labels         │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

**Adaptive Layout Rules**:

- If only PSNR: Use left Y-axis only
- If only SSIM or VMAF: Use right Y-axis only (scaled 0-100%)
- If PSNR + (SSIM and/or VMAF): Use dual Y-axes
- If only SSIM and VMAF: Use single Y-axis (0-100%), both on same scale
- Statistics subplots: Create N columns where N = number of available metrics
- Summary boxes: Only show boxes for available metrics (1-4 boxes total)

#### Dual Y-Axis Configuration

- **Left Y-axis (PSNR)**:
  - Range: 0-103 dB
  - Major ticks: every 5 dB
  - Minor ticks: every 1 dB
  - Color: blue

- **Right Y-axis (SSIM/VMAF)**:
  - Range: 0-100%
  - Major ticks: every 10%
  - Minor ticks: every 2%
  - Color: green (SSIM), orange (VMAF)

#### Line Styles

- PSNR: Solid blue line, infinite values marked with red stars
- SSIM: Dashed green line
- VMAF: Dotted orange line

#### Statistics Bars Layout

- Use `plt.subplots(1, N)` where N = number of metrics
- Each subplot shows horizontal bars for percentiles
- **All subplots use the same bar positions** (Min, 5%, 10%, 25%, 50%, 75%, 90%, 95%, Max) for comparability
- Bars with NaN or infinite values are omitted (not plotted), leaving gaps
- X-axis range matches main plot Y-axis range for that metric
- Bars use viridis colormap
- Value labels positioned to the right of bars (only for valid values)

#### Summary Text Boxes

- **All boxes positioned horizontally at bottom of main plot** (utilizing empty space below quality lines)
- **Layout order (left to right)**:
  1. Frame Info Box: Frames total, frames checked, sampling factor
  2. PSNR Summary Box (if available): Min, median, max, std, lossless count
  3. SSIM Summary Box (if available): Min, median, max, std, lossless count
  4. VMAF Summary Box (if available): Min, median, max, std, lossless count
- Each metric box has header with metric name
- Lossless count: frames with perfect quality (PSNR=inf, SSIM=1.0, VMAF=100.0)
- Monospace font for alignment
- Each box has distinct background color (frame info: wheat, metrics: match line colors)

### 4. API Module (`api.py`)

Public interface for programmatic integration into the encoding pipeline.

#### Interface

```python
from pathlib import Path
from typing import Dict, Optional

class ChunkQualityStats(TypedDict):
    psnr: Optional[Dict[str, float]]  # {min, median, max, std}
    ssim: Optional[Dict[str, float]]
    vmaf: Optional[Dict[str, float]]

def analyze_chunk_quality(
    psnr_log: Optional[Path] = None,
    ssim_log: Optional[Path] = None,
    vmaf_json: Optional[Path] = None,
    factor: int = 1,
    output_path: Optional[Path] = None,
    title: Optional[str] = None,
    generate_plot: bool = True
) -> ChunkQualityStats:
    """
    Analyze video chunk quality from metric log files.

    Args:
        psnr_log: Path to PSNR log file (optional)
        ssim_log: Path to SSIM log file (optional)
        vmaf_json: Path to VMAF JSON file (optional)
        factor: Frame sampling factor
        output_path: Where to save plot (auto-generated in same dir as metrics if None)
        title: Plot title (auto-generated if None)
        generate_plot: Whether to create visualization

    Returns:
        Dictionary with statistics for each available metric

    Side Effects:
        - Saves plot to output_path (if generate_plot=True)
        - Saves stats text files in same directory as metric files
    """
    ...
```

#### Implementation Flow

1. Parse each provided metric file
2. Compute statistics for each metric
3. Generate unified plot (if requested)
4. Save individual stats text files
5. Return structured statistics dictionary

#### Example Usage in Encoding Pipeline

```python
from api import analyze_chunk_quality

# After encoding a chunk and running metrics
stats = analyze_chunk_quality(
    psnr_log=Path("chunk_001_psnr.log"),
    ssim_log=Path("chunk_001_ssim.log"),
    vmaf_json=Path("chunk_001_vmaf.json"),
    factor=10
)

# Decision logic
if stats['vmaf']['median'] < 95.0:
    # Increase encoding quality
    ...
elif stats['psnr']['min'] < 30.0:
    # Check for problematic frames
    ...
```

### 5. CLI Module (`cli.py`)

Command-line interface for standalone usage. Analyzes a single video chunk at a time.

#### Interface

```sh
python -m metrics_visualization \
    chunk_001_psnr.log \
    chunk_001_ssim.log \
    chunk_001_vmaf.json \
    --factor 10 \
    --title "Custom Title"
```

#### Argument Specification

- Positional arguments: 1-3 metric files (auto-detect type by parsing)
- `--factor`: Sampling factor (required, no default) - used for frame alignment
- `--title`: Custom plot title (optional, auto-generated if omitted)

**Note**: Output files are automatically placed in the same directory as the metric files with adjusted naming:

- Plot: `chunk_001_metrics.png` (derived from common prefix)
- Stats: `chunk_001_psnr_stats.txt`, `chunk_001_ssim_stats.txt`, etc.

#### File Type Detection

Detection is done by attempting to parse the first few lines of each file:

1. **Try PSNR parser**: Look for `n:X mse_avg:Y psnr_avg:Z` pattern
2. **Try SSIM parser**: Look for `n:X Y:Y U:Y V:Y All:Y` pattern
3. **Try VMAF parser**: Attempt JSON parse and look for `frames` array with `metrics.vmaf`
4. **If all fail**: Raise error indicating unknown format

Each parser returns `None` or raises exception if format doesn't match, allowing the detection logic to try the next parser.

#### Implementation

```python
import argparse
from pathlib import Path
from typing import Optional, Tuple
from api import analyze_chunk_quality
from parsers import parse_psnr_file, parse_ssim_file, parse_vmaf_file

def detect_metric_type(file_path: Path) -> Optional[str]:
    """
    Detect metric type by attempting to parse file.
    Returns 'psnr', 'ssim', 'vmaf', or None if unknown.
    """
    # Try PSNR
    try:
        with file_path.open('r') as f:
            first_line = f.readline()
            if 'mse_avg:' in first_line and 'psnr_avg:' in first_line:
                return 'psnr'
    except Exception:
        pass

    # Try SSIM
    try:
        with file_path.open('r') as f:
            first_line = f.readline()
            if 'All:' in first_line and 'Y:' in first_line:
                return 'ssim'
    except Exception:
        pass

    # Try VMAF (JSON)
    try:
        import json
        with file_path.open('r') as f:
            data = json.load(f)
            if 'frames' in data and len(data['frames']) > 0:
                if 'metrics' in data['frames'][0] and 'vmaf' in data['frames'][0]['metrics']:
                    return 'vmaf'
    except Exception:
        pass

    return None

def generate_output_paths(metric_files: dict) -> Tuple[Path, dict]:
    """
    Generate output paths in same directory as metric files.
    Returns (plot_path, stats_paths_dict).
    """
    # Find common directory and prefix
    files = [f for f in metric_files.values() if f is not None]
    common_dir = files[0].parent

    # Extract common prefix (e.g., "chunk_001" from "chunk_001_psnr.log")
    common_prefix = files[0].stem.split('_')[0]  # Simplified logic

    plot_path = common_dir / f"{common_prefix}_metrics.png"
    stats_paths = {
        metric: common_dir / f"{common_prefix}_{metric}_stats.txt"
        for metric in metric_files.keys()
        if metric_files[metric] is not None
    }

    return plot_path, stats_paths

def main():
    parser = argparse.ArgumentParser(
        description="Unified Video Metrics Visualization (single chunk analysis)"
    )

    # Positional arguments for metric files
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="Metric files (1-3 files, auto-detected by content)"
    )

    # Required sampling factor
    parser.add_argument(
        "--factor",
        type=int,
        required=True,
        help="Frame sampling factor (required for alignment)"
    )

    parser.add_argument("--title", type=str, help="Plot title (optional)")

    args = parser.parse_args()

    # Validate file count
    if len(args.files) > 3:
        parser.error("Maximum 3 metric files allowed")

    # Detect metric types
    metric_files = {'psnr': None, 'ssim': None, 'vmaf': None}

    for file_path in args.files:
        if not file_path.exists():
            parser.error(f"File not found: {file_path}")

        metric_type = detect_metric_type(file_path)
        if metric_type is None:
            parser.error(f"Unknown metric file format: {file_path}")

        if metric_files[metric_type] is not None:
            parser.error(f"Duplicate {metric_type.upper()} file specified")

        metric_files[metric_type] = file_path

    # Generate output paths
    plot_path, stats_paths = generate_output_paths(metric_files)

    # Call API
    stats = analyze_chunk_quality(
        psnr_log=metric_files['psnr'],
        ssim_log=metric_files['ssim'],
        vmaf_json=metric_files['vmaf'],
        factor=args.factor,
        output_path=plot_path,
        title=args.title
    )

    print(f"\nPlot saved to: {plot_path}")
    for metric, path in stats_paths.items():
        if metric_files[metric] is not None:
            print(f"{metric.upper()} stats saved to: {path}")

    # Print summary
    print("\nQuality Statistics:")
    for metric, metric_stats in stats.items():
        if metric_stats:
            print(f"\n{metric.upper()}:")
            print(f"  Min: {metric_stats['min']:.2f}")
            print(f"  Median: {metric_stats['median']:.2f}")
            print(f"  Max: {metric_stats['max']:.2f}")
            print(f"  Std Dev: {metric_stats['std']:.2f}")

if __name__ == "__main__":
    main()
```

## Data Models

### DataFrame Structure

All parsers return DataFrames with consistent structure:

```python
# Index: frameNum (int) - actual frame number in video
# Columns: metric-specific value column(s)

# PSNR DataFrame
frameNum | psnr_avg
---------|----------
0        | 42.5
10       | 43.2
20       | inf

# SSIM DataFrame
frameNum | ssim_avg
---------|----------
0        | 0.95
10       | 0.96
20       | 0.97

# VMAF DataFrame
frameNum | vmaf_score
---------|------------
0        | 95.5
10       | 96.2
20       | 97.1
```

### Statistics Dictionary

```python
{
    'min': 30.5,
    'p5': 35.2,
    'p10': 37.8,
    'p25': 40.1,
    'p50': 42.5,  # median
    'p75': 45.3,
    'p90': 48.7,
    'p95': 50.2,
    'max': 55.8,
    'std': 5.3
}
```

### API Return Structure

```python
{
    'psnr': {
        'min': 30.5,
        'median': 42.5,
        'max': 55.8,
        'std': 5.3
    },
    'ssim': {
        'min': 0.92,
        'median': 0.96,
        'max': 0.99,
        'std': 0.02
    },
    'vmaf': {
        'min': 90.5,
        'median': 95.2,
        'max': 98.7,
        'std': 2.1
    }
}
```

## Error Handling

### Parser Errors

- **FileNotFoundError**: Raised when metric file doesn't exist
- **ValueError**: Raised when file format is invalid or doesn't match expected metric type
- **JSONDecodeError**: Raised when VMAF JSON is malformed

### Visualization Errors

- **ValueError**: Raised when no valid metrics provided
- **IOError**: Raised when output path is not writable

### API Error Handling Strategy

```python
def analyze_chunk_quality(...) -> ChunkQualityStats:
    stats = {}

    if psnr_log:
        try:
            df = parse_psnr_file(psnr_log, factor)
            stats['psnr'] = _extract_key_stats(compute_statistics(df['psnr_avg']))
        except Exception as e:
            print(f"Warning: Failed to parse PSNR: {e}")
            stats['psnr'] = None

    # Similar for SSIM and VMAF

    if not any(stats.values()):
        raise ValueError("No valid metrics could be parsed")

    return stats
```

## Testing Strategy

### Unit Tests

1. **Parser Tests** (`test_parsers.py`)
   - Test each parser with valid log files
   - Test error handling for invalid formats
   - Test frame number computation with various factors
   - Test edge cases (empty files, single frame, infinite values)

2. **Statistics Tests** (`test_statistics.py`)
   - Test quantile computation accuracy
   - Test outlier filtering for std calculation
   - Test handling of infinite and NaN values
   - Test with various data distributions

3. **Visualization Tests** (`test_visualization.py`)
   - Test plot generation with single metric
   - Test plot generation with multiple metrics
   - Test dual Y-axis configuration
   - Test file output creation
   - Mock matplotlib to avoid GUI dependencies

4. **API Tests** (`test_api.py`)
   - Test with all metrics provided
   - Test with subset of metrics
   - Test with no metrics (error case)
   - Test output file generation
   - Test return value structure

### Integration Tests

1. **End-to-End CLI Test**
   - Run CLI with sample log files
   - Verify plot and stats files created
   - Verify output format

2. **Pipeline Integration Test**
   - Simulate encoding pipeline workflow
   - Call API with chunk metrics
   - Verify decision logic can use returned stats

### Test Data

Create sample log files in `tests/fixtures/`:

- `sample_psnr.log`: 100 frames with realistic PSNR values
- `sample_ssim.log`: 100 frames with SSIM values
- `sample_vmaf.json`: 100 frames with VMAF scores
- `invalid_*.log`: Malformed files for error testing

## Migration from Existing `psnr.py`

### Approach

The existing `psnr.py` will remain as a standalone script for PSNR-only analysis. The new unified system will be a separate module that can handle all three metrics. Users can choose which tool to use based on their needs:

- Use `psnr.py` for quick PSNR-only analysis
- Use new `metrics_visualization` module for multi-metric analysis

### Code Reuse

The new parsers module will extract and refactor the parsing logic from `psnr.py`:

- `parse_psnr_line()` → moved to `parsers.py`
- `parse_psnr_file()` → updated in `parsers.py` with frame alignment fix
- `compute_statistics()` → moved to `statistics.py` (unchanged)
- `create_psnr_plot()` → refactored into `visualization.py` with multi-metric support

### No Breaking Changes

Since `psnr.py` remains unchanged, existing scripts and workflows continue to work.

## Performance Considerations

### Memory Usage

- DataFrames are loaded one at a time
- Statistics computed incrementally
- Plot generation uses streaming where possible

### Optimization Opportunities

1. **Lazy Loading**: Only parse metrics that are requested
2. **Caching**: Cache parsed DataFrames if analyzing multiple times
3. **Parallel Parsing**: Use multiprocessing for multiple metric files
4. **Chunked Reading**: For very large log files, process in chunks

### Expected Performance

- Parsing: ~1-2 seconds per 10,000 frames
- Statistics: <100ms per metric
- Visualization: ~2-3 seconds for full plot
- Total: <10 seconds for typical video chunk analysis

## Dependencies

### Required Packages

- `pandas`: DataFrame operations
- `numpy`: Numerical computations
- `matplotlib`: Plotting
- `argparse`: CLI (standard library)
- `json`: VMAF parsing (standard library)
- `pathlib`: Path handling (standard library)

### Version Requirements

```toml
[project.dependencies]
pandas = ">=2.0.0"
numpy = ">=1.24.0"
matplotlib = ">=3.7.0"
```

No new dependencies beyond what `psnr.py` already uses.

## Future Enhancements

### Potential Extensions

1. **Additional Metrics**: MS-SSIM, PSNR-HVS, VIF
2. **Interactive Plots**: Plotly for zoom/pan capabilities
3. **Comparison Mode**: Overlay multiple encoding attempts
4. **Heatmaps**: Spatial quality distribution
5. **Video Preview**: Embed thumbnail frames at quality dips
6. **Export Formats**: CSV, JSON, HTML reports
7. **Real-time Monitoring**: Stream metrics during encoding

### API Evolution

Future API could support:

```python
# Batch analysis
results = analyze_multiple_chunks([
    {'psnr': 'chunk1_psnr.log', 'ssim': 'chunk1_ssim.log'},
    {'psnr': 'chunk2_psnr.log', 'ssim': 'chunk2_ssim.log'},
])

# Quality thresholds
stats = analyze_chunk_quality(
    ...,
    thresholds={'vmaf': 95.0, 'psnr': 40.0},
    alert_on_failure=True
)
```
