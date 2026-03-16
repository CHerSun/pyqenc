# pyqenc - Quality-Based Video Encoding Pipeline

<!-- markdownlint-disable MD024 MD026 MD028 -->

A comprehensive video encoding pipeline that achieves user-specified quality targets while optimizing file size through intelligent CRF adjustment, automatic black border detection, and scene-based chunking.

> This project was inspired by [Av1an](https://github.com/rust-av/Av1an).

> AWS and Kiro team, thank you for the tooling and welcome credits. This allowed me to prototype this project incredibly fast.

## Problem & Solution

### The Problem

Traditional video encoding approaches face several challenges:

- **Fixed CRF encoding** produces unpredictable quality across different scenes
- **Target bitrate encoding** doesn't guarantee consistent quality
- **Black borders** waste encoding bits and disk space
- **Manual quality verification** is time-consuming and subjective
- **Interrupted encoding** requires starting over from scratch

### The Solution

pyqenc provides a quality-first encoding pipeline that:

- **Guarantees quality targets** using objective metrics (VMAF, SSIM, PSNR)
- **Automatically detects and removes black borders** to optimize encoding efficiency
- **Adjusts CRF iteratively** until quality targets are met for each scene
- **Supports multiple codecs** (h.264 8-bit, h.265 10-bit) with custom profiles
- **Resumes seamlessly** from interruptions using artifact-based detection
- **Processes in parallel** to maximize CPU utilization
- **Provides detailed progress** with visual feedback and logging

## Features

- ✅ Quality-targeted encoding with VMAF, SSIM, and PSNR metrics
- ✅ Automatic black border detection and cropping
- ✅ Scene-based chunking with frame-perfect splits (FFV1 lossless, default) or fast stream-copy (`--remux-chunking` mode is NOT recommended currently)
- ✅ Intelligent target CRF search
- ✅ Multiple codec support (h.264 8-bit, h.265 10-bit)
- ✅ Custom encoding profiles via YAML configuration
- ✅ Parallel chunk encoding (configurable concurrency)
- ✅ Audio processing with day/night normalization modes
- ✅ Artifact-based resumption (no explicit resume needed)
- ✅ Dry-run mode to preview operations
- ✅ Comprehensive logging and progress reporting
- ✅ Support for multiple encoding strategies - either in optimization search (selects one best strategy) or in all mode (transcode to all strategies).

## Installation

### Prerequisites

#### External Dependencies:

1. **FFmpeg** (>= 5.0) - Video encoding, scene detection, metrics calculation

   ```sh
   # Windows (using Scoop)
   scoop install ffmpeg

   # macOS (using Homebrew)
   brew install ffmpeg

   # Linux (Ubuntu/Debian)
   sudo apt install ffmpeg
   ```

2. **MKVToolNix** (>= 70.0) - MKV stream extraction and merging

   ```sh
   # Windows (using Scoop)
   scoop install mkvtoolnix

   # macOS (using Homebrew)
   brew install mkvtoolnix

   # Linux (Ubuntu/Debian)
   sudo apt install mkvtoolnix
   ```

#### Python Requirements:

- Python >= 3.13

### Install pyqenc

Using `uv` (recommended):

```sh
# Clone the repository
git clone <repository-url>
cd pyqenc

# Install with uv
uv pip install -e .
```

Using pip:

```sh
pip install -e .
```

After installation, the `pyqenc` command will be available in your terminal.

## Quick Start

### Basic Usage

```sh
# Dry-run mode (default) - preview what would be done
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq

# Execute the pipeline
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq -y

# Execute with custom working directory
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq --work-dir ./work -y
```

### Common Workflows

#### High-quality h.265 encoding:

```sh
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95,vmaf-med:98 \
  --strategies slow+h265-aq \
  -y
```

#### Fast h.264 encoding:

```sh
pyqenc auto movie.mkv \
  --quality-target vmaf-min:93 \
  --strategies fast+h264-default \
  -y
```

#### Multiple strategies with optimization:

```sh
# Tests multiple strategies, selects optimal one (smallest file size meeting quality)
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq,veryslow+h265-anime \
  -y
```

#### All strategies without optimization:

```sh
# Produces output for ALL specified strategies (no optimization)
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq,veryslow+h265-anime \
  --all-strategies \
  -y
```

#### Using default strategies:

```sh
# Uses default strategies from config: veryslow+h264*,slow+h265*
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  -y
```

#### Wildcard strategy testing:

```sh
# Tests all h265 profiles with slow preset
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265* \
  -y
```

#### Disable automatic cropping:

```sh
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq \
  --no-crop \
  -y
```

#### Manual crop specification:

```sh
# Vertical crop only (most common)
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq \
  --crop "140 140" \
  -y

# Full crop specification (top bottom left right)
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq \
  --crop "140 140 0 0" \
  -y
```

## CLI Reference

### Main Command

```sh
pyqenc auto <source_video> [options]
```

### Global Options

| Option              | Description                                                       | Default  |
| ------------------- | ----------------------------------------------------------------- | -------- |
| `--work-dir PATH`   | Working directory for intermediate files                          | `./work` |
| `--log-level LEVEL` | Logging level (debug, info, warning, critical)                    | `info`   |
| `-y, --execute [N]` | Execute phases (no flag = dry-run, `-y` = all, `-y N` = N phases) | dry-run  |

### Quality & Strategy Options

| Option                     | Description                                             | Default                        |
| -------------------------- | ------------------------------------------------------- | ------------------------------ |
| `--quality-target TARGETS` | Quality targets (see format below)                      | `vmaf-med:98`                  |
| `--strategies STRATEGIES`  | Encoding strategies (see format below)                  | `veryslow+h264*,slow+h265*`    |
| `--all-strategies`         | Disable optimization, produce output for all strategies | `False` (optimization enabled) |
| `--max-parallel N`         | Maximum concurrent encoding processes                   | `2`                            |

### Chunking Options

| Option             | Description                                                                 | Default       |
| ------------------ | --------------------------------------------------------------------------- | ------------- |
| `--remux-chunking` | Use stream-copy (`-c copy`) instead of FFV1 lossless re-encode for chunking | Lossless FFV1 |

> NOTE: Remuxing is in ALPHA stage of development. It is NOT recommended. Remuxing has to rely on source video I-frames in the current process. This could produce inaccurate scenes splitting, if source video I-frames do not match detected scenes. Current algorithm could produce wrong final video duration (normally the difference should be very small, if any). Main goal of remuxing is to reduce processing overhead in both CPU and space, but currently it is INCONSISTENT.

### Cropping Options

| Option            | Description                                                   | Default     |
| ----------------- | ------------------------------------------------------------- | ----------- |
| `--no-crop`       | Disable automatic black border detection                      | Enabled     |
| `--crop "VALUES"` | Manual crop (format: "top bottom" or "top bottom left right") | Auto-detect |

### Stream Filtering Options

| Option                 | Description                           | Example     |
| ---------------------- | ------------------------------------- | ----------- |
| `--video-filter REGEX` | Regex pattern to filter video streams | `".*eng.*"` |
| `--audio-filter REGEX` | Regex pattern to filter audio streams | `".*eng.*"` |

### Quality Target Format

Quality targets specify minimum acceptable quality using metrics and statistics:

**Format:** `metric-statistic:value[,metric-statistic:value,...]`

#### Metrics:

- `vmaf` - Video Multimethod Assessment Fusion (0-100 scale)
- `ssim` - Structural Similarity Index (0.0-1.0 scale)
- `psnr` - Peak Signal-to-Noise Ratio (dB scale)

#### Statistics:

- `min` - Minimum score across all frames
- `med` or `median` - Median score across all frames

**Default:** If not specified, defaults to `vmaf-med:98`

#### Examples:

```sh
# Single target: VMAF minimum of 95
--quality-target vmaf-min:95

# Multiple targets: VMAF minimum 95 AND median 98
--quality-target vmaf-min:95,vmaf-med:98

# SSIM target (note 0-1 scale)
--quality-target ssim-min:0.98

# PSNR target (dB scale)
--quality-target psnr-min:45

# Mixed metrics
--quality-target vmaf-min:95,ssim-med:0.99,psnr-min:45
```

### Strategy Format

Strategies combine encoder presets with custom profiles. The pipeline supports flexible strategy specifications including wildcards.

**Format:** `preset+profile[,preset+profile,...]`

**Presets:** (ffmpeg encoder presets)

- `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`
- `medium`, `slow`, `slower`, `veryslow`, `placebo`

**Profiles:** (defined in configuration file)

- `h264` - Default h.264 8-bit encoding
- `h265` - Default h.265 10-bit encoding
- `h265-aq` - h.265 with adaptive quantization tuning
- `h265-anime` - h.265 optimized for anime content

**Default Strategies:** If not specified, uses `veryslow+h264*,slow+h265*` from configuration file

#### Wildcard Support:

The strategy specification supports wildcards for flexible testing:

```sh
# Specific preset+profile combination
--strategies slow+h265-aq

# Preset with profile wildcard (all h265 profiles with slow preset)
--strategies slow+h265*
# Expands to: slow+h265, slow+h265-aq, slow+h265-anime

# Preset only (all profiles with slow preset)
--strategies slow
# Expands to: slow+h264, slow+h265, slow+h265-aq, slow+h265-anime

# Profile wildcard only (all presets with h265 profiles)
--strategies +h265*
# Expands to: ultrafast+h265, ultrafast+h265-aq, ..., placebo+h265-anime

# Specific profile only (all presets with h265-aq profile)
--strategies +h265-aq
# Expands to: ultrafast+h265-aq, superfast+h265-aq, ..., placebo+h265-aq

# Empty string (all preset+profile combinations)
--strategies ""
# Expands to all combinations across all codecs

# Multiple patterns (comma-separated)
--strategies slow+h265*,veryslow+h264
# Expands to: slow+h265, slow+h265-aq, slow+h265-anime, veryslow+h264
```

#### Examples:

```sh
# Single strategy
--strategies slow+h265-aq

# Multiple strategies with optimization (finds best one)
--strategies slow+h265-aq,veryslow+h265-anime

# Test all h265 profiles with slow preset
--strategies slow+h265*

# Test all presets with h265-aq profile
--strategies +h265-aq

# h.264 encoding
--strategies fast+h264

# Mixed codecs
--strategies slow+h265-aq,medium+h264
```

## Phase-Specific Subcommands

For advanced users who want to run individual phases:

### Extract Streams

```sh
pyqenc extract <source_video> [options]

Options:
  --work-dir PATH          Working directory
  --video-filter REGEX     Filter video streams
  --audio-filter REGEX     Filter audio streams
  --no-crop                Disable crop detection
  --crop "VALUES"          Manual crop specification
  -y, --execute            Execute (default: dry-run)
```

### Chunk Video

```sh
pyqenc chunk <video_file> [options]

Options:
  --work-dir PATH          Working directory
  --scene-threshold FLOAT  Scene detection sensitivity (0.0-1.0)
  --min-scene-length INT   Minimum frames per chunk
  --remux-chunking         Use stream-copy instead of FFV1 lossless re-encode
  -y, --execute            Execute (default: dry-run)
```

### Encode Chunks

```sh
pyqenc encode <chunks_dir> [options]

Options:
  --work-dir PATH          Working directory
  --strategies STRATEGIES  Encoding strategies
  --quality-target TARGETS Quality targets
  --max-parallel N         Concurrent processes
  -y, --execute            Execute (default: dry-run)
```

### Process Audio

```sh
pyqenc audio <audio_dir> [options]

Options:
  --work-dir PATH          Working directory
  -y, --execute            Execute (default: dry-run)
```

### Merge Final Video

```sh
pyqenc merge <video_dir> <audio_dir> [options]

Options:
  --work-dir PATH          Working directory
  --output PATH            Output file path
  -y, --execute            Execute (default: dry-run)
```

## Configuration File

### Location

Configuration files are searched in this order:

1. `./pyqenc.yaml` (current directory)
2. `~/.config/pyqenc/config.yaml` (user config)
3. Built-in defaults (embedded in code)

### Format

Create a `pyqenc.yaml` file to customize codecs, profiles, and default strategies:

```yaml
# Default strategies to use when --strategies not specified
default_strategies:
  - "veryslow+h264*"
  - "slow+h265*"

# Codec definitions with base settings
codecs:
  h264-8bit:
    encoder: libx264
    pixel_format: yuv420p
    default_crf: 23
    crf_range: [0, 51]
    presets:
      - ultrafast
      - superfast
      - veryfast
      - faster
      - fast
      - medium
      - slow
      - slower
      - veryslow
      - placebo

  h265-10bit:
    encoder: libx265
    pixel_format: yuv420p10le
    default_crf: 20
    crf_range: [0, 51]
    presets:
      - ultrafast
      - superfast
      - veryfast
      - faster
      - fast
      - medium
      - slow
      - slower
      - veryslow
      - placebo

# Encoding profiles organized by codec
profiles:
  # h.264 8-bit profiles
  h264:
    codec: h264-8bit
    description: "Default h.264 8-bit encoding"
    extra_args: []

  # h.265 10-bit profiles
  h265:
    codec: h265-10bit
    description: "Default h.265 10-bit encoding"
    extra_args: []

  h265-aq:
    codec: h265-10bit
    description: "h.265 with adaptive quantization"
    extra_args:
      - "-x265-params"
      - "aq-mode=3:aq-strength=0.8"

  h265-anime:
    codec: h265-10bit
    description: "h.265 optimized for anime"
    extra_args:
      - "-x265-params"
      - "aq-mode=2:psy-rd=1.0:deblock=-1,-1"
```

### Customizing Profiles

To add a custom profile:

1. Choose a codec (`h264-8bit` or `h265-10bit`)
2. Pick a unique profile name (e.g., `h265-custom`)
3. Add encoder-specific arguments in `extra_args`

Example custom profile:

```yaml
profiles:
  h265-custom:
    codec: h265-10bit
    description: "Custom h.265 profile for specific content"
    extra_args:
      - "-x265-params"
      - "aq-mode=3:aq-strength=1.0:psy-rd=2.0"
```

Then use it with:

```sh
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-custom -y
```

### Customizing Default Strategies

Modify the `default_strategies` section to change what strategies are used when `--strategies` is not specified:

```yaml
default_strategies:
  - "slow+h265*"      # All h265 profiles with slow preset
  - "medium+h264"     # h264 profile with medium preset
```

## Chunking Modes

pyqenc supports two modes for splitting the source video into chunks.

### Lossless FFV1 (Default)

By default, each chunk is re-encoded to **FFV1 with `-g 1`** (all-intra). Because every output frame is an I-frame, the split lands on the exact frame the scene detector identified — no I-frame snapping offset.

```sh
# Default behavior — no flag needed
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq -y
```

#### Trade-offs:

- Frame-perfect chunk boundaries
- Chunks are ~5x larger than the source video stream (FFV1 all-intra expansion)
- Slightly slower chunking phase due to re-encode

### Remux / Stream-Copy (`--remux-chunking`)

Pass `--remux-chunking` to fall back to the original `-c copy` split. Chunks are produced by copying the source bitstream directly, so boundaries snap to the nearest I-frame *before* the requested scene timestamp.

```sh
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq --remux-chunking -y
```

#### Trade-offs:

- Chunk boundaries may be off by up to one GOP (not frame-perfect)
- Chunks are approximately the same size as the source video stream
- Fastest possible chunking — no re-encode

#### When to use `--remux-chunking`:

- Disk space is constrained (chunks are ~5x smaller than lossless mode)
- Chunking speed is critical and sub-GOP boundary precision is acceptable
- Source video already has frequent I-frames (e.g., already all-intra)

## Crop Detection

### Automatic Detection (Default)

pyqenc automatically detects black borders using ffmpeg's `cropdetect` filter:

- Samples multiple frames (beginning, middle, end)
- Uses conservative crop (largest area removing all borders)
- Stores crop parameters for use in all phases
- Applies crop during chunking (chunks stored cropped)

Example output:

```log
[INFO] Detected black borders: 140 top, 140 bottom, 0 left, 0 right
[INFO] Original resolution: 1920x1080, Cropped resolution: 1920x800
```

### Manual Crop

Specify crop values manually if automatic detection fails:

```sh
# Vertical crop only (most common for letterboxing)
--crop "140 140"

# Full crop specification (top bottom left right)
--crop "140 140 0 0"
```

### Disable Cropping

To disable cropping entirely:

```sh
--no-crop
```

## Strategy Optimization

### Default Behavior (Optimization Enabled)

By default, pyqenc optimizes encoding by finding the best strategy:

1. **Test Chunk Selection**: Randomly selects ~1% of chunks (minimum 3) from the middle 80% of the video
2. **Strategy Testing**: Encodes test chunks with all specified strategies
3. **Quality Verification**: Ensures all test chunks meet quality targets
4. **Optimal Selection**: Chooses strategy with smallest average file size
5. **Full Encoding**: Encodes all chunks using only the optimal strategy

#### Benefits:

- Saves encoding time by testing strategies on representative samples
- Produces single output with best size/quality ratio
- Automatically adapts to content characteristics

#### Example:

```sh
# Tests slow+h265-aq and veryslow+h265-anime on test chunks
# Selects the one with smaller file size
# Produces single output with optimal strategy
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq,veryslow+h265-anime \
  -y
```

### Disable Optimization (All Strategies)

Use `--all-strategies` to disable optimization and produce output for all strategies:

```sh
# Encodes ALL chunks with BOTH strategies
# Produces TWO output files (one per strategy)
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq,veryslow+h265-anime \
  --all-strategies \
  -y
```

#### When to use:

- You want to compare multiple encodings side-by-side
- You need outputs for different use cases (e.g., archival vs streaming)
- You want to manually select the best result

**Note:** This significantly increases encoding time as all chunks are encoded with all strategies.

## Working Directory Structure

All intermediate files are stored in the working directory:

```log
work/
├── progress.json              # Progress tracker state
├── extracted/                 # Extracted streams
│   ├── video_001.mkv
│   ├── audio_001.mka
│   └── audio_002.mka
├── chunks/                    # Scene-based chunks (cropped)
│   ├── chunk_0001.mkv
│   ├── chunk_0002.mkv
│   └── ...
├── encoded/                   # Encoded chunks by strategy
│   └── slow+h265-aq/
│       ├── chunk_0001_attempt_001.mkv
│       ├── chunk_0001_attempt_001.psnr.log
│       ├── chunk_0001_attempt_001.ssim.log
│       ├── chunk_0001_attempt_001.vmaf.json
│       ├── chunk_0001_attempt_001.png
│       └── ...
├── audio/                     # Processed audio
│   ├── audio_001_day.aac
│   ├── audio_001_night.aac
│   └── ...
└── final/                     # Final output
    └── output_slow+h265-aq.mkv
```

## Resumption & Artifact Reuse

pyqenc automatically resumes from interruptions without explicit resume commands:

### How It Works

1. **Artifact-Based Detection**: Each phase checks for existing artifacts
2. **Automatic Reuse**: Valid artifacts are reused without re-processing
3. **Configuration Changes**: Detects changes and only processes what's needed

### Examples

#### After Interruption:

```sh
# Run the same command again - it will resume automatically
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq -y
```

#### Adding a New Strategy:

```sh
# Only encodes chunks for the new strategy
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq,veryslow+h265-anime -y
```

#### Changing Quality Targets:

```sh
# Re-evaluates existing encodings, only re-encodes chunks that don't meet new targets
pyqenc auto movie.mkv --quality-target vmaf-min:97 --strategies slow+h265-aq -y
```

## Troubleshooting

### FFmpeg Not Found

**Error:** `ffmpeg: command not found`

**Solution:** Install FFmpeg and ensure it's in your PATH:

```sh
# Verify installation
ffmpeg -version
ffprobe -version
```

### MKVToolNix Not Found

**Error:** `mkvmerge: command not found`

**Solution:** Install MKVToolNix and ensure it's in your PATH:

```sh
# Verify installation
mkvmerge --version
mkvextract --version
```

### Insufficient Disk Space

**Error:** `No space left on device`

**Solution:** Encoding requires significant intermediate disk space. The amount depends on chunking mode:

- **Lossless mode (default):** ~6-7x source size (5x for FFV1 chunks + extraction + audio)
- **Remux mode (`--remux-chunking`):** ~2-3x source size (stream-copy chunks + extraction + audio)

Options:

- Check available space: `df -h` (Linux/macOS) or `dir` (Windows)
- Use a different working directory: `--work-dir /path/to/large/disk`
- Use `--remux-chunking` to reduce chunk storage at the cost of frame-perfect splits
- Clean up previous working directories

### Slow Encoding

**Issue:** Encoding is taking too long

#### Solutions:

1. **Use faster preset**: Try `fast` or `medium` instead of `slow`
2. **Increase parallelism**: `--max-parallel 4` (if you have idle CPU cores; normally `ffmpeg` should use all already)
3. **Use different codec**: for example, h.264 is significantly faster than h.265. AV1 is very slow.

### Invalid Strategy or Profile

**Error:** `Unknown profile: xyz`

**Solution:** Check available profiles:

1. Review configuration file (`./pyqenc.yaml`, `~/.config/pyqenc/config.yaml` or built-in config)
2. Use built-in profiles: `h264`, `h265`, `h265-aq`, `h265-anime`
3. Verify profile name matches configuration exactly (case-sensitive)
4. Check strategy format: `preset+profile` (e.g., `slow+h265-aq`)

### Crop Detection Issues

**Issue:** Automatic crop detection removes too much or too little

#### Solutions:

1. **Manual crop**: Specify crop values manually with `--crop "top bottom"`

   ```sh
   pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq --crop "140 140" -y
   ```

2. **Disable crop**: Use `--no-crop` if you want to keep original aspect ratio

   ```sh
   pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq --no-crop -y
   ```

3. **Check sample frames**: Review extraction logs for detected crop values

### Progress Tracker Corruption

**Issue:** Pipeline fails to resume or shows incorrect state

**Solution:** Delete the progress tracker and restart:

```sh
# Remove progress tracker
rm <work_folder>/progress.json

# Restart pipeline (will reuse existing artifacts)
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq --work-dir <work_folder> -y
```

### Python Version Issues

**Error:** `SyntaxError` or `ImportError`

**Solution:** Ensure Python 3.13 or later is installed (globally or using venv):

```sh
# Check Python version
python --version

# Should show Python 3.13.x or later
```

### Strategy Wildcard Not Expanding

**Issue:** Wildcard strategies not working as expected

#### Solutions:

1. **Check profile names**: Ensure profiles exist in configuration
2. **Verify wildcard syntax**: Use `*` for wildcards (e.g., `h265*` matches `h265`, `h265-aq`, `h265-anime`)
3. **Test expansion**: Use dry-run mode to see expanded strategies

   ```sh
   pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265*
   ```

4. **Check logs**: Review info-level logs for strategy expansion details

## Advanced Usage

### Dry-Run Mode

Preview operations before execution:

```sh
# See what would be done
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq

# Output shows:
# - Which artifacts exist and would be reused
# - Which artifacts are missing and would be created
# - Stops at first incomplete phase
```

### Phased Execution

Execute a limited number of phases:

```sh
# Execute only the first phase (extraction)
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq -y 1

# Execute first 3 phases (extraction, chunking, encoding)
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq -y 3
```

### Debug Logging

Enable detailed logging for troubleshooting:

```sh
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq \
  --log-level debug \
  -y
```

### Stream Filtering

Filter specific streams by language or properties:

```sh
# Keep only English streams
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq \
  --video-filter ".*eng.*" \
  --audio-filter ".*eng.*" \
  -y

# Exclude commentary tracks
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq \
  --audio-filter "^(?!.*commentary).*$" \
  -y
```

## Performance Tips

1. **Use Strategy Optimization** (Default): Let the pipeline find the best strategy automatically
   - Tests all strategies on ~1% of chunks (representative samples)
   - Selects strategy with smallest file size meeting quality targets
   - Adds ~5-10 minutes but can save hours of encoding time
   - Enabled by default unless `--all-strategies` is used

2. **Parallel Encoding**: Increase `--max-parallel` if you have CPU cores available
   - Default: 2 concurrent chunks
   - Recommended: Number of physical CPU cores / 2
   - Example: `--max-parallel 4` for 8-core CPU

3. **Faster Presets**: Use `fast` or `medium` for quicker encoding (lower quality)
   - `ultrafast`, `superfast`, `veryfast` - Very fast, lower quality
   - `fast`, `faster` - Good balance of speed and quality
   - `medium` - Default FFmpeg preset
   - `slow`, `slower`, `veryslow` - Better quality, much slower

4. **Lower Quality Targets**: Reduce targets to minimize encoding attempts
   - Each CRF adjustment requires re-encoding the chunk
   - Lower targets (e.g., VMAF 90 vs 95) converge faster

5. **SSD Storage**: Use SSD for working directory to speed up I/O
   - Chunking and metrics calculation are I/O intensive
   - Significant speedup with fast storage

6. **Process Priority**: Main process automatically runs at lower priority to avoid system interference
   - All subprocesses inherit the lowered priority
   - Ensures encoding doesn't impact other activities

7. **Batch Updates**: Progress tracker batches updates to reduce disk I/O
   - Updates are written periodically during encoding
   - Reduces overhead during parallel encoding

8. **Automatic Cropping**: Black border detection optimizes encoding efficiency
   - Removes wasted bits on black borders
   - Reduces file size and encoding time
   - Enabled by default

## License

This project is open-source software. See [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Support

For issues, questions, or feature requests, please open an issue on the project repository.
