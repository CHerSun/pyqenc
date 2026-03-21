# Design Document — Phase Object Model

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-20

## Overview

This refactor restructures the pipeline from a collection of standalone functions driven by a monolithic orchestrator into a graph of self-contained `Phase` objects. Each phase owns its dependencies, artifact enumeration, recovery, invalidation, execution, and logging. The orchestrator becomes a thin driver that builds the phase registry and iterates it.

Two execution modes emerge naturally from the same phase objects:

- **Auto-pipeline** (`pyqenc auto`): orchestrator builds the full registry, runs phases in order, passes live results forward via cached `phase.result`.
- **Standalone** (`pyqenc chunk`, `pyqenc encode`, etc.): CLI constructs the target phase with its full dependency chain; dependencies scan their own artifacts on demand.

---

## Architecture

```
PipelineConfig
      │
      ▼
Orchestrator
  builds dict[type[Phase], Phase] registry (in execution order)
  iterates registry values → phase.run(dry_run)
      │
      ├── JobPhase          (no deps)
      ├── ExtractionPhase   (deps: Job)
      ├── ChunkingPhase     (deps: Job, Extraction)
      ├── OptimizationPhase (deps: Job, Chunking)
      ├── EncodingPhase     (deps: Job, Chunking, Optimization)
      ├── AudioPhase        (deps: Job, Extraction)
      └── MergePhase        (deps: Job, Encoding, Audio)
```

In standalone mode the CLI constructs only the target phase; the phase pulls its dependencies from the same registry pattern, but each dependency is freshly constructed and will `scan()` rather than return a cached result.

---

## Components and Interfaces

### Phase Protocol

```python
class Phase(Protocol):
    name:         str
    dependencies: list[Phase]   # typed deps stored internally; exposed as list for orchestrator
    result:       PhaseResult | None

    def scan(self) -> PhaseResult:
        """Enumerate and classify current artifacts without executing any work."""
        ...

    def run(self, dry_run: bool = False) -> PhaseResult:
        """Recover, execute pending work, cache and return result."""
        ...
```

Every phase implementation:
1. Receives `PipelineConfig` and `dict[type[Phase], Phase]` at construction.
2. Extracts its typed dependencies from the registry (e.g. `self._job: JobPhase = phases[JobPhase]`).
3. On `scan()` or `run()`: calls `dep.scan()` on any dependency without a cached result — this may trigger a chain of scans up the dependency tree; all scans are read-only (no file writes).
4. Checks `dep.result.is_complete` for each dependency — if any required dependency is not complete, fails fast.
5. Runs its own `_recover()` to classify its own artifacts.
6. Sets `is_complete` on its result based on whether all expected artifacts are `COMPLETE` — only the phase itself knows what "all expected" means (e.g. encoding knows it must produce N chunks × M strategies, where N comes from `ChunkingPhase.result`).
7. On `run()`: executes work for pending artifacts, then caches result in `self.result`.

### ArtifactState

```python
class ArtifactState(Enum):
    ABSENT        = "absent"         # file missing — must produce
    ARTIFACT_ONLY = "artifact_only"  # file present, sidecar missing — repair sidecar
    STALE         = "stale"          # file+sidecar present, parameters changed — re-produce or skip
    COMPLETE      = "complete"       # file+sidecar present, parameters match — skip
```

### Artifact Base

Each phase defines a concrete typed artifact. A minimal shared base:

```python
@dataclass
class Artifact:
    path:  Path
    state: ArtifactState
```

Concrete examples:
- `ChunkArtifact(Artifact)` — adds `metadata: ChunkMetadata | None`
- `EncodedArtifact(Artifact)` — adds `chunk_id: str`, `strategy: Strategy`, `crf: float`
- `MergedArtifact(Artifact)` — adds `strategy: Strategy`, `frame_count: int | None`

### PhaseResult

```python
@dataclass
class PhaseResult:
    outcome:   PhaseOutcome          # COMPLETED / REUSED / DRY_RUN / FAILED
    artifacts: list[Artifact]        # all artifacts, any state
    message:   str
    error:     str | None = None

    # Derived helpers
    @property
    def is_complete(self) -> bool:
        """True when all expected artifacts are COMPLETE (outcome is COMPLETED or REUSED)."""
        return self.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED)

    @property
    def complete(self) -> list[Artifact]: ...   # state == COMPLETE
    @property
    def pending(self) -> list[Artifact]: ...    # state in (ABSENT, ARTIFACT_ONLY, STALE)
    @property
    def did_work(self) -> bool: ...             # outcome == COMPLETED (work was done this run)
```

`outcome` is set by the phase after execution:
- All expected artifacts `COMPLETE`, no work done this run → `REUSED`
- All expected artifacts `COMPLETE`, work was done → `COMPLETED`
- Any expected artifact still not `COMPLETE` after execution → `FAILED`
- Dry-run with pending artifacts → `DRY_RUN`

`is_complete` is derived from `outcome` — downstream phases check `dep.result.is_complete` to decide whether to proceed. The phase itself is the only entity that knows what "all expected artifacts" means (e.g. encoding knows it must produce N chunks × M strategies, where N comes from `ChunkingPhase.result`).

### Strategy

```python
@dataclass(frozen=True)
class Strategy:
    name:      str   # display form: "slow+h265-aq"
    safe_name: str   # filesystem-safe: "slow_h265-aq"

    @staticmethod
    def from_name(name: str) -> "Strategy":
        return Strategy(name=name, safe_name=name.replace("+", "_").replace(":", "_"))
```

Replaces all string-based strategy name handling throughout the codebase.

---

## Phase Designs

### Artifact Recovery: Two Distinct Concerns

Every phase's `_recover()` method handles two separate concerns that must not be conflated:

**1. Dependency scanning (inputs)**
The phase's expected artifact set is derived from live upstream results — either from the pipeline-injected `phase.result` or from calling `dep.scan()` in standalone mode. This is always read from the dependency chain at recovery time; no cross-run tracking is needed. Examples:
- `EncodingPhase._recover()` iterates `chunk_ids × strategy_names` from `ChunkingPhase.result` and `OptimizationPhase.result.selected_strategies` — if strategies changed between runs, the new set is simply used directly; old strategy artifacts are never enumerated and are naturally invisible.
- `MergePhase._recover()` derives expected outputs from `EncodingPhase.result.encoded` (COMPLETE artifacts only) — no need to track which strategies were active last run.
- `AudioPhase._recover()` runs `AudioEngine.build_plan()` with the current `convert_filter` to determine which files are expected terminal outputs — the plan itself defines completeness without needing to know what filter was used last run.

**2. Own artifact recovery (outputs)**
The phase scans its own output directory directly and classifies each artifact. This is where config-change re-evaluation happens. Two sub-cases:

- **Type A — input parameters for plan building**: the current run's inputs already define the expected artifact set. No cross-run config tracking needed. The artifact is `COMPLETE`, `STALE`, or `ABSENT` purely based on whether it appears in the current expected set and exists on disk.
  - `ExtractionPhase`: `include`/`exclude` filter from current config defines expected files; files no longer matching are `STALE`.
  - `AudioPhase`: current `convert_filter` defines terminal outputs via `build_plan()`; intermediate-only files are `STALE`.

- **Type B — config that invalidates artifact validity**: the artifact files are only valid if the config that produced them matches the current config. This requires persisting the config used last run (in a phase YAML) and comparing on the next run. The resulting state depends on whether re-evaluation data exists on disk:
  - If metrics sidecars are present alongside the artifact, the phase can re-evaluate without re-running the work → downgrade to `ARTIFACT_ONLY` (file present, needs quality re-evaluation against new targets).
  - If no re-evaluation data exists, the artifact itself must be regenerated → downgrade to `STALE` (present but content is wrong, must be reproduced from scratch).
  - `OptimizationPhase`: quality targets affect which CRF values pass — persisted in `optimization.yaml`; if changed, deletes all result sidecars from `encoded/` (leaving attempt files in `encoding/` intact), then rescans CRF history. Always writes `optimization.yaml` (even in all-strategies mode) so this check works regardless of mode. Single owner of `encoded/` result sidecar invalidation.
  - `EncodingPhase`: does not check quality targets independently — `OptimizationPhase` already deleted stale result sidecars before `EncodingPhase._recover()` runs; it simply sees `ARTIFACT_ONLY` pairs and processes them normally.
  - `AudioPhase`: `audio_codec` and `audio_base_bitrate` affect the content of produced AAC files — persisted in `audio.yaml`; if changed, no re-evaluation data exists → `STALE` (AAC files must be regenerated).



- No dependencies.
- `scan()`: loads `job.yaml` if present; returns result with `force_wipe=False`.
- `run()`: validates source against `job.yaml` (path, size, resolution). On mismatch without `--force` → `FAILED`. On mismatch with `--force` → sets `force_wipe=True` on result, deletes `job.yaml` and all phase parameter YAMLs, continues. Creates/updates `job.yaml`. Resolves crop (manual → cached → detect).
- Result carries: `job: JobState`, `crop: CropParams | None`, `force_wipe: bool`.

### ExtractionPhase

- Dependencies: `JobPhase`.
- Checks `force_wipe` from `JobPhase.result` → deletes `extracted/` and `extraction.yaml`, then proceeds as if `ABSENT`.
- `_recover()`: scans `extracted/`; compares stored `include`/`exclude` from `extraction.yaml` against current params. Files matching new filter → `COMPLETE`. Files now excluded → `STALE`. Missing files that should be included → `ABSENT`.
- `run()`: extracts only `ABSENT` artifacts. Leaves `STALE` artifacts on disk (cleanup handles deletion).
- Persists `extraction.yaml` after successful run.

### ChunkingPhase

- Dependencies: `JobPhase`, `ExtractionPhase`.
- Checks `force_wipe` → deletes `chunks/` and `chunking.yaml`, then proceeds as if `ABSENT`.
- `_recover()`: loads scene boundaries from `chunking.yaml`; scans `chunks/`; classifies each chunk as `ABSENT`/`ARTIFACT_ONLY`/`COMPLETE`. Repairs `ARTIFACT_ONLY` by probing and writing missing sidecars.
- `run()`: runs scene detection if no boundaries cached; splits only pending chunks.
- Result carries: `chunks: list[ChunkArtifact]`.

### OptimizationPhase

- Dependencies: `JobPhase`, `ChunkingPhase`.
- Checks `force_wipe` → deletes optimization test artifacts and `optimization.yaml`, then proceeds as if `ABSENT`.
- **All-strategies mode**: `run()` returns all configured strategies as `selected_strategies` immediately, no test encodes, no logging — but always writes `optimization.yaml` with `strategy_results=[]` and current `quality_targets`. On the next run, if quality targets changed, deletes all result sidecars from `encoded/` (leaving attempt files in `encoding/` intact for CRF history replay) before returning.
- **Optimization mode**: checks crop mismatch against `optimization.yaml`. Without `--force` → `FAILED`. With `--force` → deletes optimization test artifacts and `optimization.yaml`, proceeds clean. Checks quality target change against `optimization.yaml`; if targets changed, deletes all result sidecars from `encoded/` (leaving attempt files intact), then treats all cached `StrategyTestResult` entries as stale and rescans CRF history from existing per-attempt sidecars (no re-encoding — existing attempt files are reused wherever they satisfy the updated targets). Runs test encodes for pending strategies. Persists per-strategy results (total size, avg CRF) ordered by increasing size to `optimization.yaml` including current quality targets. Applies tolerance to select strategies.
- On subsequent run with changed tolerance and matching quality targets: re-applies selection from `optimization.yaml` without re-encoding.
- `OptimizationPhase` is the single owner of quality-target change detection and `encoded/` result sidecar invalidation — `EncodingPhase` does not duplicate this check.
- Result carries: `selected_strategies: list[Strategy]`, `strategy_results: dict[Strategy, StrategyTestResult]`.

### EncodingPhase

- Dependencies: `JobPhase`, `ChunkingPhase`, `OptimizationPhase`.
- Checks `force_wipe` → deletes `encoding/`, `encoded/`, and `encoding.yaml`, then proceeds as if `ABSENT`. Does NOT touch optimization artifacts (those are handled by `OptimizationPhase`).
- Checks crop mismatch against `encoding.yaml`. Without `--force` → `FAILED`. With `--force` → deletes `encoding/`, `encoded/`, and `encoding.yaml`, proceeds clean.
- `_recover()`: for each `(chunk, strategy)` pair from `OptimizationPhase.result.selected_strategies`:
  - Scans `encoded/<strategy.safe_name>/` for result sidecar → `COMPLETE`.
  - Scans `encoding/<strategy.safe_name>/` for attempt files → `ARTIFACT_ONLY` (CRF search in progress or result sidecar deleted by `OptimizationPhase` on quality target change).
  - Neither → `ABSENT`.
  - Does NOT re-evaluate quality targets — `OptimizationPhase` already deleted stale result sidecars before `EncodingPhase` runs.
- `run()`: for each pending pair, runs CRF search in `encoding/<strategy.safe_name>/`. On convergence: hard-links winning attempt into `encoded/<strategy.safe_name>/`, writes result sidecar and graph. If `--cleanup` intermediate: deletes attempt files from `encoding/`.
- Result carries: `encoded: list[EncodedArtifact]`.

### AudioPhase

- Dependencies: `JobPhase`, `ExtractionPhase`.
- Checks `force_wipe` → deletes `audio/`, then proceeds as if `ABSENT`.
- `_recover()`: runs `AudioEngine.build_plan()` with the current `convert_filter` to determine expected terminal outputs; classifies each file in `audio/` as `COMPLETE` (terminal output present), `STALE` (present but not a terminal output under current filter), or `ABSENT` (expected terminal output missing). Additionally loads `audio.yaml`; if absent treats all existing files as `ARTIFACT_ONLY`; if `audio_codec` or `audio_base_bitrate` differ from current config, marks all existing artifacts `STALE` (AAC content must be regenerated).
- `run()`: processes only pending audio files; writes `audio.yaml` with current `audio_codec` and `audio_base_bitrate` after successful processing.
- Result carries: `audio_files: list[AudioArtifact]`.

### MergePhase

- Dependencies: `JobPhase`, `EncodingPhase`, `AudioPhase`.
- Checks `force_wipe` → deletes `final/`, then proceeds as if `ABSENT`.
- `_recover()`: scans `final/` for output files + sidecars; classifies as `COMPLETE` (file+sidecar) or `ABSENT`.
- In pipeline mode: reads encoded chunks from `EncodingPhase.result` directly.
- In standalone mode: scans `encoded/<strategy.safe_name>/` for each strategy.
- `run()`: concatenates chunks per strategy, measures final quality, writes sidecar.
- Result carries: `merged: list[MergedArtifact]`.

---

## Data Models

### JobState (existing, unchanged)

Stores source `VideoMetadata` and `CropParams`. Extended with `force_wipe: bool` on `JobPhaseResult`.

### Phase Parameter YAMLs

- `job.yaml` — source metadata + crop
- `extraction.yaml` — include/exclude filters
- `chunking.yaml` — scene boundaries
- `optimization.yaml` — per-strategy test results (ordered by size), selected strategies, crop, tolerance used, **quality targets** (written in both optimization mode and all-strategies mode)
- `encoding.yaml` — crop params
- `audio.yaml` — `audio_codec`, `audio_base_bitrate` (written after each successful audio run)

### EncodingResultSidecar (per-chunk result sidecar in `encoded/`)

Stores `winning_attempt`, `crf`, and `metrics` (targeted metric values). The `quality_targets` field previously stored here is **removed** — quality target tracking is now owned exclusively by `OptimizationPhase` via `optimization.yaml`. `EncodingPhase._recover()` no longer re-evaluates quality targets; `OptimizationPhase` deletes stale result sidecars before `EncodingPhase` runs.

### OptimizationParams (extended)

```python
class StrategyTestResult(BaseModel):
    strategy:    Strategy
    total_size:  int    # bytes
    avg_crf:     float

class OptimizationParams(BaseModel):
    crop:             CropParams | None
    test_chunks:      list[str]
    strategy_results: list[StrategyTestResult]  # ordered by increasing total_size
    tolerance_pct:    float                     # tolerance used when results were computed
    selected:         list[Strategy]            # selected strategies at time of last run
    quality_targets:  list[str]                 # targets active when test encodes ran, e.g. ["vmaf-min:93.0"]
```

### AudioParams (new)

```python
class AudioParams(BaseModel):
    audio_codec:        str | None   # codec used for AAC conversion (e.g. "aac")
    audio_base_bitrate: str | None   # base bitrate for 2.0 stereo (e.g. "192k")
```

Persisted to `audio.yaml` after each successful audio processing run. Used in `_recover()` to detect codec/bitrate changes that invalidate existing AAC file content.

---

## Directory Layout

```
work_dir/
  job.yaml
  extraction.yaml
  chunking.yaml
  optimization.yaml
  encoding.yaml
  audio.yaml

  extracted/          ← ExtractionPhase output
  chunks/             ← ChunkingPhase output
  encoding/           ← EncodingPhase CRF workspace (attempts)
    <strategy.safe_name>/
      <chunk_id>.<res>.crf<N>.mkv       (attempt file)
      <chunk_id>.<res>.crf<N>.yaml      (per-attempt metrics sidecar)
      <chunk_id>.<res>.crf<N>.png       (per-attempt quality graph)
  encoded/            ← EncodingPhase finalized artifacts (hard-links)
    <strategy.safe_name>/
      <chunk_id>.<res>.crf<N>.mkv       (hard-link of winning attempt)
      <chunk_id>.<res>.yaml             (result sidecar — COMPLETE marker)
      <chunk_id>.<res>.crf<N>.png       (hard-link of winning attempt graph)
  audio/              ← AudioPhase output
  final/              ← MergePhase output
```

---

## Dependency Injection and Registry

```python
# Orchestrator builds registry in execution order
def _build_registry(config: PipelineConfig) -> dict[type[Phase], Phase]:
    registry: dict[type[Phase], Phase] = {}
    registry[JobPhase]          = JobPhase(config, registry)
    registry[ExtractionPhase]   = ExtractionPhase(config, registry)
    registry[ChunkingPhase]     = ChunkingPhase(config, registry)
    registry[OptimizationPhase] = OptimizationPhase(config, registry)
    registry[EncodingPhase]     = EncodingPhase(config, registry)
    registry[AudioPhase]        = AudioPhase(config, registry)
    registry[MergePhase]        = MergePhase(config, registry)
    return registry
```

Each phase constructor extracts its typed dependencies:

```python
class EncodingPhase:
    def __init__(self, config: PipelineConfig, phases: dict[type[Phase], Phase]) -> None:
        self._config = config
        self._job    = cast(JobPhase,          phases[JobPhase])
        self._chunks = cast(ChunkingPhase,     phases[ChunkingPhase])
        self._opt    = cast(OptimizationPhase, phases[OptimizationPhase])
```

For standalone CLI, the same `_build_registry` function is called — phases without a cached result will `scan()` on first access.

---

## Orchestrator (simplified)

```python
class PipelineOrchestrator:
    def run(self, dry_run: bool) -> PipelineResult:
        registry = _build_registry(self.config)
        for phase in registry.values():
            result = phase.run(dry_run=dry_run)
            if result.outcome == PhaseOutcome.FAILED:
                logger.critical("Phase %s failed: %s", phase.name, result.error)
                return PipelineResult(success=False, error=result.error)
            if dry_run and result.outcome == PhaseOutcome.DRY_RUN:
                logger.info("[DRY-RUN] Stopping at %s — work needed", phase.name)
                break
        return PipelineResult(success=True, ...)
```

No phase-specific logic remains in the orchestrator.

---

## Cleanup Integration

Cleanup level is stored on `PipelineConfig` as `CleanupLevel` `IntEnum`:

```python
class CleanupLevel(IntEnum):
    NONE         = 0   # keep everything (default, no --cleanup flag)
    INTERMEDIATE = 1   # delete workspace files per artifact on completion (--cleanup)
    ALL          = 2   # superset of INTERMEDIATE + delete remaining dirs on success (--cleanup all)
```

- `>= INTERMEDIATE`: each phase checks `config.cleanup >= CleanupLevel.INTERMEDIATE` after marking an artifact `COMPLETE` and deletes its workspace files (e.g. encoding phase deletes `encoding/<strategy>/` attempt files after hard-linking).
- `>= ALL`: post-pipeline step checks `config.cleanup >= CleanupLevel.ALL` and deletes remaining intermediate directories on full success.
- Phases never delete `.tmp` files via cleanup — those are removed during `_recover()`.

## Logging Standards

Logging is split between job-level (orchestrator/CLI) and phase-level. All phase-specific messages originate from within the phase.

### Job-level (orchestrator / CLI)

Emitted before any phase runs. Covers pipeline intro, config summary, disk space check, and job init:

```
Starting automatic pipeline execution
Source:           <path>
Work directory:   <path>
CRF granularity:  0.5
Cropping:         automatic | manual (<params>) | disabled
Strategies:       <list or "using defaults">
Targets:          vmaf-min≥93.0, vmaf-median≥96.0
Work mode:        EXECUTE ALL PHASES | DRY-RUN

Source video size         18.30 GB
Estimated required space  122.59 GB
Available space           370.74 GB

✔ Sufficient disk space available

Initialized job.yaml for new pipeline run
Cropping: detecting black borders...
Detected cropping: 28 top, 28 bottom, 0 left, 0 right (content 1920x1024)
```

After all phases: overall pipeline outcome (success / failure / dry-run summary).

### Phase-level (inside each phase object)

Every phase follows this structure at `info` level:

**1. Phase banner** — emitted by the phase itself (not the orchestrator), using the shared `emit_phase_banner(name, logger)` helper from `pyqenc/utils/log_format.py`:
```
════════════════════════════════════════
EXTRACTION
════════════════════════════════════════
```

`JobPhase` is the exception: it does **not** emit a banner. Its output (source path, work dir, disk space, crop detection) is pipeline initialisation context, not phase-level work. The orchestrator/CLI is responsible for any pipeline-start header if one is desired.

**2. Key parameters** — one line per relevant parameter:
```
Source: movie.mkv
Include filter: .*eng.*
```

**3. Recovery / discovery result** — single line:
```
Recovery: 3 artifacts complete, 0 pending — reusing
Recovery: 0 artifacts complete, 6 pending — full run needed
Recovery: 4 artifacts complete, 2 pending — resuming
```

**4. Action plan** — what the phase is about to do. For extraction: the streams table. For chunking: scene count. For encoding: chunk × strategy matrix. Shown before work starts.

**5. Work progress** — the rule depends on step size:

- **Large steps** (operations that can take seconds to minutes: strategy test encodes, scene detection, crop detection, per-strategy merge, final quality measurement) — emit a start message at `info` so the user knows long work is ahead, then a completion message at `info`.
- **Small steps** (individual CRF attempts, individual audio task execution, per-file loudnorm passes) — emit only the completion message at `info`. No start message. Start messages for small steps go to `debug` only.

Example for encoding (small steps — no attempt start at info):
```
✔  chunk 00꞉00꞉00-00꞉01꞉30: CRF 18.5 — vmaf_min=94.2, vmaf_median=96.8
✘  chunk 00꞉01꞉30-00꞉03꞉00: CRF 20.0 — vmaf_min=91.3 (below target)
```

Example for optimization (large step — start message at info):
```
Testing strategy: slow+h265-anime
✔  chunk 00꞉00꞉00-00꞉01꞉30: CRF 18.5 — vmaf_min=94.2, vmaf_median=96.8
✔  chunk 00꞉01꞉30-00꞉03꞉00: CRF 20.0 — vmaf_min=93.1, vmaf_median=95.4
[strategy result block]
```

**6. Step / strategy completion summary** — table format for multi-item phases (optimization per-strategy, encoding per-strategy):
```
────────────────────────────────────────
Strategy result: slow+h265-anime
  Status    : ✅ PASSED
  Avg CRF   : 20.29
  Total size: 36.71 MB  (14 chunks)
────────────────────────────────────────
```

**7. Phase completion summary** — final table or key-value block:
```
════════════════════════════════════════
OPTIMIZATION SUMMARY
════════════════════════════════════════
  Optimal strategy : slow+h265

  Strategy                    Avg CRF   Size (MB)   Status
  --------------------------  -------  ----------  --------
  slow+h265                     20.50        36.1   passed ◀ optimal
  slow+h265-anime               20.29        36.7   passed
════════════════════════════════════════
```

### What goes to `debug`

- Start messages for small steps (individual CRF attempt start, individual audio task start, per-file loudnorm pass start)
- Sidecar write confirmations
- Individual file paths during scanning
- ffmpeg command lines

---



- Dependency prerequisites not met → phase logs `critical`, returns `FAILED` immediately.
- Source mismatch without `--force` → `JobPhase` returns `FAILED`, pipeline stops.
- Individual artifact failure → logged at `error` level; phase continues with remaining artifacts; returns `FAILED` if any artifact could not be completed.
- Unexpected exception in a phase → caught at phase level, logged at `critical`, returns `FAILED`.
- Orchestrator never catches phase exceptions — phases are responsible for their own error handling.

---

## Testing Strategy

- Unit test each phase's `_recover()` method with fixture directories covering all `ArtifactState` combinations.
- Unit test `JobPhase` source mismatch detection and `force_wipe` propagation.
- Unit test `OptimizationPhase` tolerance re-application from cached results.
- Unit test `EncodingPhase` quality target re-evaluation (COMPLETE → ARTIFACT_ONLY downgrade).
- Integration test: full pipeline run, interrupt mid-encoding, resume — verify only pending artifacts are re-processed.
- Integration test: crop param change mid-run with and without `--force`.
