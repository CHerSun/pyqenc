# Design Document: Config Settings Alignment

<!-- markdownlint-disable MD024 -->

- Created: 2026-08-12
- Completed: 2026-08-29

## Cross-Reference Notes

This spec (Created: 2026-08-12) extends `config-refactor` (Completed: 2026-07-06). All field path references in `config-refactor`'s design that use the old names are superseded by this document.

| Spec | Relationship |
|------|--------------|
| `config-refactor` | **Extended.** Authoritative for loading mechanism (`_deep_merge`, `load_app_config`, layer priority). This spec supersedes its field naming: `quality_targets` → `targets`, `max_parallel` → `concurrency`, `strategy_selection_tolerance` → `optimize_tolerance`, `metrics_sampling` → `measurement.sampling`, audio profiles flattened. The design diagrams in `config-refactor` remain valid for the loading sequence; phase access paths are updated here. |

---

## Overview

All changes in this spec are **rename and restructure only** — no new pipeline behavior is added. The goal is a coherent naming contract between three surfaces:

1. `AppConfig` Python fields
2. `default_config.yaml` keys
3. CLI argument names and `dest` attributes

The mapping rule: config YAML keys are descriptive within their section; CLI flags are the terse equivalent; Python field names match the YAML key exactly (underscores replacing hyphens).

---

## Full Field Mapping

### Before → After

| Old Python path | New Python path | Old YAML key | New YAML key | CLI flag (old) | CLI flag (new) |
|---|---|---|---|---|---|
| `encoding.quality_targets` | `encoding.targets` | `encoding.quality_targets` | `encoding.targets` | `--targets` (dest `quality_target`) | `--targets` (dest `targets`) |
| `encoding.max_parallel` | `encoding.concurrency` | `encoding.max_parallel` | `encoding.concurrency` | `--max-parallel` | `--concurrency` |
| `encoding.metrics_sampling` | `measurement.sampling` | `encoding.metrics_sampling` | `measurement.sampling` | `--sampling` | `--sampling` |
| `encoding.strategy_selection_tolerance` | `encoding.optimize_tolerance` | `encoding.strategy_selection_tolerance` | `encoding.optimize_tolerance` | *(none)* | *(none)* |
| `encoding.crop_params` | *(removed)* | *(absent)* | *(absent)* | `--crop` | `--crop` → `JobPhaseResult.crop` |
| `audio.convert_filter` | `audio.convert_pattern` | `audio.convert_filter` | `audio.convert_pattern` | `--audio-convert` → *removed* | *(config-only)* |
| `audio.profiles` | *(removed)* | `audio.profiles` | *(removed)* | `--audio-codec`, `--audio-bitrate` → *removed* | *(config-only)* |
| `audio.audio_codec` | *(removed)* | `audio.audio_codec` | *(removed)* | — | — |
| `audio.audio_base_bitrate` | *(removed)* | `audio.audio_base_bitrate` | *(removed)* | — | — |
| *(absent)* | `audio.codec` | *(absent)* | `audio.codec` | — | — |
| *(absent)* | `audio.bitrate_per_channel` | *(absent)* | `audio.bitrate_per_channel` | — | — |
| *(absent)* | `audio.extension` | *(absent)* | `audio.extension` | — | — |
| `chunking.mode` | `chunking.mode` | `chunking.mode` | `chunking.mode` | `--chunking` (dest `chunking`) | `--chunking-mode` (dest `chunking_mode`) |
| `encoding.optimize` | `encoding.optimize` | `encoding.optimize` | `encoding.optimize` | `--all-strategies` | `--no-optimize` |

**Unchanged:** `extraction.include`, `extraction.exclude`, `chunking.scene_threshold`, `chunking.min_scene_length`, `encoding.strategies`, `encoding.visual_hash`, `codecs`, `profiles`.

---

## New AppConfig Model Tree

```python
class MeasurementConfig(BaseModel):
    sampling: int               # required — no Python default; YAML is the source of truth

class EncodingConfig(BaseModel):
    targets:            list[str]   # required
    strategies:         list[str]   # required
    optimize:           bool        # required
    optimize_tolerance: float       # required; was strategy_selection_tolerance
    concurrency:        int         # required; was max_parallel
    visual_hash:        bool        # required
    # crop_params: REMOVED
    # metrics_sampling: REMOVED — moved to AppConfig.measurement

class AudioConfig(BaseModel):
    convert_pattern:     str    # required; was convert_filter
    codec:               str    # required
    bitrate_per_channel: str    # required; scaled ×channel_count at runtime
    extension:           str    # required
    # profiles, audio_codec, audio_base_bitrate: REMOVED

class AppConfig(BaseModel):
    extraction:  ExtractionConfig          # required
    chunking:    ChunkingConfig            # required
    encoding:    EncodingConfig            # required
    audio:       AudioConfig               # required
    measurement: MeasurementConfig         # required; top-level; used by encoding, merge, and measure command
    codecs:      dict[str, CodecConfig]    # required
    profiles:    dict[str, ProfileConfig]  # required
```

`ExtractionConfig` keeps its `= None` sentinels — `None` means "no filter applied", which is structural, not a default:

```python
class ExtractionConfig(BaseModel):
    include: str | None = None   # sentinel: None = no include filter
    exclude: str | None = None   # sentinel: None = no exclude filter
```

---

## AudioConfig Bitrate Scaling

The audio phase receives `config.audio.bitrate_per_channel` (e.g. `"96k"`) and must derive the final per-stream bitrate by multiplying by channel count. Channel count is read from the stream's channel layout tag embedded in the filename (existing `AUDIO_CH_*` constants).

```
"2.0"    → 2 channels  → 96k × 2 = 192k
"5.1"    → 6 channels  → 96k × 6 = 576k
"7.1"    → 8 channels  → 96k × 8 = 768k
"stereo" → 2 channels  → 96k × 2 = 192k
```

Parsing `bitrate_per_channel`: strip trailing `k` or `K`, parse as int, multiply, re-append `k`. If the string ends with `M` or `m`, treat as kbits×1000 (unlikely for audio, but defensively handled).

---

## `crop_params` Volatility

`crop_params` currently lives on `EncodingConfig` despite being a per-run override with no YAML default. After this spec it travels as a plain kwarg:

```
CLI --crop → CropParams | None
          → _build_config() → passed to _build_registry(crop_params=...)
          → _build_registry forwards to JobPhase(crop_params=...)
          → stored as JobPhaseResult.crop (already exists)
```

No `AppConfig` field. No YAML entry. All phase code that reads crop reads `self._job.result.crop`.

---

## `default_config.yaml` Structure

Target structure after revision — all sections present, comments short and inline:

```yaml
extraction:
  include: null   # regex — keep only matching streams; null = all
  exclude: null   # regex — drop matching streams; overrides include

chunking:
  mode: lossless            # "lossless" (FFV1, frame-perfect) | "remux" (stream-copy, I-frame aligned)
  scene_threshold: 0.3      # 0.0–1.0, lower = more sensitive
  min_scene_length: 24      # minimum frames per chunk

encoding:
  targets:
    - "vif-med:92.0"        # film grain retention; 92–94 is good
    - "vif-min:88.0"        # min safeguard
    - "vmaf-p05:95.0"       # p05 is more stable than median on short chunks
    - "vmaf-min:92.0"       # min safeguard
    - "psnr-med:45.0"       # 44–46 gives good quality retention
    - "psnr-min:42.0"       # min safeguard
    - "ssim-med:98.0"       # SSIM is highly non-linear; 98+ is visually good
    - "ssim-min:95.0"       # min safeguard
  strategies:
    - "veryslow+h264*"
    - "slow+h265*"
    # - "4+av1*"            # AV1: limited HW decoding support on older devices
    # - "p7+nvenc-h265-10bit-cq"  # NVENC: requires NVIDIA GPU
  optimize: true            # false = encode all strategies, skip selection
  optimize_tolerance: 5.0   # % size margin — strategies within this % of best are equivalent
  concurrency: 1            # max simultaneous ffmpeg encoding processes
  visual_hash: true         # emoji prefix on chunk log lines

measurement:
  sampling: 1               # measure every N-th frame (1 = every frame; 3 = faster, slightly less precise)
                            # applies to encoding quality checks, merge verification, and standalone measure command

audio:
  convert_pattern: "^(norm|dynaudnorm|2\\.0 (std|night|nboost)) ←"  # selects processed files for delivery conversion
  codec: aac                # ffmpeg audio codec
  bitrate_per_channel: 96k  # per-channel; runtime scaling: ×2=2.0→192k, ×6=5.1→576k, ×8=7.1→768k
  extension: .m4a           # output file extension

codecs:
  ...  # unchanged from config-refactor

profiles:
  ...  # unchanged from config-refactor
```

Principles for the revised YAML:
- No multi-paragraph block comments; replace with section header `# SECTION NAME` and inline `# ...` annotations.
- Strategy syntax guide reduced to the 6 essential pattern examples only (already present as inline examples in the `strategies:` list).
- Codec and profile entries keep their existing inline comments (they are already appropriately detailed).
- Every `AppConfig` field appears with its default value and a short description.

---

## Single Source of Truth for Defaults

The bundled YAML is the only place operational defaults live. The Python model declares structure and types; the YAML declares values.

### Pydantic field declarations

```python
# Before — duplicates the YAML:
concurrency:        int   = DEFAULT_CONCURRENCY
optimize:           bool  = True
optimize_tolerance: float = 5.0
visual_hash:        bool  = True

# After — required, YAML must supply:
concurrency:        int
optimize:           bool
optimize_tolerance: float
visual_hash:        bool
```

Nullable sentinel fields stay as-is — `None` is not a default value, it is a structural "not set":

```python
include: str | None = None   # None = no include filter; not a default the YAML would duplicate
exclude: str | None = None
```

### CLI argument defaults

```python
# Before — repeats the config value, silently overrides even when user omitted the flag:
parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)

# After — None = not provided, _build_config only overrides when explicitly set:
parser.add_argument("--concurrency", type=int, default=None)
```

Args with no config equivalent keep their own defaults:
```python
parser.add_argument("--log-level", default="info")   # no AppConfig field — fine
parser.add_argument("--work-dir",  default=LongPath("."))  # volatile, not in AppConfig — fine
```

### Constants to delete

These constants exist solely to hold a default value for a Pydantic field or CLI arg. After making fields required and CLI args `default=None`, they have no remaining callers:

| Constant | Reason for deletion |
|---|---|
| `DEFAULT_MAX_PARALLEL` / `DEFAULT_CONCURRENCY` | Only used as Pydantic default + CLI default |
| `DEFAULT_METRICS_SAMPLING` | Only used as Pydantic default + CLI default |

Constants that survive (used in logic or multi-site):

| Constant | Reason to keep |
|---|---|
| `DEFAULT_SCREENSHOT_COUNT` | Used in CLI default for `--screenshots` (no config equivalent) |
| `THRESHOLD_ATTEMPTS_WARNING` | Used in comparison logic inside encoding phase |
| All `AUDIO_CH_*`, `NORMALISED_PREFIXES`, etc. | Pattern matching logic |

### Validation behavior after change

`AppConfig.model_validate({})` raises `ValidationError` listing all missing required fields. This is the correct behavior — a misconfigured or empty YAML fails loudly at startup rather than producing a silently wrong config.

---

## Component Changes Summary

### `pyqenc/app_config.py`
- Add `MeasurementConfig(BaseModel)` with `sampling: int` — required, no Python default; add docstring
- `EncodingConfig`: rename `quality_targets` → `targets`, `max_parallel` → `concurrency`, `strategy_selection_tolerance` → `optimize_tolerance`; remove `metrics_sampling`; remove `crop_params`; **no** `measurement` sub-model field here; make all value-bearing fields required (remove Python defaults); update all docstrings
- `AppConfig`: add `measurement: MeasurementConfig` as a **top-level** required field (no `Field(default_factory=...)`); make all sub-model fields required
- `AudioConfig`: replace all fields with `convert_pattern: str`, `codec: str`, `bitrate_per_channel: str`, `extension: str` — all required; delete `AudioConversionProfile`
- Update `_inject_codec_names` validator and `resolve()` calls to use new field names

### `pyqenc/constants.py`
- Delete `DEFAULT_MAX_PARALLEL` (was the only caller was Pydantic default + CLI default, both now gone)
- Delete `DEFAULT_METRICS_SAMPLING` (same reason)
- `DEFAULT_SCREENSHOT_COUNT` and all other constants remain

### `pyqenc/cli.py`
- Rename `--chunking` → `--chunking-mode`, dest `chunking` → `chunking_mode`
- Rename `--all-strategies` → `--no-optimize`, dest `all_strategies` → `no_optimize`
- Rename `--max-parallel` → `--concurrency`, dest `max_parallel` → `concurrency`
- Rename `--targets` dest `quality_target` → `targets`
- Delete `_add_audio_convert_arguments()` and all calls to it
- In `_build_config()`: update all assignment paths to new field names; remove audio override block; update `--concurrency`, `--no-optimize`, `--chunking-mode`, `--targets` handling; update `--sampling` to assign `config.measurement.sampling`; remove `crop_params` assignment from `_build_config()` (move to command handlers that call `_build_registry`)
- In each command handler: pass `crop_params` as a keyword argument to `_build_registry` instead of assigning to config

### `pyqenc/phase.py`
- `_build_registry`: add `crop_params: CropParams | None = None` parameter; forward to `JobPhase`

### `pyqenc/phases/job.py`
- `JobPhase.__init__`: add `crop_params: CropParams | None = None` parameter; assign to `self._crop_params`
- Populate `result.crop` from `self._crop_params` (already exists on `JobPhaseResult`)

### All phases reading encoding config
- `config.encoding.quality_targets` → `config.encoding.targets`
- `config.encoding.max_parallel` → `config.encoding.concurrency`
- `config.encoding.metrics_sampling` → `config.measurement.sampling`
- `config.encoding.strategy_selection_tolerance` → `config.encoding.optimize_tolerance`
- `config.encoding.crop_params` → `self._job.result.crop`

### Audio phase
- Replace all profile lookup logic with flat field reads + bitrate scaling computation

### `pyqenc/default_config.yaml`
- Full rewrite per the structure above; all settings present; comments concise and inline

---

## Testing Strategy

- Existing `_deep_merge` and `AppConfig` property-based tests remain valid (field names in test fixtures updated).
- A new unit test verifies `AudioConfig` bitrate scaling helper produces correct output for 2.0, 5.1, 7.1 channel counts.
- A new unit test verifies `AppConfig.model_validate({})` raises `ValidationError` (required fields, no silent fallbacks).
- Smoke test: dry-run against sample video exits 0 and logs show `concurrency`, `optimize_tolerance`, `measurement.sampling` in the config display.
