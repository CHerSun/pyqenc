# Design Document: Pipeline Metrics Report

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-10

## Overview

This feature adds a `MetricsCollector` component that is injected into every phase constructor and accumulates wall-clock timing, disk space, and CRF convergence data throughout a pipeline run. The data is persisted incrementally to `metrics.yaml` in the work directory root using the `.tmp`-then-rename atomic write protocol. The report survives interruptions and resumes across runs.

The design follows three principles established in the requirements discussion:

1. **Protocol-based injection** — `MetricsCollector` is a `Protocol`; phases depend only on the abstract surface, enabling OTel/Prometheus substitution later.
2. **Phase-owned timing** — each phase calls `collector.time(key)` and `collector.record_step(...)` for its own sub-steps; the orchestrator does not wrap phase calls.
3. **Self-managing flush** — the collector auto-flushes every `FLUSH_INTERVAL` recording calls; the orchestrator only calls `flush(partial=True)` on abnormal exit.

### Key Design Decisions

- `TimeKey` and `SpaceKey` are `StrEnum` with dotted values (`"encoding.main"`, `"extracted.video"`). Internal storage is `dict[TimeKey, float]` (seconds) and `dict[SpaceKey, int]` (bytes). Grouping by prefix at report time is derived by splitting on `"."`.
- Time serializes as integer seconds + `"[Dd ]HH:MM:SS"` duration string + `"X.X%"` percent string. Space serializes as `"X.XX GB"` string + `"X.X%"` percent string. All formatting happens at serialization time — internal storage stays lossless.
- `MetricsCollector` is injected as a **required** constructor parameter in every phase — no `None` fallback. Tests use `NoOpMetricsCollector`.
- Space is measured at flush time via `Path.stat()` and directory traversal only — no ffprobe calls.
- Convergence stats accumulate incrementally via `record_step(key, elapsed, convergence_update=...)` as each chunk converges.
- `partial=False` is set only by the orchestrator calling `collector.flush(partial=False)` after all phases complete successfully. All auto-flushes and signal-handler flushes use `partial=True`.
- `api.py` standalone callers construct and pass a `NoOpMetricsCollector` (or a real one if they want metrics).

---

## Architecture

```
PipelineOrchestrator
  │
  ├── if config.no_metrics → constructs NoOpMetricsCollector()
  │   else                 → constructs YamlMetricsCollector(work_dir, config)
  │                          registers signal handlers → collector.flush(partial=True)
  ├── passes collector to every phase constructor
  │
  ├── JobPhase(config, registry, collector)
  ├── ExtractionPhase(config, registry, collector)
  ├── ChunkingPhase(config, registry, collector)
  ├── OptimizationPhase(config, registry, collector)
  ├── EncodingPhase(config, registry, collector)
  ├── AudioPhase(config, registry, collector)
  └── MergePhase(config, registry, collector)

pyqenc/metrics.py
  ├── TimeKey (StrEnum)
  ├── SpaceKey (StrEnum)
  ├── ConvergenceUpdate (dataclass)
  ├── ConvergenceStats (Pydantic model)
  ├── TimeEntry (Pydantic model)
  ├── SpaceEntry (Pydantic model)
  ├── PipelineMetrics (Pydantic model)
  ├── MetricsCollector (Protocol)
  ├── YamlMetricsCollector (concrete implementation)
  └── NoOpMetricsCollector (no-op for tests / standalone)
```

The `_build_registry` factory in `phase.py` is updated to accept and thread the collector through to every phase constructor.

---

## Components and Interfaces

### `TimeKey` StrEnum

```python
class TimeKey(StrEnum):
    JOB_PROBE               = "job.probe"
    JOB_CROP_DETECT         = "job.crop_detect"
    EXTRACTION              = "extraction.mkvextract"
    CHUNKING_SCENE_DETECT   = "chunking.scene_detect"
    CHUNKING_SPLIT          = "chunking.split"
    AUDIO                   = "audio.processing"
    ENCODING_OPTIMIZATION   = "encoding.optimization"
    ENCODING_MAIN           = "encoding.main"
    MERGE_CONCAT            = "merge.concat"
    MERGE_QUALITY_MEASURE   = "merge.quality_measure"
    RECOVERY                = "recovery"
```

### `SpaceKey` StrEnum

```python
class SpaceKey(StrEnum):
    SOURCE               = "source"
    EXTRACTED_VIDEO      = "extracted.video"
    EXTRACTED_AUDIO      = "extracted.audio"
    EXTRACTED_OTHER      = "extracted.other"
    CHUNKS               = "chunks"
    AUDIO_INTERMEDIATE   = "audio.intermediate"
    AUDIO_FINAL          = "audio.final"
    ENCODING_WORKSPACE   = "encoding.workspace"
    ENCODING_OUTPUTS     = "encoding.outputs"
    FINAL                = "final"
```

### `ConvergenceUpdate` dataclass

Passed by phases to `record_step()` when a chunk's CRF search converges:

```python
@dataclass
class ConvergenceUpdate:
    strategy:       str   # display name (with + separators)
    attempt_count:  int   # total attempts for this chunk/strategy pair
```

### `MetricsCollector` Protocol

The phase-facing surface (recording only):

```python
@runtime_checkable
class MetricsCollector(Protocol):
    def time(self, key: TimeKey) -> ContextManager[None]: ...
    def record_step(
        self,
        key:                TimeKey,
        elapsed_seconds:    float,
        convergence_update: ConvergenceUpdate | None = None,
    ) -> None: ...
```

The full interface (also includes flush, used only by the orchestrator):

```python
    def flush(self, partial: bool = True) -> None: ...
```

`flush()` is intentionally excluded from the phase-facing Protocol surface — phases never call it.

### `YamlMetricsCollector`

Concrete implementation backed by `metrics.yaml`:

```python
class YamlMetricsCollector:
    def __init__(
        self,
        work_dir:    Path,
        config:      PipelineConfig,
        force_wipe:  bool = False,
    ) -> None: ...
```

**Constructor behaviour:**
- If `force_wipe=True`: delete existing `metrics.yaml` and start fresh.
- Otherwise: load existing `metrics.yaml` and resume accumulation from persisted state.
- Stores `work_dir` and `config` for space measurement at flush time.

**`time(key)` context manager:**
- Records `time.monotonic()` on enter.
- On exit: computes elapsed, calls `record_step(key, elapsed)`.

**`record_step(key, elapsed_seconds, convergence_update=None)`:**
- Adds `elapsed_seconds` to `_time_accum[key]`.
- If `convergence_update` is not None: updates Welford accumulators for the strategy — increments `_conv_n[strategy]`, updates `_conv_min[strategy]`, `_conv_max[strategy]`, `_conv_total[strategy]`, and updates `_conv_welford_mean[strategy]` and `_conv_welford_M2[strategy]` using Welford's online algorithm.
- Increments `_flush_counter`. If `_flush_counter >= FLUSH_INTERVAL`: calls `flush(partial=True)` and resets counter.

**`flush(partial)`:**
- Measures current disk space via `_measure_space()`.
- Builds `PipelineMetrics` from accumulated state.
- Sets `partial` flag and updates `run_date` to current local time.
- Writes atomically to `work_dir / METRICS_YAML_FILENAME` via `.tmp`-then-rename.
- On write failure: logs WARNING, does not raise.

### `NoOpMetricsCollector`

Satisfies the `MetricsCollector` Protocol but discards all data:

```python
class NoOpMetricsCollector:
    def time(self, key: TimeKey) -> ContextManager[None]: ...      # no-op context manager
    def record_step(self, key: TimeKey, elapsed_seconds: float,
                    convergence_update: ConvergenceUpdate | None = None) -> None: ...
    def flush(self, partial: bool = True) -> None: ...
```

Used in tests and `api.py` standalone callers.

---

## Data Models

### `PipelineMetrics` (Pydantic)

```python
class TimeEntry(BaseModel):
    category: str   # TimeKey value, e.g. "encoding.main"
    seconds:  int   # integer seconds (sub-second precision is noise at this scale)
    duration: str   # "[Dd ]HH:MM:SS", e.g. "02:05:00" or "1d 01:01:01"
    percent:  str   # 1 decimal place with % suffix, e.g. "54.6%"

class SpaceEntry(BaseModel):
    category: str   # SpaceKey value, e.g. "encoding.workspace"
    size:     str   # "X.XX GB" (2 decimal places), e.g. "18.42 GB"
    percent:  str   # 1 decimal place with % suffix, e.g. "61.3%"

class AttemptStats(BaseModel):
    total:  int
    min:    int
    mean:   float   # rounded to 1 decimal place
    max:    int
    stddev: float   # population stddev, rounded to 1 decimal place

class ConvergenceStats(BaseModel):
    strategy: str
    chunks:   int
    attempts: AttemptStats

class TimeDistribution(BaseModel):
    updated_at:     str   # "YYYY-MM-DD HH:MM:SS" — when time/convergence data was last captured
    total_seconds:  int
    total_duration: str   # "[Dd ]HH:MM:SS"
    breakdown:      list[TimeEntry]   # sorted descending by seconds

class SpaceDistribution(BaseModel):
    updated_at:  str   # "YYYY-MM-DD HH:MM:SS" — when space snapshot was taken
    total_size:  str   # "X.XX GB"
    breakdown:   list[SpaceEntry]   # sorted descending by bytes

class ConvergenceSection(BaseModel):
    updated_at:  str                  # same as TimeDistribution.updated_at
    strategies:  list[ConvergenceStats]

class PipelineMetrics(BaseModel):
    run_date:           str                            # "YYYY-MM-DD HH:MM:SS" — last file write
    partial:            bool
    time_distribution:  TimeDistribution
    space_distribution: SpaceDistribution
    convergence:        ConvergenceSection | None = None  # omitted when no data
```

**Internal storage vs. serialization:**
- Time is stored internally as `dict[TimeKey, float]` (accumulated seconds, float precision). Converted to `int` seconds only at serialization — `int(round(seconds))`.
- Space is stored internally as `dict[SpaceKey, int]` (bytes, exact). Converted to `"X.XX GB"` string at serialization via `_format_gb(n: int) -> str` — always GB, 2 decimal places, 1024-based (`n / 1024**3`).
- Percentages are formatted as `f"{value:.1f}%"` strings at serialization.
- Duration strings are formatted by `_format_duration(seconds: int) -> str`: days component omitted when 0, e.g. `"00:05:30"` for 330 s, `"1d 02:03:04"` for 93784 s.
- All datetime strings use `datetime.now().strftime("%Y-%m-%d %H:%M:%S")` — space separator, no `T`, no timezone suffix.
- `run_date` is set on every file write. `time_distribution.updated_at` and `convergence.updated_at` are set on every `_flush_incremental()` and `flush()`. `space_distribution.updated_at` is set only when `_measure_space()` runs (inside `flush()`).

**YAML serialisation:** `pyyaml` with `default_flow_style=False`. The top-level key is `pipeline_metrics:` wrapping the model fields.

---

## Space Measurement Logic

`_measure_space(work_dir, config) -> dict[SpaceKey, int]` performs a point-in-time filesystem scan. Called only from `flush()` (on normal or abnormal exit) — never from `_flush_incremental()`. This avoids scanning potentially tens of thousands of files on every auto-flush. No ffprobe or ffmpeg calls.

| SpaceKey | Measurement |
|---|---|
| `SOURCE` | `config.source_video.stat().st_size` |
| `EXTRACTED_VIDEO` | sum of `*.mkv` in `extracted/` |
| `EXTRACTED_AUDIO` | sum of `*.mka` in `extracted/` |
| `EXTRACTED_OTHER` | sum of all other files in `extracted/` |
| `CHUNKS` | recursive sum of all files in `chunks/` |
| `AUDIO_INTERMEDIATE` | sum of `*.flac` in `audio/` |
| `AUDIO_FINAL` | sum of non-`.flac` files in `audio/` |
| `ENCODING_WORKSPACE` | recursive sum of all files in `encoding/` |
| `ENCODING_OUTPUTS` | recursive sum of all files in `encoded/` |
| `FINAL` | recursive sum of all files in `final/` |

Missing directories or files return `0`. `OSError` on individual `stat()` calls is caught and logged at DEBUG level.

**`audio/` split logic:** iterate `audio/` (non-recursive); files with `.flac` suffix → `AUDIO_INTERMEDIATE`; all other files → `AUDIO_FINAL`. This matches the pipeline convention: intermediate FLAC processing chain outputs vs. final AAC/other delivery files.

**`extracted/` split logic:** iterate `extracted/` (non-recursive); `.mkv` → `EXTRACTED_VIDEO`; `.mka` → `EXTRACTED_AUDIO`; everything else → `EXTRACTED_OTHER`.

---

## Flush Mechanics

```
FLUSH_INTERVAL = 10   # named constant in metrics.py
METRICS_YAML_FILENAME = "metrics.yaml"   # named constant
```

**Two flush modes:**

- `_flush_incremental()` — writes time and convergence accumulators only. No filesystem scan. Called automatically by `record_step()` every `FLUSH_INTERVAL` updates.
- `flush(partial: bool)` — full flush: logs `"Measuring disk space for metrics..."` at INFO level (so the user sees why exit is delayed), runs `_measure_space()`, then writes time + convergence + space. Called only by the orchestrator on normal or abnormal exit.

**Counter-based auto-flush:** `_flush_counter` is incremented on every `record_step()` call (including those triggered by `time()` context manager exit). When `_flush_counter >= FLUSH_INTERVAL`, `_flush_incremental()` is called and the counter resets to 0. Space distribution is omitted from these intermediate writes.

**Atomic write:** Both flush paths write to `work_dir / (METRICS_YAML_FILENAME + TEMP_SUFFIX)` then rename to `work_dir / METRICS_YAML_FILENAME` via `Path.replace()`.

**Orchestrator abnormal-exit flush:** When `config.no_metrics` is `False`, the orchestrator registers `signal.signal(SIGINT, ...)` and `signal.signal(SIGTERM, ...)` handlers (plus `signal.signal(signal.CTRL_C_EVENT, ...)` on Windows) that call `collector.flush(partial=True)` before re-raising or exiting. An `atexit` handler also calls `flush(partial=True)` as a safety net for unhandled exceptions. When `config.no_metrics` is `True`, no signal handlers or `atexit` hooks are registered — there is nothing to flush.

**Final flush on success:** When `config.no_metrics` is `False`, after all phases complete successfully the orchestrator calls `collector.flush(partial=False)` to write the final report with `partial: false` and a complete space snapshot. When `config.no_metrics` is `True`, this call is skipped.

---

## Phase Integration

### Constructor Change

Every phase `__init__` gains a required `collector: MetricsCollector` parameter:

```python
def __init__(
    self,
    config:    PipelineConfig,
    phases:    dict[type[Phase], Phase] | None = None,
    collector: MetricsCollector = ...,   # required, no default
) -> None:
```

`_build_registry` in `phase.py` is updated to accept a `collector` argument and pass it to every phase constructor.

### Per-Phase Timing Calls

| Phase | TimeKey(s) | Call site |
|---|---|---|
| `JobPhase` | `JOB_PROBE` | wraps `VideoMetadata` probing calls in `_create_or_update_job` |
| `JobPhase` | `JOB_CROP_DETECT` | wraps `detect_crop_parameters` call in `_resolve_crop` |
| `ExtractionPhase` | `EXTRACTION` | wraps `extractor.extract_tracks()` call |
| `ExtractionPhase` | `RECOVERY` | wraps `_recover()` call in `run()` |
| `ChunkingPhase` | `CHUNKING_SCENE_DETECT` | wraps `detect_scenes()` call |
| `ChunkingPhase` | `CHUNKING_SPLIT` | `record_step(CHUNKING_SPLIT, elapsed)` after each chunk split |
| `ChunkingPhase` | `RECOVERY` | wraps `_recover()` call in `run()` |
| `AudioPhase` | `AUDIO` | wraps the full async engine execution |
| `AudioPhase` | `RECOVERY` | wraps `_recover()` call in `run()` |
| `OptimizationPhase` | `ENCODING_OPTIMIZATION` | `record_step(ENCODING_OPTIMIZATION, elapsed, convergence_update)` after each test-chunk attempt converges |
| `OptimizationPhase` | `RECOVERY` | wraps `_recover()` / param-load call in `run()` |
| `EncodingPhase` | `ENCODING_MAIN` | `record_step(ENCODING_MAIN, elapsed, convergence_update)` after each chunk/strategy pair converges |
| `EncodingPhase` | `RECOVERY` | wraps `_recover_encoding_attempts()` call in `run()` |
| `MergePhase` | `MERGE_CONCAT` | wraps ffmpeg concat call per strategy |
| `MergePhase` | `MERGE_QUALITY_MEASURE` | wraps `_measure_quality()` call per strategy |
| `MergePhase` | `RECOVERY` | wraps `_recover()` call in `run()` |

**`RECOVERY` accumulation pattern:** each phase wraps its own `_recover()` call:

```python
import time as _time
_t0 = _time.monotonic()
artifacts = self._recover(force_wipe=force_wipe, execute=True)
self._collector.record_step(TimeKey.RECOVERY, _time.monotonic() - _t0)
```

**Convergence update pattern** (EncodingPhase, after `_finalize_winning_attempt`):

```python
self._collector.record_step(
    TimeKey.ENCODING_MAIN,
    elapsed_for_this_chunk,
    convergence_update=ConvergenceUpdate(
        strategy=strategy,
        attempt_count=attempt_number,
    ),
)
```

### `api.py` Integration

Each standalone function in `api.py` constructs a `NoOpMetricsCollector` and passes it to `_build_registry`:

```python
from pyqenc.metrics import NoOpMetricsCollector

collector = NoOpMetricsCollector()
registry  = _build_registry(config, collector)
```

`_build_registry` signature becomes:

```python
def _build_registry(
    config:    PipelineConfig,
    collector: MetricsCollector | None = None,
) -> dict[type[Phase], Phase]:
```

When `collector` is `None`, a `NoOpMetricsCollector` is constructed internally as a safe default.

### `PipelineConfig` — `no_metrics` field

`PipelineConfig` gains one new field:

```python
no_metrics: bool = False
```

Default is `False` (metrics enabled). Set to `True` by the CLI when `--no-metrics` is passed. The field travels through the existing config path — no separate parameter threading is needed.

### CLI — `--no-metrics` flag

The CLI argument parser adds:

```python
parser.add_argument(
    "--no-metrics",
    action="store_true",
    default=False,
    help="Suppress metrics.yaml output (metrics are still collected internally but not written to disk)",
)
```

Wiring in the CLI entry point:

```python
config = PipelineConfig(
    ...
    no_metrics = args.no_metrics,
)
```

The flag is documented in help text exactly as: `"Suppress metrics.yaml output (metrics are still collected internally but not written to disk)"` (Req 8.6).

### Orchestrator — collector construction

```python
if config.no_metrics:
    collector: MetricsCollector = NoOpMetricsCollector()
else:
    collector = YamlMetricsCollector(work_dir=config.work_dir, config=config, force_wipe=force_wipe)
    _register_metrics_signal_handlers(collector)
    atexit.register(collector.flush, partial=True)
```

Signal handler and `atexit` registration are skipped entirely when `no_metrics` is `True`. The collector is then passed to `_build_registry` as before — phases are unaffected.

### Phase transparency

Phases always receive a `MetricsCollector` and call `time()` / `record_step()` normally. They have no knowledge of whether the injected implementation is `YamlMetricsCollector` or `NoOpMetricsCollector`. This is the core benefit of Protocol-based injection — the `--no-metrics` flag requires zero changes to any phase.

---

## Convergence Stats Computation

`_compute_convergence(accumulators: dict[str, ConvergenceAccumulator]) -> list[ConvergenceStats] | None`

- `ConvergenceAccumulator` is an internal dataclass holding per-strategy running state: `n` (chunks_total), `total` (attempts_total), `min`, `max`, `welford_mean`, `welford_M2`.
- If all accumulators are empty (`n == 0`): return `None` (omit section).
- For each strategy with `n >= 1`:
  - `chunks` = `n`
  - `attempts.total` = `total`
  - `attempts.min` = `min`
  - `attempts.max` = `max`
  - `attempts.mean` = `round(welford_mean, 1)`
  - `attempts.stddev` = `round(sqrt(welford_M2 / n), 1)` — population stddev; `0.0` when `n == 1`
- Returns list sorted by strategy name for deterministic output.

**Welford update on each `record_step` with `convergence_update`:**
```python
n     += 1
total += x
min    = min(min, x)
max    = max(max, x)
delta  = x - mean
mean  += delta / n
M2    += delta * (x - mean)   # uses updated mean
```

**Resume from `metrics.yaml`:** The persisted YAML stores `chunks_total`, `attempts_total`, `attempts_min`, `attempts_max`, `attempts_mean`, `attempts_stddev`. On load, `welford_mean` is restored directly from `attempts_mean`. `welford_M2` is restored as `attempts_stddev² * n`. This allows exact resumption of the running accumulators from the persisted state.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| `metrics.yaml` write fails | Log WARNING, pipeline continues |
| `metrics.yaml` load fails on resume | Log WARNING, start fresh (do not abort) |
| `stat()` fails on individual file during space scan | Log DEBUG, treat as 0 bytes |
| `source_video` does not exist at flush time | Log DEBUG, record 0 for `SpaceKey.SOURCE` |
| Phase raises exception before `time()` context exits | Context manager catches and re-raises; elapsed is still recorded |

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Time accumulation round-trip

*For any* `TimeKey` and any sequence of positive elapsed durations recorded via `record_step`, the accumulated seconds for that key must equal the sum of all recorded durations.

**Validates: Requirements 2.1, 2.2, 2.2a**

### Property 2: Time distribution math

*For any* set of time recordings, `total_seconds` in the flushed report must equal the sum of all individual key `seconds` values, and each key's `percent` must equal `(seconds / total_seconds) * 100` (within floating-point tolerance). Zero-second keys must still appear in the breakdown.

**Validates: Requirements 2.3, 2.5**

### Property 3: Breakdown sorted descending

*For any* flushed `PipelineMetrics`, both `time_distribution.breakdown` and `space_distribution.breakdown` must be sorted in descending order of their primary numeric field (`seconds` and bytes respectively).

**Validates: Requirements 2.6, 3.5**

### Property 4: Space measurement accuracy

*For any* work directory configuration with known file sizes, the space measurement must return the exact byte count for each `SpaceKey` category, with absent directories contributing 0 bytes. The `total_bytes` must equal the sum of all category bytes, and each `percent` must equal `(category_bytes / total_bytes) * 100`.

**Validates: Requirements 3.1, 3.3, 3.4**

### Property 5: Convergence stats math

*For any* set of per-strategy attempt count sequences fed incrementally via `record_step`, the computed `ConvergenceStats` must satisfy: `attempts_min == min(counts)`, `attempts_max == max(counts)`, `attempts_total == sum(counts)`, `chunks_total == len(counts)`, `attempts_mean == round(mean(counts), 1)`, and `attempts_stddev == round(population_stddev(counts), 1)`.

**Validates: Requirements 4.2, 4.1a**

### Property 6: YAML serialization round-trip

*For any* valid `PipelineMetrics` instance, serializing to YAML and deserializing back must produce an equivalent instance (all numeric fields within floating-point tolerance, all string fields identical).

**Validates: Requirements 5.1, 5.2, 5.3, 5.4**

---

## Testing Strategy

### Unit Tests (specific examples and edge cases)

- `force_wipe=True` deletes existing `metrics.yaml` and starts fresh (Req 1.2)
- Write failure (mocked `Path.replace` raising `OSError`) logs WARNING and does not propagate (Req 1.5)
- `TimeKey` StrEnum contains exactly the 11 required keys (Req 2.4)
- `SpaceKey` StrEnum contains exactly the 10 required keys (Req 3.2)
- Empty convergence data produces `convergence: null` / omitted section in YAML (Req 4.4)
- `NoOpMetricsCollector` satisfies `isinstance(noop, MetricsCollector)` via `runtime_checkable` (Req 6.1)
- Each phase constructor accepts a `collector` parameter (Req 6.2)
- `flush(partial=False)` sets `partial: false` in output; `flush(partial=True)` sets `partial: true` (Req 5.4)
- When `config.no_metrics=True`, the orchestrator constructs `NoOpMetricsCollector` and skips signal handler / `atexit` registration (Req 8.2, 8.5)

### Property-Based Tests (using `hypothesis`)

Each property test runs a minimum of 100 iterations. Tag format: `# Feature: pipeline-metrics-report, Property N: <text>`

**Property 1 — Time accumulation round-trip**
Generate: random `TimeKey`, random list of positive floats as elapsed durations.
Assert: `collector._time_accum[key] == sum(durations)` after all `record_step` calls.

**Property 2 — Time distribution math**
Generate: random mapping of `TimeKey → float` (non-negative).
Assert: `total_seconds == sum(values)`, each `percent == value / total * 100` (or 0.0 when total is 0).

**Property 3 — Breakdown sorted descending**
Generate: random `PipelineMetrics` instances.
Assert: `time_distribution.breakdown` is sorted descending by `seconds`; `space_distribution.breakdown` is sorted descending by bytes.

**Property 4 — Space measurement accuracy**
Generate: random directory trees with known file sizes (using `tmp_path` fixture).
Assert: `_measure_space()` returns exact byte counts per category; total == sum of parts.

**Property 5 — Convergence stats math**
Generate: random sequences of attempt counts (integers ≥ 1) fed incrementally via `record_step`.
Assert: all `ConvergenceStats` fields match `min/max/sum/mean/population_stddev/len` of the input sequences. Also assert that resume from persisted YAML produces identical results to a fresh run.

**Property 6 — YAML serialization round-trip**
Generate: random valid `PipelineMetrics` instances.
Assert: `deserialize(serialize(m)) == m` (field-by-field comparison within tolerance).

### Integration Notes

- `hypothesis` is already an approved test dependency (used via `pytest-hypothesis` or direct import). If not yet in `pyproject.toml`, it must be added to `[dependency-groups] test`.
- Property tests live in `tests/test_metrics_properties.py`; unit tests in `tests/test_metrics.py`.
- Phase integration tests (verifying that phases actually call the collector) use a spy/mock `MetricsCollector` and verify `record_step` was called with the expected keys after `phase.run()`.
