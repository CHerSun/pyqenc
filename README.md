# pyqenc

<!-- markdownlint-disable MD024 MD026 MD028 -->

pyqenc (`PY`thon `Q`uality-based `E`ncoder) - an encoding pipeline that achieves user-specified quality targets while optimizing file size through intelligent CRF adjustment, automatic crop detection, and scene-based chunking.

> This project was inspired by [Av1an](https://github.com/rust-av/Av1an).

> AWS and Kiro IDE team - thank you for the agentic IDE and welcome credits. This allowed me to prototype this project incredibly fast. Truly a new approach to development.

## Problem & Solution

### The Problem

Traditional video encoding approaches face several challenges:

- **Fixed CRF encoding** produces unpredictable quality across different scenes
- **Target bitrate encoding** doesn't guarantee consistent quality
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
- ✅ Scene-based chunking with frame-perfect splits
- ✅ Target CRF search
- ✅ Multiple codec support (h.264 8-bit, h.265 10-bit)
- ✅ Custom encoding strategies via configuration
- ✅ Optimization phase to choose the best encoding strategy (optional)
- ✅ Parallel chunk encoding (configurable concurrency)
- ✅ Audio processing with day/night normalization modes and dialogs boosting
- ✅ Artifact-based resumption (no explicit resume needed)
- ✅ Dry-run mode to preview operations
- ✅ Comprehensive logging and progress reporting
- Currently targeting only MKV sources. Remux into MKV if needed.

## Installation

### Prerequisites

#### External Dependencies:

- **FFmpeg** - for video encoding, scene detection, metrics calculation.
- **MKVToolNix** - MKV stream extraction and merging.

```sh
# Windows (using Scoop)
scoop install ffmpeg mkvtoolnix

# macOS (using Homebrew)
brew install ffmpeg mkvtoolnix

# Linux (Ubuntu/Debian)
sudo apt install ffmpeg mkvtoolnix
```

### Run pyqenc directly using `uv`

This is the recommended way. It has no external python dependencies, `uv` will create a local `.venv` with everything required.

```sh
git clone <repository-url>
cd pyqenc

uv run pyqenc <your_arguments>
```

### Install pyqenc

This needs global python of version >=3.13. Install using `uv`:

```sh
git clone <repository-url>
cd pyqenc

uv pip install .
```

After installation, the `pyqenc` command will be available in your terminal. To run then:

```sh
pyqenc <your_arguments>
```

## Quick Start

### Basic Usage

See installation section on how to run depending on the way you installed. See `--help` for full help.

```sh
# Dry-run mode - preview what's to be done (1 phase ahead) with default settings
pyqenc auto movie.mkv

# Execute (`-y`) the automatic pipeline using the specified work dir with default settings.
pyqenc auto movie.mkv --work-dir ./work -y

# Execute with custom quality targets and strategies selection
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq --work-dir ./work -y
```

> NOTE: It is highly recommended to use separate `--work-dir` per encode.

Default settings:

- target VMAF min >=93, VMAF med >=96;
- use all strategies defined in the config;
- enable optimization phase to search for the best variant.

### Command line basic examples

Slow h265 strategy tuned to better encode dark scenes and for crisper look with higher quality targets:

```sh
pyqenc auto movie.mkv --quality-target vmaf-min:95,vmaf-med:98 --strategies slow+h265-aq --work-dir ./work -y
```

Fast basic h.264 encoding strategy targeting only the VMAF min score:

```sh
pyqenc auto movie.mkv --quality-target vmaf-min:93 --strategies fast+h264-default --work-dir ./work -y
```

Search through multiple strategies for the best one (or a few) and encode to it:

```sh
pyqenc auto movie.mkv --strategies slow+h265-aq,veryslow+h265-anime --work-dir ./work -y
```

Encode using all strategies chosen with NO optimization phase:

```sh
pyqenc auto movie.mkv --strategies slow+h265-aq,veryslow+h265-anime --all-strategies --work-dir ./work -y
```

Wildcard strategy selection (slow preset + all h265 profiles):

```sh
pyqenc auto movie.mkv --strategies slow+h265* --work-dir ./work -y
```

> NOTE: Some shells might need to escape the `*` character. The easiest is to just enclose full `slow+h265*` in quotes `"slow+h265*"` - this normally helps.

Disable automatic cropping:

```sh
pyqenc auto movie.mkv --no-crop --work-dir ./work -y
```

Manual crop specification:

```sh
# Vertical crop only (most common)
pyqenc auto movie.mkv --crop "140 140" -y

# Full crop specification (top bottom left right)
pyqenc auto movie.mkv --crop "140 140 0 0" -y
```

## CLI Reference

### Main Command

```sh
pyqenc auto <source_video> [options]
```

### Global Options

| Option              | Description                                        | Default  |
| ------------------- | -------------------------------------------------- | -------- |
| `--work-dir PATH`   | Working directory for intermediate files           | `./work` |
| `--log-level LEVEL` | Logging level (debug, info, warning, critical)     | `info`   |
| `-y, --execute`     | Execute phases (no flag = dry-run, `-y` = execute) | dry-run  |

### Quality & Strategy Options

| Option                     | Description                                             | Default                        |
| -------------------------- | ------------------------------------------------------- | ------------------------------ |
| `--quality-target TARGETS` | Quality targets (see format below)                      | `vmaf-min:93,vmaf-med:96`      |
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
| `--no-crop`       | Disable automatic black border detection                      | Auto-detect |
| `--crop "VALUES"` | Manual crop (format: "top bottom" or "top bottom left right") | Auto-detect |

> NOTE: `--no-crop` is the same as `--crop "0 0 0 0"`, just a short-hand.

### Stream Filtering Options

| Option                  | Description                                                          | Example            | Default                            |
| ----------------------- | -------------------------------------------------------------------- | ------------------ | ---------------------------------- |
| `--include REGEX`       | Regex pattern to filter streams                                      | `"\b(RUS\|ENG)\b"` | Include all                        |
| `--exclude REGEX`       | Regex pattern to filter streams away                                 | `"\bJPN\b"`        | Exclude none                       |
| `--audio-convert REGEX` | Regex pattern to tell which audio streams to convert to final format | `"5\.1"`           | All normalized and all 2.0 results |

### Quality Target Format

Quality targets specify minimum acceptable quality using metrics and statistics:

**Format:** `metric-statistic:value[,metric-statistic:value,...]`

#### Metrics:

- `vmaf` - Video Multimethod Assessment Fusion (0-100.0 scale)
- `ssim` - Structural Similarity Index (0.0-1.0 scale normalized to 0.0-100.0 scale)
- `psnr` - Peak Signal-to-Noise Ratio (dB scale, clipped to 0.0-100.0 scale; good quality is normally around 40-60)

#### Statistics:

- `min` - Minimum score across all frames
- `med` or `median` - Median score across all frames

**Default:** If not specified, defaults to `vmaf-min:93,vmaf-med:96`

#### Examples:

```sh
# Single target: VMAF minimum of 95
--quality-target vmaf-min:95

# Multiple targets: VMAF minimum 95 AND median 98
--quality-target vmaf-min:95,vmaf-med:98

# SSIM target (note the normalized 0-100 scale)
--quality-target ssim-min:98

# PSNR target (dB scale clipped to 0-100, but normally in range ~40-60 for good quality)
--quality-target psnr-min:45

# Mixed metrics
--quality-target vmaf-min:95,ssim-med:99,psnr-min:45
```

### Strategy Format

Strategies combine encoder presets with custom profiles. The pipeline supports flexible strategy specifications including wildcards.

**Format:** `preset+profile[,preset+profile,...]`

**Presets:** (encoder presets)

- `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`
- `medium`, `slow`, `slower`, `veryslow`, `placebo`

**Profiles:** (defined in configuration file)

- `h264` - Default h.264 8-bit encoding
- `h265` - Default h.265 10-bit encoding
- `h265-aq` - h.265 with adaptive quantization tuning (crisper, better dark areas details)
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

It is possible to run individual phases. See `--help` for subcommands (`extract`, `chunk` ...; see the `--help` for the list).

> NOTE: It is not recommended to use manual mode unless you really know what you are doing.

## Configuration File

Configuration files are searched in this order:

1. `./pyqenc.yaml` (current directory)
2. `~/.config/pyqenc/config.yaml` (user config)
3. Built-in defaults (embedded in code `<project_folder>\pyqenc\default_config.yaml`)

It is NOT recommended to adjust built-in profile. Make a copy and edit it.

Refer to comments in the config for formatting details.

Through config you can adjust codecs, their presets and profiles, and many other settings.

## Chunking Modes

pyqenc supports two modes for splitting the source video into chunks.

### Lossless FFV1 (Default)

By default, each chunk is re-encoded to lossless FFV1 for frame-perfect scene splitting. No extra settings are required for this.

#### Trade-offs:

- Frame-perfect chunk boundaries
- Chunks are ~5x larger than the source video stream (FFV1 all-intra expansion) - 100 GB per movie for chunking is to be expected.
- Slightly slower chunking phase due to re-encode

### Remux / Stream-Copy (`--remux-chunking`)

Remux mode is NOT recommended currently.

Pass `--remux-chunking` to use remuxing mode. It is impossible to do precise chunking in this mode, scene boundaries are aligned to original video I-frames.

```sh
pyqenc auto movie.mkv <...> --remux-chunking -y
```

#### Trade-offs:

- Scenes are not perfectly aligned
  - This could reduce encoding effectiveness
  - This could introduce discrepancies in length between original video and resulting one, causing audio desync
- Remuxing is much faster and needs less space (1x original video size).

## Crop Detection

### Automatic Detection (Default)

pyqenc automatically detects black borders using ffmpeg's `cropdetect` filter:

- Samples multiple frames
- Uses conservative crop
- The same crop parameters are used through all phases
- Applies crop during encoding only (chunks stay not cropped for compatibility with remux chunking)

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
4. **Optimal Selection**: Chooses strategy with the smallest resulting size (or a few, within tolerance threshold of 5% by default)
5. **Full Encoding**: Encodes all chunks using only the optimal strategy(ies)

#### Benefits:

- Saves encoding time by testing multiple strategies on a small subset of chunks
- Produces only outputs with the best size/quality ratio
- Automatically adapts to content characteristics

#### Example:

```sh
# Tests slow+h265-aq and veryslow+h265-anime on test chunks
# Selects the one with smaller file size
# Produces single output with optimal strategy
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq,veryslow+h265-anime -y
```

### Disable Optimization (All Strategies)

Use `--all-strategies` to disable optimization and produce outputs for all strategies:

```sh
# Encodes ALL chunks with BOTH strategies
# Produces TWO output files (one per strategy)
pyqenc auto movie.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq,veryslow+h265-anime --all-strategies -y
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
├── job.yaml               # Job parameters
├── chunking.yaml          # Detected scenes for chunking
├── optimization.yaml      # Optimization phase parameters
├── encoding.yaml          # Encoding phase parameters
├──
├── extracted/                 # Extracted streams and attachments
│   ├── "#0 ID=0 (video) res=1920x1080.mkv"
│   ├── "#1 ID=1 (audio-dts) lang=rus ch=5.1(side).mka"
│   ├── "#2 ID=2 (audio-ac3) lang=eng ch=5.1(side).mka"
│   └── "chapters.xml"
├── chunks/                    # Scene-based chunks
│   ├── "00꞉00꞉00․000-00꞉01꞉18․667.mkv"
│   ├── "00꞉01꞉18․667-00꞉01꞉39․542.mkv"
│   └── ...
├── encoding/                  # Chunks encoding attempts
│   └── slow+h265-aq/              # Strategy subfolder with its chunk attempts
│       ├── "00꞉00꞉00․000-00꞉01꞉18․667.1920x1024.crf20.0/"          # raw metrics subfolder
│       ├── "00꞉00꞉00․000-00꞉01꞉18․667.1920x1024.crf20.0.mkv"       # encoded attempt
│       ├── "00꞉00꞉00․000-00꞉01꞉18․667.1920x1024.crf20.0.png"       # metrics graph for the attempt
│       ├── "00꞉00꞉00․000-00꞉01꞉18․667.1920x1024.crf20.0.yaml"      # sidecar with calculated metrics snapshot
│       └── ...
├── encoded/                   # Winning chunks encoding attempts (hard-links or copies) - to be merged into final video
│   └── slow+h265-aq/              # Strategy subfolder with its chunk attempts
│       ├── "00꞉00꞉00․000-00꞉01꞉18․667.1920x1024.crf20.0.mkv"       # winning chunk attempt
│       ├── "00꞉00꞉00․000-00꞉01꞉18․667.1920x1024.crf20.0.png"       # metrics graph for the winning attempt
│       ├── "00꞉00꞉00․000-00꞉01꞉18․667.1920x1024.crf20.0.yaml"      # sidecar with calculated metrics snapshot
│       └── ...
├── audio/                     # Processed audio
│   ├── audio_001_day.aac
│   ├── audio_001_night.aac
│   └── ...
└── final/                     # Final output
    └── movie_slow+h265-aq.mkv     # 1 variant per strategy used
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

**Solution:** Encoding requires significant intermediate disk space. The amount depends greatly on chunking mode and number of strategies:

- **Lossless mode (default):** ~6-7x source size (5x for FFV1 chunks + extraction + audio)
- **Remux mode (`--remux-chunking`):** ~2-3x source size (stream-copy chunks + extraction + audio)

Options:

- Check available space: `df -h` (Linux/macOS) or `dir` (Windows)
- Use a different working directory: `--work-dir /path/to/large/disk`
- Use `--remux-chunking` to reduce chunk storage at the cost of frame-perfect splits
- Use `--cleanup` flag for intermediate results cleanups (care: in case of changing arguments this could require re-encoding of all chunks).
- Clean up previous working directories

### Slow Encoding

**Issue:** Encoding is taking too long

#### Solutions:

1. **Use faster preset**: Try `fast` or `medium` instead of `slow`
2. **Increase parallelism**: `--max-parallel 4` (if you have idle CPU cores; normally `ffmpeg` should use all already)
3. **Use different codec**: h.264 is significantly faster than h.265. AV1 is very slow.

### Invalid Strategy or Profile

**Error:** `Unknown profile: xyz`

**Solution:** Check available profiles:

1. Review configuration file (`./pyqenc.yaml`, `~/.config/pyqenc/config.yaml` or built-in config)
2. Use built-in profiles: `h264`, `h265`, `h265-aq`, `h265-anime`
3. Verify profile name matches configuration exactly (case-sensitive)
4. Check strategy format: `preset+profile` (e.g., `slow+h265-aq`)

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
  --include "\beng\b" \
  -y

# Exclude commentary tracks
pyqenc auto movie.mkv \
  --quality-target vmaf-min:95 \
  --strategies slow+h265-aq \
  --exclude "commentary" \
  -y
```

## Performance notes

1. **Use Strategy Optimization** (Default): Let the pipeline find the best strategy automatically
   - Tests all strategies on ~1% of chunks
   - Selects strategy with the smallest file size meeting quality targets
   - Uses extra time for optimization phase, but can save encoding time significantly

2. **Parallel Encoding**: We use concurrency of 2 to avoid orchestrator-caused time wasting. Normally ffmpeg already scales onto all available CPUs. You can control this via `--max-parallel` flag

3. **Process Priority**: Main process automatically runs at lower priority to avoid system interference
   - All subprocesses inherit the lowered priority
   - Ensures encoding doesn't impact other activities

## License

This project is open-source software. See [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Support

For issues, questions, or feature requests, please open an issue on the project repository.
