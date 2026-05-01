# CLI Reference

<!-- markdownlint-disable MD024 -->

## Commands

```sh
# main subcommands
pyqenc auto    <source_video> [options]   # Full automatic pipeline (main command)
pyqenc measure <source_video> [targets]   # Measure quality metrics
pyqenc config  [target_dir]               # Copy active config for customization
# advanced subcommands
pyqenc extract <source_video> [options]   # Extract streams only
pyqenc chunk   <source_video> [options]   # Chunk video into scenes only
pyqenc encode  <source_video> [options]   # Encode chunks only
pyqenc audio   <source_video> [options]   # Process audio only
pyqenc merge   <source_video> [options]   # Merge final output only
```

Use `pyqenc --help` or `pyqenc <command> --help` for full argument lists.

Only `auto`, `measure` and `config` subcommands are intended for normal usage.

It is NOT recommended to use phase-specific subcommands (`extract`, `chunk`, `encode`, `audio`, `merge`) unless you know what you're doing.

---

## Global Options

Applies to all subcommands.

| Option              | Description                                           | Default |
| ------------------- | ----------------------------------------------------- | ------- |
| `--work-dir PATH`   | Working directory for intermediate files              | `.`     |
| `--log-level LEVEL` | Logging level: `debug`, `info`, `warning`, `critical` | `info`  |
| `-y, --execute`     | Actually execute commands (omit = dry-run preview)    | dry-run |

---

## `auto` Options

### Quality & Strategy

| Option                     | Description                                                                              | Default                                                  |
| -------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `--quality-target TARGETS` | Quality targets (see [Quality Target Format](#quality-target-format))                    | `vif-med:92.0,vmaf-p05:95.0,psnr-med:45.0,ssim-med:98.0` |
| `--strategies STRATEGIES`  | Encoding strategies (see [Strategy Format](#strategy-format))                            | `veryslow+h264*,slow+h265*`                              |
| `--all-strategies`         | Disable optimization — produce output for all strategies                                 | `False` (optimization enabled)                           |
| `--max-parallel N`         | Maximum concurrent encoding processes. Increase only if you see CPU cores underutilized. | `1`                                                      |

### Cropping

| Option            | Description                                                                            | Default     |
| ----------------- | -------------------------------------------------------------------------------------- | ----------- |
| `--crop "VALUES"` | Manual crop: `"top bottom"` or `"top bottom left right"`. Use `"0 0"` for no cropping. | Auto-detect |

Automatic crop detection uses ffmpeg's `cropdetect` filter. The same crop parameters are applied consistently across all phases. Crop is applied during encoding only — chunks stay uncropped for remux compatibility.

> NOTE: ⚠ Change of cropping value midway requires full re-encode.

### Stream Filtering

Applied during extraction phase, use dry-run to preview.

| Option                  | Description                                                          | Example            | Default                            |
| ----------------------- | -------------------------------------------------------------------- | ------------------ | ---------------------------------- |
| `--include REGEX`       | Regex pattern to include streams                                     | `"\b(RUS\|ENG)\b"` | Include all                        |
| `--exclude REGEX`       | Regex pattern to exclude streams                                     | `"comment"`        | Exclude none                       |
| `--audio-convert REGEX` | Regex pattern selecting processed audio files to convert to delivery format | `"5\.1"`    | All normalized and all 2.0 results |

### Chunking

| Option             | Description                                                                 | Default       |
| ------------------ | --------------------------------------------------------------------------- | ------------- |
| `--remux-chunking` | Use stream-copy (`-c copy`) instead of FFV1 lossless re-encode for chunking | Lossless FFV1 |

> NOTE: ⚠ Remux chunking is **NOT recommended**. It relies on source I-frames for scene boundaries, which can produce inaccurate splits and potential audio desync. Its main benefit is reduced disk usage (~1x source size vs ~5x for FFV1).

### Cleanup

| Option      | Description                                                                                         | Default |
| ----------- | --------------------------------------------------------------------------------------------------- | ------- |
| `--cleanup` | Remove intermediate files after successful step completion. Reduces ability to resume/adjust later. | Off     |

---

## `config` Subcommand

Copies the active configuration file to a target location for customisation.

```sh
pyqenc config              # Copy to ~/.config/pyqenc/config.yaml (user home)
pyqenc config .            # Copy to ./pyqenc.yaml (current directory)
pyqenc config /some/dir    # Copy to /some/dir/config.yaml
pyqenc config -y           # Actually execute (default is dry-run)
```

Config search order (first found wins):

1. `./pyqenc.yaml` (current directory)
2. `~/.config/pyqenc/config.yaml` (user home)
3. Built-in defaults (embedded in the package)

---

## Quality Target Format

**Format:** `metric-statistic:value[,...]`

### Metrics

| Metric | Scale                       | Notes                                                                                                                             |
| ------ | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `vmaf` | 0–100                       | Main perceived quality metric. Biased toward smooth content — combine with `vif` if film grain matters. Good quality is ~95+      |
| `vif`  | 0–100                       | Visual Information Fidelity. Embedded in the VMAF pass. Less biased than VMAF for noisy/grainy content. Good quality is ~90+.     |
| `psnr` | 0–100 (clipped, dB scale)   | Good quality is typically in 40–60 dB range. Stable and reliable, but doesn't directly correlates with perceived quality.         |
| `ssim` | 0–100 (normalized from 0–1) | Non-linear scale, compressed near 100. Good quality is ~98+. Use as a secondary control. Badly correlates with perceived quality. |

### Statistics

| Statistic       | Description |
| --------------- | ----------- |
| `min`           | Minimum across all frames — avoid, sensitive to outliers |
| `p05`           | 5th percentile — **recommended over `min`** |
| `p25`           | 25th percentile |
| `med`, `median` | Median (50th percentile) |
| `p75`           | 75th percentile |
| `p95`           | 95th percentile |
| `max`           | Maximum across all frames — avoid, often useless for measuring. |

> NOTE: `vmaf-min` is unreliable due to a first-frame bias (VMAF lacks motion context on frame 0). Use `vmaf-p05` instead. See [quality-targeting.md](quality-targeting.md) for a detailed discussion.

### Examples

```sh
# Recommended baseline (multiple metrics, stable statistics)
--quality-target vif-med:92,vmaf-p05:95,psnr-med:45,ssim-med:98

# Higher quality targets for near-lossless archival
--quality-target vif-med:94,vmaf-p05:97,psnr-med:48,ssim-med:99
```

---

## Strategy Format

**Format:** `preset+profile[,preset+profile,...]`

### Presets

Presets are defined in the codec itself, we only use them.

**h.264** and **h.265** codecs define profiles as: `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium`, `slow`, `slower`, `veryslow`, `placebo`, with `placebo` being the max quality one.

Presets are not the same between codecs, even though they use the same naming. For h.264 you might want `veryslow` - it still has acceptable speed normally and gives good benefits. For h.265 `veryslow` is painfully slow, normally you'd go for `slow` for h.265.

**nvenc** defines codecs as: `p1`...`p7` with `p7` being the max quality one.

### Built-in Profiles

| Profile      | Description                                                         |
| ------------ | ------------------------------------------------------------------- |
| `h264`       | h.264 8-bit                                                         |
| `h265`       | h.265 10-bit                                                        |
| `h265-aq`    | h.265 10-bit with adaptive quantization (crisper, better dark area detail) |
| `h265-anime` | h.265 10-bit optimized for anime content                                   |

Additional profiles can be defined in the configuration file.

> NOTE: Currently there's no color space management implemented. You need to control that yourself.

> NOTE: There's no default h264 for 10-bit encoding profile - this is intentional, h.264 isn't the best choice for 10-bit. Similarly, there's no 8-bit profile for h.265 - the codec is tuned for 10-bit, 10-bit should be used even if your source is 8-bit.

### Wildcard Support

```sh
--strategies slow+h265*          # All h265 profiles with slow preset
--strategies slow                # All profiles with slow preset
--strategies +h265-aq            # All presets with h265-aq profile
--strategies slow+h265*,veryslow+h264
```

> NOTE: Some shells require quoting for wildcards: `"slow+h265*"`

### Optimization Phase

By default, pyqenc tests all specified strategies on ~1% of chunks (minimum 3, from the middle 80% of the video), then encodes the full video using only the strategy(ies) with the smallest output size that still meets quality targets. Strategies within 5% (`Tolerance`) of the best size are also selected.

Use `--all-strategies` to skip optimization and produce one output per strategy.

---

## `measure` Subcommand

Measure quality metrics between a source and one or more encoded videos.

```sh
pyqenc measure <source_video> [<target_video> ...] [options]
```

Outputs go under `<work-dir>/measure/`:

- Per-target metrics YAML sidecar
- Per-target quality plot
- Screenshots from source and each target (default: 20, evenly distributed)

| Option             | Description                                                        | Default            |
| ------------------ | ------------------------------------------------------------------ | ------------------ |
| `--sampling N`     | For metrics measure every N-th frame (tradeoff: speed vs accuracy) | from config        |
| `--screenshots N`  | Number of screenshots per video                                    | `20`               |
| `--every INTERVAL` | Screenshot interval (e.g. `"30s"`, `"5m"`) instead of count mode   | —                  |
| `--width W`        | Scale both videos to width W before metric computation             | no scaling         |
| `--crop PARAMS`    | Crop parameters (same format as `auto`)                            | auto from job.yaml |
