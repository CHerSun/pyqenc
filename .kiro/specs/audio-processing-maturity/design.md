# Design Document — Audio Processing Maturity

<!-- markdownlint-disable MD024 -->

Created: 2026-03-18
Completed: 2026-03-18

## Overview

This document describes the technical design for maturing the audio processing phase in pyqenc. The changes span four areas:

1. **Correctness** — `.tmp` flow for all ffmpeg audio calls; removal of `ffmpeg-normalize` dependency.
2. **Naming & taxonomy** — `strategy_short` attribute on every strategy; `←` separator constant; human-readable chained filenames.
3. **Strategy graph** — well-defined `check()` conditions per strategy; natural graph termination without an `is_terminal` flag; shared 2-pass EBU R128 helper for clipping prevention and standalone normalisation.
4. **Configuration & CLI** — `audio_output` section in `default_config.yaml` (convert filter, per-channel-layout profiles); unified `-i`/`-x` stream filters for extraction; `--audio-convert`, `--audio-codec`, `--audio-bitrate` CLI options.

---

## Architecture

```
CLI (cli.py)
  │  -i/--include, -x/--exclude → ExtractionConfig
  │  --audio-convert, --audio-codec, --audio-bitrate → AudioConfig
  ▼
ConfigManager (config.py)
  │  loads audio_output section + streams section from default_config.yaml / pyqenc.yaml
  ▼
Extraction phase (phases/extraction.py)
  │  streams_filter_plain_regex() — unified include/exclude for all stream types
  │  persists include/exclude in phase sidecar; validates on next run
  ▼
Audio phase (phases/audio.py)
  │  AudioEngine.build_plan() — scans directory, applies strategy.check(), builds task graph
  │  _two_pass_loudnorm() — shared EBU R128 helper (used by norm, 2.0 std, 2.0 night, 2.0 nboost)
  │  SynchronousRunner — executes plan with alive_bar progress
  ▼
run_ffmpeg / run_ffmpeg_async (utils/ffmpeg_runner.py)
  │  .tmp-then-rename protocol enforced via output_file= argument
  ▼
Output: aac ← ... .aac files (profile-selected CBR bitrate)
```

---

## Components and Interfaces

### 2.1 Constants (`pyqenc/constants.py`)

New constants added:

```python
AUDIO_STEM_SEPARATOR = "←"
"""Separator used between strategy_short and source stem in audio output filenames."""

AUDIO_CH_71  = "ch=7.1"
AUDIO_CH_51  = "ch=5.1"
AUDIO_CH_20  = "ch=2.0"
AUDIO_CH_STEREO = "ch=stereo"
"""Channel layout tags embedded in audio filenames by the extraction phase."""
```

### 2.2 `BaseStrategy` (`phases/audio.py`)

`strategy_short` becomes a required attribute. All concrete `execute` / `execute_async` implementations MUST pass the output `Path` as `output_file` to `run_ffmpeg` / `run_ffmpeg_async` — no strategy may write directly to the final output path:

```python
class BaseStrategy(ABC):
    def __init__(self, name: str, strategy_short: str) -> None:
        self.name           = name
        self.strategy_short = strategy_short

    def output_path(self, source: Path, extension: str = "flac") -> Path:
        """Construct output path using the naming convention."""
        sep = AUDIO_STEM_SEPARATOR
        return source.parent / f"{self.strategy_short} {sep} {source.stem}.{extension}"
```

`AudioEngine.__init__` validates uniqueness of `strategy_short` across all registered strategies.

### 2.3 Strategy implementations

| Class | `strategy_short` | `check()` condition | ffmpeg passes |
|---|---|---|---|
| `DownmixStrategy71to51` | `5.1` | filename contains `AUDIO_CH_71` | 1 pass |
| `DownmixStrategy51to20Std` | `2.0 std` | filename contains `AUDIO_CH_51` | 2-pass EBU R128 |
| `DownmixStrategy51to20Night` | `2.0 night` | filename contains `AUDIO_CH_51` | 2-pass EBU R128 |
| `DownmixStrategy51to20NBoost` | `2.0 nboost` | filename contains `AUDIO_CH_51` | 2-pass EBU R128 |
| `NormStrategy` | `norm` | filename does NOT start with any normalised prefix | 2-pass EBU R128 |
| `DynaudnormStrategy` | `dynaudnorm` | filename starts with a normalised prefix | 1 pass |
| `ConversionStrategy` | `aac` | never via `check()` — applied via keep filter only | 1 pass |

Normalised prefixes (shared constant set used by both `NormStrategy.check()` and `DynaudnormStrategy.check()`):

```python
_NORMALISED_PREFIXES = (
    f"norm {AUDIO_STEM_SEPARATOR}",
    f"2.0 std {AUDIO_STEM_SEPARATOR}",
    f"2.0 night {AUDIO_STEM_SEPARATOR}",
    f"2.0 nboost {AUDIO_STEM_SEPARATOR}",
)
```

### 2.4 Shared 2-pass EBU R128 helper

```python
async def _two_pass_loudnorm(
    source:       Path,
    output:       Path,
    extra_filters: list[str] = [],   # prepended before loudnorm in the filter chain
) -> None:
    """Run 2-pass EBU R128 loudnorm normalisation, optionally with prepended filters.

    Pass 1: measure integrated loudness and true peak.
    Pass 2: apply linear normalisation (and prepended filters) → output FLAC.
    Both passes use run_ffmpeg_async with output_file= to enforce .tmp flow.
    Raises RuntimeError if pass 1 JSON cannot be parsed.
    """
```

Used by:
- `NormStrategy.execute_async()` — no extra filters
- `DownmixStrategy51to20Std.execute_async()` — standard downmix filter prepended
- `DownmixStrategy51to20Night.execute_async()` — night-mode downmix filter prepended
- `DownmixStrategy51to20NBoost.execute_async()` — nboost downmix filter prepended

Pass 1 parses the `loudnorm` JSON block from ffmpeg stderr (the runner already collects all stderr lines in `FFmpegRunResult.stderr_lines`). The JSON block appears between `[Parsed_loudnorm` lines and is extracted with a simple regex scan.

### 2.5 `AudioEngine` plan builder

The `build_plan()` method is simplified:

- No `is_terminal` flag on `Task`.
- The keep-filter regex (from config / CLI) is compiled once and tested against each candidate output filename. Any file matching the keep filter gets a `ConversionStrategy` task appended.
- `max_depth` parameter is removed; graph termination is natural because no strategy's `check()` returns `True` for a `dynaudnorm ←` prefixed file.

```python
@dataclass
class Task:
    source:   Path
    output:   Path
    strategy: BaseStrategy
    depth:    int
    failed:   bool      = False
    parent:   Task|None = None
```

### 2.6 `ConversionStrategy` — profile-aware

`ConversionStrategy` now accepts a `profiles` dict (keyed by channel layout string) and selects the appropriate codec/bitrate by scanning the source filename for a channel layout tag:

```python
@dataclass
class AudioConversionProfile:
    codec:     str   # e.g. "aac"
    bitrate:   str   # e.g. "192k"
    extension: str   # e.g. ".aac"
```

CBR mode is enforced unconditionally by passing `-b:a <bitrate>` without `-vbr` to the AAC encoder. All strategies — including `ConversionStrategy` — MUST pass the output `Path` as `output_file` to `run_ffmpeg` / `run_ffmpeg_async` to enforce the `.tmp`-then-rename protocol. No strategy may write directly to the final output path.

When `--audio-bitrate` is supplied on the CLI, it is treated as the base bitrate for 2.0 stereo. Bitrates for other channel layouts are derived by scaling proportionally to channel count relative to 2.0 (2 channels):

| Layout | Channels | Scale factor | Example (base 192k) |
|---|---|---|---|
| 2.0 | 2 | 1.0× | 192k |
| 5.1 | 6 | 3.0× | 576k |
| 7.1 | 8 | 4.0× | 768k |

The scaling is applied at runtime; the config profiles are not mutated.

### 2.7 `SynchronousRunner` / `AsyncRunner` progress display

Audio processing runs **sequentially** via `SynchronousRunner` (the `AsyncRunner` exists but is not used in the main pipeline — audio tasks have I/O-bound dependencies that make parallelism complex and the gain marginal for typical track counts).

Progress bar behaviour:
- A single `alive_bar` is created with `total = len(plan.tasks)` before execution starts.
- The bar text slot shows a running summary: `✔ {success}  ✘ {failed}  ⏭ {skipped}` — updated after each task completes. This avoids the noisy per-task text updates that would be misleading in any future parallel mode.
- Each completed task (success, failure, or skip) advances the bar by one unit.
- In dry-run mode, the task list is printed line by line (no bar) and no tasks are executed.

### 2.8 Configuration (`config.py` + `default_config.yaml`)

New `audio_output` section in `default_config.yaml`:

```yaml
audio_output:
  convert_filter: "^(norm|dynaudnorm|2\\.0 (std|night|nboost)) ←"
  profiles:
    "2.0":
      codec: aac
      bitrate: 192k
      extension: .aac
    "5.1":
      codec: aac
      bitrate: 512k
      extension: .aac
    "7.1":
      codec: aac
      bitrate: 768k
      extension: .aac
```

New `streams` section:

```yaml
streams:
  include: null   # null = include all
  exclude: null   # null = exclude none
```

`ConfigManager` gains `get_audio_output_config() -> AudioOutputConfig` and `get_stream_filter() -> StreamFilterConfig`.

### 2.9 Extraction phase changes (`phases/extraction.py`)

- Replace `video_filter` / `audio_filter` parameters with unified `include` / `exclude` applied to all stream types via the existing `streams_filter_plain_regex()`.
- Persist `include` and `exclude` values in the extraction phase sidecar.
- On subsequent runs, compare persisted vs current values; log a warning and mark phase as needing re-execution if they differ.
- Display each stream in a compact tabular format with a single symbol column — `✔` for included, `✗` for excluded — followed by the would-be output filename, vertically aligned. Example:

  ```
    ✔  #01 ID=1 (video-h264) lang=eng res=1920x1080 start=0.0.mkv
    ✔  #02 ID=2 (audio-ac3) lang=eng ch=5.1(side) start=0.028.mka
    ✗  #03 ID=3 (audio-ac3) lang=rus ch=5.1(side) start=0.028.mka
    ✔  #04 ID=4 (subtitle-subrip) lang=eng start=0.0.srt
  ```

  This display is shown in both dry-run and execute modes.

### 2.10 CLI changes (`cli.py`)

Removed arguments:
- `--video-filter`, `--audio-filter` (replaced by `-i`/`-x`)
- `--audio-keep` (renamed to `--audio-convert`)

New arguments (on `auto` and `extract` subcommands):
- `-i` / `--include` — stream include regex
- `-x` / `--exclude` — stream exclude regex

New arguments (on `auto` and `audio` subcommands):
- `--audio-convert` — override `audio_output.convert_filter`
- `--audio-codec` — override codec for all profiles in this run
- `--audio-bitrate` — set the base bitrate for 2.0 stereo; the system scales this proportionally for higher channel counts (e.g. 5.1 = base × 2.67, 7.1 = base × 4)

---

## Data Models

### `AudioOutputConfig`

```python
@dataclass
class AudioConversionProfile:
    codec:     str
    bitrate:   str
    extension: str

@dataclass
class AudioOutputConfig:
    convert_filter: str
    profiles:       dict[str, AudioConversionProfile]  # keyed by channel layout e.g. "2.0"
```

### `StreamFilterConfig`

```python
@dataclass
class StreamFilterConfig:
    include: str | None
    exclude: str | None
```

### `Task` (simplified)

```python
@dataclass
class Task:
    source:   Path
    output:   Path
    strategy: BaseStrategy
    depth:    int
    failed:   bool      = False
    parent:   Task|None = None
```

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| ffmpeg audio command fails | `.tmp` file deleted by runner; task marked `failed`; downstream tasks skipped |
| Pass 1 loudnorm JSON not parseable | `RuntimeError` raised inside strategy; task marked `failed` |
| No audio files in output directory | `AudioResult(success=False, error=...)` returned |
| Channel layout tag not found in filename for profile selection | Falls back to `2.0` profile; `logger.warning` emitted |
| Duplicate `strategy_short` at `AudioEngine` construction | `ValueError` raised immediately |
| Extraction filter change detected on subsequent run | Warning logged; phase marked as needing re-execution |

---

## Testing Strategy

- Unit test `_two_pass_loudnorm` by mocking `run_ffmpeg_async` and verifying the loudnorm JSON parsing logic with realistic stderr samples.
- Unit test each strategy's `check()` method with filenames that should and should not match.
- Unit test `AudioEngine.build_plan()` with a mock directory to verify the task graph shape matches the target processing graph.
- Unit test `ConversionStrategy` profile selection with filenames containing various channel layout tags.
- Unit test `streams_filter_plain_regex()` with include-only, exclude-only, and combined patterns.
- Integration test: run `process_audio_streams()` against a real extracted audio file (from the test MKV) and verify output filenames follow the `{strategy_short} ← {stem}` pattern.
