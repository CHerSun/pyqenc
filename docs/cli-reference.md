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
pyqenc chunk   <source_video> [options]   # Detect scene chunking only
pyqenc encode  <source_video> [options]   # Encode chunks only
pyqenc audio   <source_video> [options]   # Process audio only
pyqenc merge   <source_video> [options]   # Merge final output only
```

Use `pyqenc --help` or `pyqenc <command> --help` for full argument lists.

Only `auto`, `measure` and `config` subcommands are intended for normal usage.

It is NOT recommended to use phase-specific subcommands (`extract`, `chunk`, `encode`, `audio`, `merge`) unless you know what you're doing.

---

## Base Options

Applies to all subcommands.

| Option              | Description                                           | Default |
| ------------------- | ----------------------------------------------------- | ------- |
| `--work-dir PATH`   | Working directory for intermediate files and state    | `.`     |
| `--log-level LEVEL` | Logging level: `debug`, `info`, `warning`, `critical` | `info`  |

## Execution Options

Applies to all pipeline subcommands (`auto`, `extract`, `chunk`, `encode`, `audio`, `merge`).

| Option          | Description                                                                                                                                       | Default |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `-y, --execute` | Actually execute phases (omit = dry-run preview)                                                                                                  | dry-run |
| `--force`       | On source-file mismatch or unrecoverable config changes in execute mode: delete all intermediate artifacts and reset state to start anew          | off     |
| `--cleanup`     | Delete workspace files per artifact after completion. `--cleanup all`: also remove remaining intermediate directories after full pipeline success | off     |
| `--no-metrics`  | Suppress process metrics.yaml output (pipeline run stats). Does not affect quality metrics measurements.                                          | off     |

---

## `auto` Options

### Quality & Strategy

| Option                    | Description                                                                                                                                                                    | Default                            |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------- |
| `--targets TARGETS`       | Quality targets (see [Quality Target Format](#quality-target-format))                                                                                                          | from config (8 targets, see below) |
| `--strategies STRATEGIES` | Encoding strategies (see [Strategy Format](#strategy-format))                                                                                                                  | from config (`h264*,h265*`)        |
| `--no-optimize`           | Disable optimization — produce output for all strategies                                                                                                                       | `False` (optimization enabled)     |
| `--concurrency N`         | Maximum concurrent encoding processes. Increase only if you see CPU cores underutilized.                                                                                       | from config (`1`)                  |
| `--sampling N`            | Frame sampling for quality measurement: every N-th frame. Lower = more precise but slower. Measurements precision directly affect encoding results. sampling=1 is recommended. | from config (`1`)                  |
| `--no-visual-hash`        | Disable emoji visual-hash prefix on chunk log lines                                                                                                                            | off                                |

### Cropping

| Option            | Description                                                                            | Default     |
| ----------------- | -------------------------------------------------------------------------------------- | ----------- |
| `--crop "VALUES"` | Manual crop: `"top,bottom"` or `"top,bottom,left,right"`. Use `"0,0"` for no cropping. | Auto-detect |

Automatic crop detection uses ffmpeg's `cropdetect` filter. The same crop parameters are applied consistently across all phases. Source remains untouched.

> NOTE: ⚠ Change of cropping value midway requires full re-encode.

### Stream Filtering

Applied during extraction phase, use dry-run to preview.

| Option             | Description                  | Example            | Default      |
| ------------------ | ---------------------------- | ------------------ | ------------ |
| `--include REGEX`  | Regex pattern to include streams | `"\b(RUS\|ENG)\b"` | Include all  |
| `--exclude REGEX`  | Regex pattern to exclude streams | `"comment"`     | Exclude none |

> Audio processing (filters, chains, track selection) is configured under `audio:` in your config file, not via CLI flags. See the [Audio Processing Guide](./audio-processing.md).

### Chunking

Chunks are timestamp windows computed directly from the source — no intermediate copy or lossless re-encode is produced.

| Option                    | Description                                                                 | Default              |
| ------------------------- | --------------------------------------------------------------------------- | -------------------- |
| `--scene-threshold VALUE` | Scene detection sensitivity, 0.0–255.0. Lower = more sensitive (more cuts). | from config (`27.0`) |
| `--min-scene-length N`    | Minimum frames per chunk — merges short scenes into one chunk.              | from config (`24`)   |

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

| Statistic       | Description                                                     |
| --------------- | --------------------------------------------------------------- |
| `min`           | Minimum across all frames — avoid, sensitive to outliers        |
| `p05`           | 5th percentile                                                  |
| `p10`           | 10th percentile — **recommended over `min`**                    |
| `p25`           | 25th percentile                                                 |
| `med`, `median` | Median (50th percentile)                                        |
| `p75`           | 75th percentile                                                 |
| `p90`           | 90th percentile                                                 |
| `p95`           | 95th percentile                                                 |
| `max`           | Maximum across all frames — avoid, often useless for measuring. |

> NOTE: `vmaf-min` is unreliable due to a first-frame bias (VMAF lacks motion context on frame 0). Use `vmaf-p10` instead, it should be more stable. See [quality-targeting.md](quality-targeting.md) for a detailed discussion.

### Examples

```sh
# Override with a single metric
--targets vmaf-p05:95

# Multiple targets (any failing target forces higher quality)
--targets vif-med:92,vmaf-p05:95,psnr-med:45,ssim-med:98

# Higher quality targets for near-lossless archival
--targets vif-med:94,vmaf-p05:97,psnr-med:48,ssim-med:99
```

When omitted, the config defaults apply (`vmaf-med:96, psnr-med:45, ssim-med:98, vif-med:93` plus `min` floor safeguards at `vmaf-min:93, psnr-min:43, ssim-min:96, vif-min:90`).

---

## Strategy Format

**Format:** `profile[+preset][,profile[+preset],...]`

A strategy combines a profile (the encoding configuration) with a preset (the encoder speed/quality tradeoff). The profile part is required; the preset part is optional — omit it to use each codec's configured `default_preset`.

Strategy names in logs and on disk are composed in the same way - `profile+preset`.

### Profiles

Profiles define the codec and optional extra encoder tuning. Each profile references a codec and may add extra ffmpeg arguments.

**Built-in profiles:**

| Profile                  | Codec        | Description                                                              |
| ------------------------ | ------------ | ------------------------------------------------------------------------ |
| `h264`                   | h264-8bit    | h.264 8-bit, no extra tuning                                             |
| `h265`                   | h265-10bit   | h.265 10-bit, no extra tuning                                            |
| `h265-aq`                | h265-10bit   | h.265 10-bit with adaptive quantization (crisper, better dark area detail) |
| `h265-anime`             | h265-10bit   | h.265 10-bit optimized for anime (crisp edges, reduced blocking)         |
| `nvenc-h265-10bit-cq`    | nvenc CQ     | HEVC NVENC GPU encoding — CQ mode. Requires NVIDIA GPU.                  |
| `nvenc-h265-10bit-vbr`   | nvenc VBR    | HEVC NVENC GPU encoding — multipass VBR mode. Not recommended.           |
| `vulkan-h265-10bit-qp`   | vulkan QP    | HEVC Vulkan GPU encoding — QP mode. Strongly not recommended.            |
| `av1`                    | av1-10bit    | AV1 10-bit, no extra tuning                                              |
| `av1-grain`              | av1-10bit    | AV1 10-bit tuned to preserve original grain                              |
| `fgs-av1-light/medium/high` | av1-10bit | AV1 10-bit with Film Grain Synthesis at varying strength                 |

Additional profiles can be defined in the configuration file. Profile names must not contain `+`.

> NOTE: There is no default h264 10-bit profile — h.264 is not well-suited for 10-bit. Similarly, there is no h.265 8-bit profile — h.265 is tuned for 10-bit and should be used even with an 8-bit source.

> NOTE: Currently there's no color space management implemented. You need to control that yourself.

### Presets

Presets control encoder speed vs quality tradeoff and are defined per codec. Each codec declares a `default_preset` used when the preset is omitted from a strategy pattern.

**h.264 / h.265:** `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium`, `slow`, `slower`, `veryslow`, `placebo`
- h.264 default: `veryslow` (affordable on modern CPUs, good benefit)
- h.265 default: `slow` (veryslow is too slow for most use cases)

**nvenc (GPU):** `p1`…`p7` — default: `p7` (highest quality; speed difference between presets is negligible on modern GPUs)

**AV1 (SVT-AV1):** `0`…`13` — default: `3` (good speed/quality balance; `2`+ recommended for grain retention profiles)

### Pattern Syntax

```sh
--strategies h265-aq               # h265-aq with its default preset (slow)
--strategies h265*                 # all h265* profiles with their default presets
--strategies h265-aq+slow          # h265-aq with explicit slow preset
--strategies h265*+slow            # all h265* profiles with slow preset
--strategies h265*+*               # all h265* profiles with all their presets
--strategies "*"                   # all profiles with their default presets
--strategies "*+*"                 # all profiles with all presets
--strategies h265-aq,h264          # multiple patterns, comma-separated
```

> NOTE: Some shells require quoting for wildcards and asterisks: `"h265*"`, `"*"`, `"*+*"`

### Optimization Phase

By default, pyqenc tests all specified strategies on ~1% of chunks (minimum 3, from the middle 80% of the video), then encodes the full video using only the strategy(ies) with the smallest output size that still meets quality targets. Strategies within 5% (`optimize_tolerance`) of the best size are also selected.

Use `--no-optimize` to skip optimization and produce one output per strategy.

---

## `measure` Subcommand

Measure quality metrics between a source and one or more encoded videos.

```sh
pyqenc measure <source_video> [<target_video> ...] [options]
```

> NOTE: ORDER MATTERS — swapping source and target produces incorrect metrics (VMAF is not symmetric).

With no target videos the command runs in screenshots-only mode (no metric computation).

Outputs go under `<work-dir>/measure/`:

- Per-target metrics YAML sidecar
- Per-target quality plot
- Screenshots from source and each target

| Option             | Description                                                                  | Default              |
| ------------------ | ---------------------------------------------------------------------------- | -------------------- |
| `--sampling N`     | For metrics measure every N-th frame (tradeoff: speed vs accuracy)           | from config          |
| `--screenshots N`  | Number of screenshots per video (caps the total in interval mode)            | `20`                 |
| `--every INTERVAL` | Screenshot interval (e.g. `"30s"`, `"5m"`) instead of count mode             | —                    |
| `--include-edges`  | Include frame 0 and the last frame in screenshot positions (count mode only) | off                  |
| `--width W`        | Scale both videos to width W before metric computation                       | no scaling           |
| `--crop PARAMS`    | Crop parameters (same format as `auto`)                                      | auto from probe.yaml |
