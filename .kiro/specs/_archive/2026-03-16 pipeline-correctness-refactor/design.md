# Design Document — Pipeline Correctness Refactor

- Created: 2026-03-16
- Completed: 2026-03-16

## Overview

This refactor addresses seven interconnected correctness and consistency issues in the pyqenc pipeline. The changes are primarily structural: consolidating duplicate types, wiring data that already exists through phases that currently ignore it, committing to a single naming convention, and centralising all magic strings/numbers into `constants.py`. No new external dependencies are introduced.

The work is scoped to `pyqenc/models.py`, `pyqenc/constants.py`, `pyqenc/progress.py`, `pyqenc/orchestrator.py`, and the six phase modules. The public API surface (`api.py`, `cli.py`) requires only minor updates to reflect renamed types.

---

## Architecture

The pipeline architecture is unchanged. The refactor operates within the existing phased pipeline:

```
Source Video
    │
    ▼
[Extraction] ──► VideoMetadata (with crop_params populated) + list[AudioMetadata]
    │
    ▼
[Chunking] ──────► list[ChunkMetadata] (timestamp-named, no crop applied)
    │
    ▼
[Optimization] ──► optimal_strategy persisted in PipelineState
    │
    ▼
[Encoding] ──────► AttemptMetadata per attempt; CRF-only filenames; tmp→rename
    │
    ▼
[Audio] ─────────► list[AudioMetadata] (unchanged flow)
    │
    ▼
[Merge] ─────────► optimal-only or all-strategies output + final metrics
```

The key architectural change is that `CropParams` now travels as a field on `VideoMetadata` (and therefore on `PipelineState.source_video`) rather than being stored separately in `PhaseMetadata`. This makes it available to every downstream phase without extra lookups.

---

## Components and Interfaces

### 1. `pyqenc/models.py` — Typed Data Model Consolidation

#### Rename `ChunkVideoMetadata` → `ChunkMetadata`

`ChunkVideoMetadata` is renamed to `ChunkMetadata`. The frame-based `start_frame` field is removed; timestamp fields replace it as the primary identifiers:

```python
class ChunkMetadata(VideoMetadata):
    chunk_id:        str
    start_timestamp: float
    end_timestamp:   float
```

`chunk_id` is derived from `_chunk_name_duration(start_timestamp, end_timestamp)` before the file is created.

The duplicate `ChunkInfo` dataclass defined in both `phases/chunking.py` and `phases/encoding.py` is removed. All callers use `ChunkMetadata`.

#### New `AudioMetadata` model

```python
class AudioMetadata(BaseModel):
    path:             Path
    codec:            str | None = None
    channels:         int | None = None
    language:         str | None = None
    title:            str | None = None
    duration_seconds: float | None = None
    start_timestamp:  float | None = None   # delay relative to video in seconds
```

`title` stores the user-provided descriptive text from the track's metadata tag (e.g. "Surround 5.1", "Commentary"). The `audio_filter` regex is matched against a display string that includes `title` alongside `language`, `codec`, and `channels` — replacing the previous approach of embedding title in the extracted filename. `start_timestamp` captures the "Delay relative to video" value (e.g. 7 ms → 0.007). `ExtractionResult.audio_files` changes from `list[Path]` to `list[AudioMetadata]`.

#### New `AttemptMetadata` model

```python
class AttemptMetadata(BaseModel):
    path:            Path
    chunk_id:        str
    strategy:        str
    crf:             float
    resolution:      str
    file_size_bytes: int
```

`AttemptMetadata` is a plain `BaseModel` rather than a subclass of `ChunkMetadata` or `VideoMetadata`. An encoded attempt is a finished artifact with all fields known at creation time — it does not need lazy ffprobe machinery, and it carries encoding-specific fields (`strategy`, `crf`) that have no meaning on a source chunk.

`AttemptMetadata` is also the unit of artifact recovery. At encoding-phase start, the strategy output directories are scanned and each matching filename is parsed via `ENCODED_ATTEMPT_NAME_PATTERN` into an `AttemptMetadata` instance. All fields are recovered from the filename and filesystem alone — no `progress.json` lookup is needed:

- `chunk_id` — parsed from the filename stem (everything before `.<WxH>.crf`)
- `resolution` — the `WxH` segment in the filename
- `crf` — the value after `crf` in the filename
- `strategy` — inferred from the parent directory name (e.g. `encoded/slow_h265-aq/`)
- `path` — the file path itself
- `file_size_bytes` — `path.stat().st_size`

**Metrics persistence for recovery**: knowing that a CRF-named file exists is not sufficient for artifact-based recovery — we also need to know whether it passed quality targets. To avoid re-running expensive metrics evaluation on every restart, each encoded attempt is accompanied by a sidecar file `<stem>.metrics.json` written atomically after metrics evaluation completes. On recovery, the sidecar is parsed to get pass/fail status and the measured metrics dict. A missing sidecar means "encode succeeded, metrics not yet measured" — the attempt is re-evaluated (not re-encoded) before the CRF search continues. A sidecar is considered valid only when it contains all metric keys required by the current quality targets; if any key is missing the sidecar is overwritten after re-evaluation.

**Sidecar validity check**: `_read_metrics_sidecar` returns the raw dict; the caller (`encode_chunk`) is responsible for checking that all keys in `{f"{t.metric}_{t.statistic}" for t in quality_targets}` are present in `sidecar["metrics"]`. If any are missing, the sidecar is treated as absent and quality evaluation is re-run.

**Reuse messaging**: whenever an attempt `.mkv` file or its sidecar is reused, the pipeline logs an info-level message (not debug) identifying the file by name and CRF value. This gives the user clear visibility into which work was recovered from a previous run, which is especially important during development when the recovery logic itself is being validated.

**Tracker cleanup**: with sidecar-based recovery in place, the `AttemptInfo` list stored under `PipelineState.chunks.<chunk_id>.strategies.<strategy>.attempts` becomes redundant — the ground truth is on disk. The `ChunkState` / `StrategyState` / `AttemptInfo` models and the `update_chunk` / `get_chunk_state` tracker methods are removed. `PipelineState.chunks` is removed. The tracker retains only phase-level state (`phases`) and chunk metadata (`chunks_metadata`). `get_successful_crf_average` is also removed; the encoding phase derives the CRF seed by scanning existing sidecar files in the strategy directory instead.

This replaces the current progress-tracker lookup in `_check_existing_encoding` — the encoded file + sidecar on disk is the ground truth, not the tracker.

`ChunkEncodingResult.encoded_file` changes from `Path | None` to `AttemptMetadata | None`.

#### `CropParams` on `VideoMetadata`

`VideoMetadata` gains a new serialisable field:

```python
class VideoMetadata(BaseModel):
    ...
    crop_params: CropParams | None = None
```

After extraction completes, `crop_params` is always a `CropParams` instance (possibly all-zero). `None` is only valid before detection has run. This field is included in `model_dump_full()` / `model_validate_full()` so it round-trips through `progress.json`.

#### `ExtractionResult` — single video

`ExtractionResult.video_files: list[VideoMetadata]` becomes `video: VideoMetadata` (single stream). The orchestrator accesses `result.video.crop_params` directly.

#### `PipelineState` — `source_video` carries `crop_params`

`PipelineState.source_video` is already a `VideoMetadata`. With `crop_params` added to that model, no structural change to `PipelineState` is needed. The `PhaseMetadata.crop_params: str | None` field is removed; crop data lives exclusively on `source_video`.

---

### 2. `pyqenc/constants.py` — Centralised Constants

All magic strings, numbers, and patterns are moved here. New additions:

```python
# Chunk naming
CHUNK_NAME_PATTERN      = re.compile(r"^(\d{2,}꞉\d{2}꞉\d{2}․\d{3})-(\d{2,}꞉\d{2}꞉\d{2}․\d{3})$")
CHUNK_GLOB_PATTERN      = "*.mkv"   # used with output_dir.glob()

# Encoded attempt naming
ENCODED_ATTEMPT_GLOB_PATTERN = "*.crf*.mkv"
ENCODED_ATTEMPT_NAME_PATTERN = re.compile(
    r"^(?P<chunk_id>.+)\.(?P<resolution>\d+x\d+)\.crf(?P<crf>[\d.]+)\.mkv$"
)

# Separator lines (already present — confirmed compliant)
LINE_WIDTH = 72
THIN_LINE  = "─" * LINE_WIDTH
THICK_LINE = "═" * LINE_WIDTH

# Status symbols (already present — confirmed compliant)
SUCCESS_SYMBOL_MINOR = "✔"
SUCCESS_SYMBOL_MAJOR = "✅"
FAILURE_SYMBOL_MINOR = "✘"
FAILURE_SYMBOL_MAJOR = "❌"
WARNING_SYMBOL       = "⚠"

# Filename characters (already present — confirmed compliant)
TIME_SEPARATOR_SAFE = "꞉"
TIME_SEPARATOR_MS   = "․"
RANGE_SEPARATOR     = "-"
BRACKET_LEFT        = "｟"
BRACKET_RIGHT       = " ｠"

# Temp suffix (already present)
TEMP_SUFFIX = ".tmp"
```

No module outside `constants.py` may define these inline. The `_CHUNK_NAME_PATTERN` local variable in `chunking.py` is removed and replaced with the import.

---

### 3. `pyqenc/phases/extraction.py` — CropParams and AudioMetadata

**`_detect_crop_parameters`** return type changes: it now always returns a `CropParams` instance. When no borders are found it returns `CropParams()` (all zeros) instead of `None`.

**`extract_streams`** signature and return type:

```python
def extract_streams(
    source_video: Path,
    output_dir:   Path,
    video_filter: str | None = None,
    audio_filter: str | None = None,
    detect_crop:  bool = True,
    manual_crop:  str | None = None,
    force:        bool = False,
    dry_run:      bool = False,
) -> ExtractionResult: ...
```

`ExtractionResult` is updated:

```python
@dataclass
class ExtractionResult:
    video:   VideoMetadata        # single stream; crop_params populated
    audio:   list[AudioMetadata]
    outcome: PhaseOutcome
    error:   str | None = None
```

After extraction, `video.crop_params` is always set (never `None`). The orchestrator stores `result.video` as `state.source_video` and no longer writes crop data into `PhaseMetadata`.

---

### 4. `pyqenc/phases/chunking.py` — Timestamp Naming and `ChunkMetadata`

**Dead code removal**: `_chunk_name` (frame-based) is deleted. Only `_chunk_name_duration` is used.

**Stateless path removed**: `_chunk_video_stateless` and the `tracker: ProgressTracker | None` optional are removed. The tracker is now a required parameter. `chunk_video` delegates directly to `_chunk_video_tracked` (which may be inlined). Callers that previously omitted the tracker must now provide one.

**`_CHUNK_NAME_PATTERN`** local variable is removed; `CHUNK_NAME_PATTERN` from `constants.py` is imported.

**`split_chunks_from_state`** populates `ChunkMetadata` with `start_timestamp` and `end_timestamp`:

```python
chunk_meta = ChunkMetadata(
    path=chunk_file,
    chunk_id=stem,
    start_timestamp=start_ts,
    end_timestamp=end_ts,
)
chunk_meta.populate_from_ffmpeg_output(proc.stderr.splitlines())
```

The `populate_from_ffmpeg_output` call replaces the separate `get_frame_count` probe for newly-split chunks, satisfying Req 2.8.

**Resume detection** uses `CHUNK_NAME_PATTERN` to match existing file stems instead of the old frame-based pattern.

**`ChunkingResult.chunks`** type changes from `list[ChunkInfo]` to `list[ChunkMetadata]`.

---

### 5. `pyqenc/phases/encoding.py` — Attempt Naming and AttemptMetadata

**`ChunkInfo` dataclass is removed.** All encoding functions accept `list[ChunkMetadata]`.

**Attempt filename pattern** (Req 4.1):

```
<chunk_id>.<width>x<height>.crf<CRF>.mkv
```

No attempt number. Stored under `work_dir/encoded/<safe_strategy>/`.

**Temp-file protocol** (Req 4.2):

```python
tmp_path   = final_path.with_suffix(TEMP_SUFFIX)   # e.g. chunk.1920x800.crf18.0.tmp
# encode with: ffmpeg ... -f matroska <tmp_path>
# on success:
tmp_path.replace(final_path)
```

`-f matroska` is added to the ffmpeg command so the container format is explicit and does not depend on the file extension. This ensures `.tmp` files are never matched by `ENCODED_ATTEMPT_GLOB_PATTERN` (`*.crf*.mkv`).

**Existence check** (Req 4.3–4.4): `_check_existing_encoding` scans the strategy directory for a file matching `ENCODED_ATTEMPT_NAME_PATTERN` with the correct `chunk_id`, `resolution`, and `crf`. No progress-tracker lookup needed for this check.

**Stale `.tmp` cleanup** (Req 4.5): at the start of `encode_all_chunks`, scan all strategy directories for `*.tmp` files (matching `TEMP_SUFFIX`), log a warning for each, and delete them.

**`ChunkEncodingResult.encoded_file`** changes to `AttemptMetadata | None`.

**CropParams injection** (Req 2.4): `ChunkEncoder._encode_with_ffmpeg` already injects the crop filter when `self._crop_params` is set and non-empty. No change needed here beyond ensuring the orchestrator passes `state.source_video.crop_params`.

**QualityEvaluator crop reference** (Req 2.5): `evaluate_chunk` is called with `crop_reference=self._crop_params` so the reference chunk is cropped to the same dimensions as the encoded output before comparison.

---

### 6. `pyqenc/orchestrator.py` — CropParams Flow and Merge Selection

**`_resolve_crop_params`** is replaced by a direct read:

```python
def _get_crop_params(self) -> CropParams | None:
    state = self.tracker._state
    if state is None:
        return None
    return state.source_video.crop_params
```

**`_execute_extraction`**: after `extract_streams` returns, the orchestrator sets `state.source_video = result.video` (which carries `crop_params`) and saves state. It no longer writes `PhaseMetadata(crop_params=...)`. Audio files are passed downstream as `result.audio`.

**`_execute_encoding` and `_execute_optimization`**: both call `self._get_crop_params()` and pass the result to `encode_all_chunks` / `find_optimal_strategy`.

**`_execute_merge`** (Req 5.1–5.2): the orchestrator passes `optimal_strategy=self._optimal_strategy` to `merge_final_video`. When `self._optimal_strategy` is set, the merge phase processes only that strategy. When it is `None` (optimization disabled), all strategies are merged.

**CropParams change detection** (Req 2.7): when loading state, if `state.source_video.crop_params` differs from the crop params that would be detected/applied now, log a warning, clear `state.phases["chunking"].metadata.scene_boundaries`, and mark all chunk encoding states as `NOT_STARTED`.

**Work-directory binding** (Req 8): on `load_state`, compare `state.source_video` filename, `file_size_bytes`, and `resolution` against the current `config.source_video`. Behaviour depends on mode:

- dry-run + mismatch → log warning, return without further action
- execute + mismatch, no `--force` → log critical, abort
- execute + `--force` → log warning, delete all artifacts, reset state, continue

---

### 7. `pyqenc/phases/merge.py` — Merge Correctness and Final Metrics

**`merge_final_video` signature** gains `optimal_strategy: str | None`:

```python
def merge_final_video(
    encoded_chunks:     dict[str, dict[str, AttemptMetadata]],
    audio:              list[AudioMetadata],
    output_dir:         Path,
    source_video:       VideoMetadata | None = None,
    quality_targets:    list[QualityTarget] | None = None,
    source_frame_count: int | None = None,
    optimal_strategy:   str | None = None,
    subsample_factor:   int = 10,
    verify_frames:      bool = True,
    measure_quality:    bool = True,
    force:              bool = False,
    dry_run:            bool = False,
) -> MergeResult: ...
```

**Strategy selection** (Req 5.1–5.2):

```python
if optimal_strategy:
    strategies_to_merge = {optimal_strategy}
else:
    strategies_to_merge = set(all_available_strategies)
```

**Chunk ordering** (Req 5.3): chunks are sorted by filename (which encodes start timestamp lexicographically via `TIME_SEPARATOR_SAFE`).

**Frame count verification** (Req 5.4–5.5): log chunk count and total frame count at info level before each merge. If total differs from source, log a warning and continue (do not abort).

**Final metrics** (Req 6.1–6.5): after a successful merge, call `QualityEvaluator.evaluate_chunk` with `crop_reference=source_video.crop_params`. Save output to `final_metrics_<safe_strategy>/` inside `output_dir`. Log a summary at info level. Metrics failure is non-fatal (log warning, continue).

---

### 8. `pyqenc/progress.py` — State Serialisation

**`_serialize_state`**: `source_video` is serialized via `model_dump_full()` which now includes `crop_params` (a Pydantic model, serialized as a nested dict). `PhaseMetadata.crop_params` field is removed from serialisation.

**`_deserialize_state`**: `source_video` is restored via `model_validate_full()`. `crop_params` is reconstructed as a `CropParams` instance from the nested dict.

**`chunks_metadata`**: renamed to `chunks` in `PipelineState`. Keys and values use `ChunkMetadata` (renamed from `ChunkVideoMetadata`). Serialisation/deserialisation updated accordingly.

**`update_chunk_metadata`** renamed to `update_chunk` and accepts `ChunkMetadata`.

**`get_chunk_metadata`** renamed to `get_chunk` and returns `ChunkMetadata | None`.

---

### 9. Quality Metrics Progress Bar (Req 9)

`QualityEvaluator.evaluate_chunk` is updated to display an `alive_bar` progress bar during ffmpeg metric subprocess execution:

- Bar total is set to `num_metric_passes * duration_seconds`, where `num_metric_passes` is the number of separate ffmpeg invocations (or filter passes) used to compute all metrics. If all metrics are computed in a single ffmpeg command, `num_metric_passes = 1` and the bar advances 0 → `duration_seconds` once. If run as separate commands, each command contributes one `duration_seconds` segment to the total.
- ffmpeg is launched with `-progress pipe:1`; an async reader advances the bar based on the `out_time_ms` field reported by ffmpeg.
- stderr is drained asynchronously by a shared `_drain_stderr` coroutine (see below). On process exit the bar is forced to completion.
- If `duration_seconds` is unavailable, an indeterminate spinner is shown instead.

**Shared `_drain_stderr` coroutine**: ffmpeg writes status updates to stderr using `\r` (carriage return), not `\n`. A `readline()`-based reader will block indefinitely waiting for a newline that never comes. The shared coroutine reads stderr in raw chunks, splits on both `\r` and `\n`, and maintains a rolling buffer of the last `STDERR_TAIL_LINES` lines (defined in `constants.py`). On process exit the buffer is available for error logging. This coroutine is used for all ffmpeg subprocess calls in the pipeline — not just metrics — to prevent pipe blocking. Duration-based progress tracking (advancing an `alive_bar` via `out_time_ms`) is only applied during final metrics evaluation; other ffmpeg calls use item-based progress as they do today.

This is an async change within `evaluate_chunk`; the existing `asyncio.run()` call site in the orchestrator is unaffected.

---

## Data Models

### Updated `VideoMetadata`

```
VideoMetadata
├── path:        Path
├── crop_params: CropParams | None = None   ← NEW (serialisable)
└── (lazy private fields: _duration_seconds, _frame_count, _fps, _resolution, _pix_fmt, _file_size_bytes)
```

Note: `start_frame` is removed from `VideoMetadata`. Positional information is carried by `ChunkMetadata.start_timestamp` / `end_timestamp` for chunks.

### `ChunkMetadata` (renamed from `ChunkVideoMetadata`)

```
ChunkMetadata(VideoMetadata)
├── chunk_id:        str    # = _chunk_name_duration(start_timestamp, end_timestamp)
├── start_timestamp: float
└── end_timestamp:   float
```

Note: `start_frame` (previously on `ChunkVideoMetadata`) is removed. Timestamps are the sole positional identifiers.

### `AudioMetadata` (new)

```
AudioMetadata
├── path:             Path
├── codec:            str | None
├── channels:         int | None
├── language:         str | None
├── title:            str | None
├── duration_seconds: float | None
└── start_timestamp:  float | None   # delay relative to video in seconds (e.g. 0.007)
```

### `AttemptMetadata` (new)

```
AttemptMetadata
├── path:            Path
├── chunk_id:        str
├── strategy:        str
├── crf:             float
├── resolution:      str
└── file_size_bytes: int
```

### `PipelineState` (updated structure)

```
PipelineState
├── version:       str
├── source_video:  VideoMetadata   # crop_params now lives here
├── current_phase: str             # last active phase; used for display/logging only
├── phases:        dict[str, PhaseState]
└── chunks:        dict[str, ChunkMetadata]   # renamed from chunks_metadata
```

`PipelineState.chunks` (previously `chunks_metadata`) stores per-chunk metadata keyed by `chunk_id`. The old `chunks` field (per-chunk attempt history) is removed — attempt history is now on disk as sidecar files. The `ChunkState`, `StrategyState`, `AttemptInfo`, `ChunkUpdate` models and the `update_chunk`, `get_chunk_state`, `get_successful_crf_average` tracker methods are removed.

### Phase Outcome Enum

All phase result types (`ExtractionResult`, `ChunkingResult`, `MergeResult`, `EncodingResult`, `PhaseResult`) replace the `success: bool`, `reused: bool`, `needs_work: bool` triple with a single `outcome: PhaseOutcome` field:

```python
class PhaseOutcome(Enum):
    COMPLETED = "completed"   # phase did real work and succeeded
    REUSED    = "reused"      # all artifacts existed; no work performed (valid in both modes)
    DRY_RUN   = "dry_run"     # dry-run mode; work would be needed; pipeline stops here
    FAILED    = "failed"      # phase failed (error field populated)
```

`REUSED` applies in both dry-run and execute mode — it means "no work needed regardless of mode". `DRY_RUN` is only set when the phase is in dry-run mode and detected that work would be required. The orchestrator stops at the first `DRY_RUN` outcome and continues through `REUSED` outcomes.

### `ExtractionResult` (updated)

```
ExtractionResult
├── video:   VideoMetadata        # single stream; crop_params always set after extraction
├── audio:   list[AudioMetadata]
├── outcome: PhaseOutcome
└── error:   str | None
```

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| Crop detection fails | `CropParams()` (all zeros) returned; warning logged |
| Stale `.tmp` found at encoding start | Warning logged; file deleted |
| Encoded attempt file already exists (CRF match) | Reused; info-level log with filename and CRF |
| Encoded attempt sidecar exists and valid | Reused without re-evaluating metrics; info-level log |
| Encoded attempt file exists, sidecar missing | Re-evaluate metrics, write sidecar; do not re-encode |
| Encoded attempt sidecar missing required metric keys | Re-evaluate metrics, overwrite sidecar |
| Frame count mismatch before merge | Warning logged; merge continues |
| Final metrics evaluation fails | Warning logged; merge result still success |
| Work-dir source mismatch (execute, no `--force`) | Critical log; pipeline aborts |
| Work-dir source mismatch (execute, `--force`) | Warning logged; artifacts deleted; pipeline continues |
| Work-dir source mismatch (dry-run) | Warning logged; no state modified |
| CropParams changed between runs | Warning logged; scene boundaries cleared; chunks reset to NOT_STARTED |

---

## Testing Strategy

Unit tests focus on the pure logic that is easiest to break during this refactor:

- `ChunkMetadata` serialisation round-trip (including `start_timestamp`, `end_timestamp`, `crop_params`).
- `_chunk_name_duration` produces names that match `CHUNK_NAME_PATTERN`.
- `ENCODED_ATTEMPT_NAME_PATTERN` correctly parses filenames produced by the new naming scheme.
- `CropParams.to_ffmpeg_filter()` output for zero and non-zero values.
- Work-directory source-file mismatch detection logic (all three modes: dry-run, execute, force).
- Merge strategy selection: optimal-only vs all-strategies.

Integration tests (existing) cover the full pipeline flow and are expected to pass without modification once the refactor is complete.

---

## Design Decisions and Rationale

**`crop_params` on `VideoMetadata` rather than `PhaseMetadata`**
Storing crop on the video metadata object means every phase that receives a `VideoMetadata` automatically has access to crop without an extra tracker lookup. It also survives serialisation naturally via `model_dump_full`. The previous approach (string in `PhaseMetadata`) required a parse step and was easy to miss.

**`ChunkMetadata` replaces both `ChunkVideoMetadata` and `ChunkInfo`**
Two separate types for the same concept caused divergence. `ChunkInfo` was a plain dataclass with no serialisation; `ChunkVideoMetadata` was a Pydantic model with lazy probing. Unifying them eliminates the conversion step in the orchestrator and encoding phase.

**Attempt files named by CRF only (no attempt number)**
The attempt number is an implementation detail of the search algorithm, not a property of the artifact. A file named `chunk.crf18.0.mkv` unambiguously represents a complete encode at CRF 18.0. Presence on disk is sufficient proof of completion; no tracker lookup is needed. This also makes manual inspection and cleanup straightforward.

**Temp-file rename protocol**
Writing to `<stem>.tmp` (with `-f matroska` to force MKV container) and renaming only on success means a crash during encoding never leaves a file that looks complete. The cleanup sweep at encoding-phase start removes any leftovers from previous crashes.

**Merge selects optimal strategy when available**
When optimization ran, merging all strategies would produce output files the user did not ask for and waste disk space. The `optimal_strategy` field already exists in `PipelineState`; the merge phase just needs to respect it.

**`AudioMetadata` instead of bare `Path`**
Audio files carry codec, channel, and language metadata that is needed for muxing decisions and display. Wrapping them in a typed model makes this information available without re-probing.

**`CHUNK_NAME_PATTERN` and `CHUNK_GLOB_PATTERN` in `constants.py`**
These patterns are used in at least three places (chunking resume, encoding resume, merge ordering). Centralising them ensures a single change propagates everywhere and prevents the pattern drift that currently exists between the frame-based and timestamp-based naming.
