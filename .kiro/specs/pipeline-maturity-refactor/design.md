# Design Document — Pipeline Maturity Refactor

- Created: 2026-03-15
- Completed: 2026-03-15

## Overview

This refactor matures five interconnected areas of the pyqenc codebase without changing external behavior:

1. **Unified `VideoMetadata` / `ChunkVideoMetadata` classes** — replace the two separate `SourceVideoMetadata` / `ChunkMetadata` dataclasses and bare `Path` usage with a single lazy-loading Pydantic model hierarchy.
2. **Typed `ProgressTracker` updates** — introduce `PhaseUpdate` and `ChunkUpdate` Pydantic models so every state mutation is self-describing.
3. **Crash-safe `ProgressTracker` flushing** — register signal handlers, `atexit`, and a top-level exception guard so in-memory state is always written before the process dies.
4. **Two-phase chunking** — split scene detection and chunk splitting into independently resumable sub-phases, each persisted in `PipelineState`.
5. **Legacy module removal** — migrate the four legacy sub-packages into the main codebase and delete `pyqenc/legacy/`.

Additionally, chunk file naming is changed to frame-range identifiers for stability across restarts, and `pydantic` is adopted as the serialization and validation layer for all models.

---

## Architecture

The changes are confined to the following files; no new top-level modules are introduced:

```log
pyqenc/
  models.py          <- VideoMetadata, ChunkVideoMetadata, PhaseUpdate, ChunkUpdate (all Pydantic)
  progress.py        <- typed update methods, signal/atexit flush registration
  phases/
    chunking.py      <- detect_scenes_to_state(), split_chunks_from_state()
    extraction.py    <- MKVTrackExtractor logic inlined (legacy removed)
    audio.py         <- audio strategy logic inlined (legacy removed)
  quality.py         <- metric running logic inlined (legacy removed)
  utils/
    visualization.py <- NEW — metrics plotting logic migrated from legacy
    ffmpeg.py        <- opportunistic metadata population helpers
pyqenc/legacy/        <- DELETED after migration
```

`pydantic` is added to `pyproject.toml` as a runtime dependency.

---

## Components and Interfaces

### 1. VideoMetadata and ChunkVideoMetadata (`pyqenc/models.py`)

All models are converted from `@dataclass` to Pydantic `BaseModel`, giving free JSON serialization/deserialization, field validation, and Path ↔ string coercion.

```python
from pydantic import BaseModel, PrivateAttr

class VideoMetadata(BaseModel):
    path: Path
    start_frame: int = 0          # 0 for source; chunk offset for chunks

    # Probe-derived backing fields — None until populated
    _duration_seconds: float | None = PrivateAttr(default=None)
    _frame_count: int | None        = PrivateAttr(default=None)
    _fps: float | None              = PrivateAttr(default=None)
    _resolution: str | None         = PrivateAttr(default=None)

    @property
    def duration_seconds(self) -> float | None: ...  # lazy, probes once

    @property
    def frame_count(self) -> int | None: ...

    @property
    def fps(self) -> float | None: ...

    @property
    def resolution(self) -> str | None: ...

    def populate_from_ffprobe(self, data: dict) -> None:
        """Fill any None backing fields from a pre-parsed ffprobe JSON dict."""

    def populate_from_ffmpeg_output(self, stderr_lines: list[str]) -> None:
        """Fill any None backing fields by parsing ffmpeg stderr output."""

    def model_dump_full(self) -> dict:
        """Serialize including cached private fields for round-trip persistence."""

class ChunkVideoMetadata(VideoMetadata):
    chunk_id: str   # e.g. "chunk.000000-000319"
```

**Lazy-loading strategy**: each property checks its `PrivateAttr` backing field. On `None`, it calls the appropriate probe method:

- `duration_seconds`, `fps`, `resolution` are fetched together via a fast `ffprobe -show_streams -show_format` call (~175ms). All three are populated in one call.
- `frame_count` is fetched via `ffmpeg -c copy -f null` (~2-3s for a typical file). This run also parses duration/fps/resolution from stderr as a bonus, so if those fields are still `None` they get filled too.

This means at most two probe calls per file, and only when the data is actually needed.

**Opportunistic population**: helpers in `ffmpeg.py` that already run ffprobe or ffmpeg (e.g. `get_frame_count`) call `video_meta.populate_from_ffprobe(data)` or `populate_from_ffmpeg_output(lines)` to fill the instance before returning, avoiding a second probe.

**Serialization**: `ProgressTracker` calls `model_dump_full()` (which includes the cached private fields) and deserializes back into a `VideoMetadata` with all backing fields pre-filled (bypassing lazy-load). Pydantic handles Path ↔ string conversion automatically.

**Validation on resume**: when loading an existing `progress.json`, the tracker compares persisted fields against a freshly-constructed `VideoMetadata` for the source file. Any mismatch triggers a `logger.warning`.

All other models in `models.py` (`PhaseState`, `ChunkState`, `StrategyState`, `AttemptInfo`, `PipelineState`, `PhaseUpdate`, `ChunkUpdate`, `QualityTarget`, `CodecConfig`, `StrategyConfig`, `CropParams`) are also converted to Pydantic `BaseModel`, replacing `@dataclass`. `dict[str, Any]` fields are replaced with explicit typed Pydantic models (`PhaseMetadata`, `SceneBoundary`, etc.) wherever the shape is known. This gives automatic validation and consistent `.model_dump()` / `.model_validate()` for the entire state tree.

---

### 2. Typed Update Objects (`pyqenc/models.py`)

```python
class PhaseUpdate(BaseModel):
    phase: str
    status: PhaseStatus
    metadata: PhaseMetadata | None = None

class ChunkUpdate(BaseModel):
    chunk_id: str
    strategy: str
    attempt: AttemptInfo
```

`PhaseMetadata` is a typed Pydantic model that covers all known metadata shapes across phases. For the chunking phase it carries `scene_boundaries: list[SceneBoundary]`; for extraction it carries `crop_params: str | None`. Unknown phases can extend it or use a subclass. This eliminates `dict[str, Any]` from the update path entirely.

---

### 3. Crash-safe ProgressTracker (`pyqenc/progress.py`)

```log
ProgressTracker.__init__
  └─ _register_flush_handlers()
       ├─ atexit.register(self.flush)
       ├─ signal.signal(SIGINT,  _signal_flush)
       └─ signal.signal(SIGTERM, _signal_flush)
           (Windows: also handles CTRL_C_EVENT / CTRL_BREAK_EVENT via signal module)
```

The top-level `cli.py` entry point wraps the pipeline call in a `try/except BaseException` that calls `tracker.flush()` in a `finally` block, covering unhandled exceptions that bypass signal handlers.

`flush()` is idempotent: if `_pending_updates == 0` it returns immediately without writing to disk.

---

### 4. Two-phase Chunking (`pyqenc/phases/chunking.py`)

#### SceneBoundary model

```python
class SceneBoundary(BaseModel):
    frame: int
    timestamp_seconds: float
```

#### Sub-phase 1 — `detect_scenes_to_state`

```python
def detect_scenes_to_state(
    video_meta: VideoMetadata,
    tracker: ProgressTracker,
    scene_threshold: float = 27.0,
    min_scene_length: int = 15,
) -> list[SceneBoundary]:
```

Uses PySceneDetect `ContentDetector`. On completion the list is serialized into `PipelineState.phases["chunking"].metadata["scene_boundaries"]` and flushed immediately.

If zero scenes are detected, a single boundary at frame 0 / t=0 is stored (treating the whole video as one chunk), and a warning is logged.

> **Reference**: if PySceneDetect produces incorrect results (wrong threshold, missed cuts, etc.), consult `ref/Av1an/` for how Av1an implements scene detection in Rust as a reference for algorithm tuning. Do not copy the Rust code directly — adapt the approach to Python/PySceneDetect parameters.

#### Sub-phase 2 — `split_chunks_from_state`

```python
def split_chunks_from_state(
    video_meta: VideoMetadata,
    output_dir: Path,
    tracker: ProgressTracker,
    crop_params: CropParams | None = None,
) -> list[ChunkVideoMetadata]:
```

Reads `scene_boundaries` from state. For each boundary pair it:

1. Derives the frame-range chunk name: `chunk.<start_padded>-<end_padded>.mkv`.
2. Skips if the chunk_id already exists in `PipelineState.chunks_metadata`.
3. Calls ffmpeg to split that segment (using `-ss` / `-to` with exact timestamps).
4. Verifies the output file exists and is non-empty; marks FAILED if not.
5. Calls `tracker.update_chunk_metadata(chunk_meta)` immediately after each successful split.

#### Orchestrator entry point `chunk_video`

```log
chunk_video()
  ├─ if "scene_boundaries" not in state.phases["chunking"].metadata
  │     → detect_scenes_to_state()
  └─ split_chunks_from_state()   (skips already-done chunks)
```

---

### 5. Frame-range Chunk Naming

Chunk names follow the pattern `chunk.<start_padded>-<end_padded>.mkv` where both frame numbers are zero-padded to 6 digits (e.g. `chunk.000000-000319.mkv`).

Encoded attempt files live under `<work_dir>/encoded/<strategy>/` and are named:
`chunk.<start>-<end>.<width>x<height>.attempt_<N>.crf<CRF>.mkv`

The resolution component (`<width>x<height>`) is derived from the chunk's actual output dimensions after cropping. This prevents conflicts when crop parameters change between pipeline runs, since a different crop produces a different resolution and therefore a different filename.

`ChunkVideoMetadata.chunk_id` is set to the stem of the chunk file (e.g. `chunk.000000-000319`), making it stable and unique regardless of insertion order.

---

### 6. Legacy Migration

| Legacy module                              | Destination                     | Notes                                                                                                                |
| ------------------------------------------ | ------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `pymkvextract/main.py`                     | `pyqenc/phases/extraction.py`   | `MKVTrackExtractor`, `StreamBase` hierarchy, `streams_filter_plain_regex` inlined; `mkvextract` subprocess call kept |
| `pymkvcompare/metrics.py` + `processes.py` | `pyqenc/quality.py`             | `run_metric`, `MetricType` inlined; async wrappers simplified                                                        |
| `pymkva2/`                                 | `pyqenc/phases/audio.py`        | `AudioEngine`, strategy classes inlined; `alive_progress` bar kept                                                   |
| `metrics_visualization/`                   | `pyqenc/utils/visualization.py` | `analyze_chunk_quality`, parsers, statistics, `create_unified_plot` inlined                                          |

After migration all `from pyqenc.legacy.*` imports are removed and `pyqenc/legacy/` is deleted.

---

## Data Models

### Updated `PipelineState`

```python
class PipelineState(BaseModel):
    version: str
    source_video: VideoMetadata          # replaces SourceVideoMetadata
    current_phase: str
    phases: dict[str, PhaseState]
    chunks_metadata: dict[str, ChunkVideoMetadata]  # replaces dict[str, ChunkMetadata]
    chunks: dict[str, ChunkState]
```

### `PhaseState` metadata

`PhaseState.metadata` is typed as `PhaseMetadata | None` where `PhaseMetadata` is a Pydantic model:

```python
class SceneBoundary(BaseModel):
    frame: int
    timestamp_seconds: float

class PhaseMetadata(BaseModel):
    # Extraction phase
    crop_params: str | None = None
    # Chunking phase
    scene_boundaries: list[SceneBoundary] = []
```

This replaces the previous `dict[str, Any]` metadata field throughout `PhaseState` and `PhaseUpdate`.

### Serialization contract

`VideoMetadata` serializes to:

```json
{
  "path": "/abs/path/to/file.mkv",
  "start_frame": 0,
  "duration_seconds": 5400.0,
  "frame_count": 129600,
  "fps": 24.0,
  "resolution": "1920x1080"
}
```

`ChunkVideoMetadata` adds `"chunk_id": "chunk.000000-000319"`.

---

## Error Handling

| Scenario                                 | Behavior                                                        |
| ---------------------------------------- | --------------------------------------------------------------- |
| ffprobe fails during lazy-load           | Field stays `None`; warning logged; no exception                |
| Source file changed on resume            | Warning logged; pipeline continues with live values             |
| Zero scenes detected                     | Warning logged; single boundary stored; one chunk produced      |
| Chunk file missing/empty after split     | Critical logged; chunk marked `FAILED` in state; pipeline halts |
| Signal received mid-run                  | `flush()` called; process exits with non-zero code              |
| Unhandled exception                      | `flush()` called in `finally` block; exception re-raised        |
| Pydantic validation error on deserialize | Warning logged; field set to default/None; pipeline continues   |

---

## Testing Strategy

- Unit tests for `VideoMetadata` lazy-loading: verify probe is called exactly once even when multiple properties are accessed.
- Unit tests for `VideoMetadata.populate_from_ffprobe`: verify fields are filled without triggering a probe.
- Unit tests for Pydantic round-trip: verify `model_dump_full()` → `model_validate()` preserves all fields including cached private ones.
- Unit tests for `ProgressTracker` typed updates: verify `PhaseUpdate` and `ChunkUpdate` round-trip through serialization.
- Unit tests for `detect_scenes_to_state` with a mock detector returning zero scenes: verify single-boundary fallback.
- Unit tests for `split_chunks_from_state` resumption: verify already-recorded chunks are skipped.
- Unit tests for frame-range naming: verify padding width and name format.
- Integration test using the provided sample videos to verify end-to-end chunking produces non-empty chunks with correct names.
- Import smoke test: verify no `pyqenc.legacy` imports remain after migration.
