# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-20

## Introduction

This spec covers a structural refactor of the pipeline to make each phase a self-contained object. Currently phases are standalone functions, job initialisation only runs inside the auto-pipeline, input discovery is inconsistent across modes, and the pipeline always rescans the filesystem even when it just produced the data. The goal is a uniform `Phase` protocol where every phase owns its dependencies, artifact enumeration, recovery, execution, and logging — and the pipeline simply drives phase objects rather than containing phase logic itself.

## Glossary

- **Phase object**: An instance of a class implementing the `Phase` protocol; owns all knowledge about one pipeline phase.
- **Phase protocol**: The common interface (`dependencies`, `output_artifacts()`, `recover()`, `execute()`, `scan()`, `result`) that every phase must implement.
- **JobPhase**: A special phase that initialises `job.yaml` and resolves crop parameters; a dependency of every other phase.
- **Dependency chain**: The directed graph of phase objects where each phase holds references to its prerequisite phase objects.
- **Artifact**: A file produced by a phase (e.g. a chunk `.mkv` + its `.yaml` sidecar).
- **ArtifactState**: Four-value classification of a single artifact: `ABSENT` (file missing), `ARTIFACT_ONLY` (file present, sidecar missing), `STALE` (file and sidecar present but parameters changed — valid data, wrong parameters), `COMPLETE` (file and sidecar present, parameters match).
- **Recovery**: The process of scanning a phase's output directory, classifying each artifact, repairing incomplete sidecars, and returning the set of pending work.
- **Input discovery**: Verifying that prerequisite phases have produced complete artifacts; implemented by asking dependency phase objects for their results.
- **Live result passing**: When the pipeline runs phases sequentially, a completed phase's result is read directly by the next phase without rescanning the filesystem.
- **Standalone mode**: A phase invoked directly via CLI (e.g. `pyqenc chunk`) without the auto-pipeline driving it.
- **Auto-pipeline mode**: Full pipeline invoked via `pyqenc auto`; drives all phases in order.
- **Orchestrator**: The component that drives phase objects in sequence in auto-pipeline mode.
- **Phase-level logging**: All log messages describing a phase's own work, status, and results; must originate from within the phase object, not from the orchestrator.
- **Strategy**: A typed object representing an encoding configuration, carrying `name` (display form, e.g. `slow+h265-aq`) and `safe_name` (filesystem-safe form, e.g. `slow_h265-aq`).
- **Strategy selection tolerance**: A configurable percentage threshold; strategies whose total encoded size is within this percentage of the best strategy's size are also considered optimal. `0%` means exactly one strategy is selected.
- **All-strategies mode**: Pipeline mode where the optimization phase is bypassed — it returns all configured strategies silently without logging or running any test encodes.
- **Optimization mode**: Pipeline mode where the optimization phase runs test encodes and selects the best strategy (or strategies within tolerance).

---

## Requirements

### Requirement 1 — Phase Protocol

**User Story:** As a developer, I want every phase to implement a common interface, so that the orchestrator and CLI can drive phases uniformly without knowing their internals.

#### Acceptance Criteria

1. THE system SHALL define a `Phase` protocol with the following members: `name: str`, `dependencies: list[Phase]`, `result: PhaseResult | None`, `scan() -> PhaseResult`, `run(dry_run: bool) -> PhaseResult`.
2. WHEN a phase's `run()` is called, THE phase SHALL call `recover()` internally, then execute work for all pending artifacts, and return a result where all artifacts are `COMPLETE` (or `FAILED` if work could not complete).
3. WHEN a phase's `scan()` is called, THE phase SHALL enumerate and classify all artifacts in their current state — including `ABSENT` and `ARTIFACT_ONLY` — and return a result without executing any work or modifying any files.
4. THE system SHALL define a `PhaseResult` dataclass containing: `outcome: PhaseOutcome`, `artifacts: list[Artifact]`, `message: str`, `error: str | None`; the `outcome` SHALL be derivable from artifact states: all `COMPLETE` → `REUSED` or `COMPLETED` (depending on whether any work was done), any `FAILED` → `FAILED`, dry-run with pending → `DRY_RUN`.
5. WHEN a phase completes `run()` or `scan()`, THE phase SHALL cache the result in `self.result` so subsequent callers can read it without triggering another scan.

---

### Requirement 2 — JobPhase

**User Story:** As a developer, I want job initialisation (job.yaml, crop detection) to run in both auto-pipeline and standalone modes, so that all phases have access to stable job-level data regardless of how they are invoked.

#### Acceptance Criteria

1. THE system SHALL implement `JobPhase` as a `Phase` object with no dependencies.
2. WHEN `JobPhase.run()` is called, THE `JobPhase` SHALL validate the source video against any existing `job.yaml`, create or update `job.yaml` with source metadata, and resolve crop parameters (manual → cached → auto-detect).
3. WHEN `JobPhase.scan()` is called, THE `JobPhase` SHALL load and return the existing `job.yaml` state without running crop detection or writing any files.
4. THE system SHALL make `JobPhase` a declared dependency of every other phase object.
5. WHEN any phase is constructed for standalone use, THE phase SHALL construct a `JobPhase` instance as part of its dependency chain so job initialisation runs automatically.

---

### Requirement 3 — Dependency Chain and Input Discovery

**User Story:** As a developer, I want each phase to discover its inputs by asking its dependency phases, so that there is no separate `discover_inputs` function and no `standalone` flag on phase functions.

#### Acceptance Criteria

1. WHEN a phase's `run()` or `scan()` is called, THE phase SHALL call `dep.scan()` on each dependency that does not yet have a cached result, to obtain the dependency's current artifact state.
2. WHEN a dependency's `scan()` returns a result, THE phase SHALL check `dep.result.is_complete`; if `False`, THE phase SHALL log a critical message and return a `FAILED` outcome without executing any work.
3. THE system SHALL remove the `standalone: bool` parameter from all phase entry-point functions.
4. THE system SHALL remove the `discover_inputs()` function from `recovery.py` once all phases use the dependency chain for input discovery.
5. WHEN the auto-pipeline runs phases sequentially, THE orchestrator SHALL pass the already-executed phase object to the next phase as its dependency, so the next phase reads `dep.result` directly without calling `dep.scan()`.
6. EACH phase SHALL declare its specific dependencies by looking them up from a `dict[type[Phase], Phase]` registry passed at construction time; each phase SHALL store its dependencies as typed attributes internally (no `Any` or untyped lookups after construction).

---

### Requirement 4 — Artifact Enumeration and Recovery

**User Story:** As a developer, I want each phase to own its artifact enumeration and recovery logic, so that recovery is uniform and self-contained within the phase.

#### Acceptance Criteria

1. THE system SHALL define a typed `Artifact` base class (or generic) where each phase defines its own concrete artifact type with fully typed metadata fields, reusing existing metadata models (`ChunkMetadata`, `AudioMetadata`, `VideoMetadata`, etc.) where applicable.
2. WHEN a phase's `recover()` is called, THE phase SHALL enumerate its expected output artifacts, classify each as `ABSENT`, `ARTIFACT_ONLY`, `STALE`, or `COMPLETE`, repair any `ARTIFACT_ONLY` artifacts (e.g. write missing sidecars), and return the list of pending artifacts.
3. THE system SHALL move per-phase recovery functions from `recovery.py` into the corresponding phase objects.
4. WHEN `recovery.py` is emptied of per-phase logic, THE system SHALL remove the file or retain it only for shared low-level helpers (e.g. `_cleanup_tmp_files`, `_parse_chunk_timestamps`).
5. WHEN a phase finds leftover `.tmp` files in its output directory during recovery, THE phase SHALL delete them and log a warning.

---

### Requirement 5 — Artifact Invalidation

**User Story:** As a developer, I want each phase to own its invalidation logic using a `STALE` artifact state, so that parameter changes are handled with minimal re-work and cleanup of stale files is controlled by the user's cleanup level.

#### Acceptance Criteria

1. THE system SHALL add `STALE` as a fourth `ArtifactState` value, meaning the artifact file and sidecar are present and internally consistent, but the parameters under which they were produced no longer match the current run parameters.
2. WHEN a phase classifies an artifact as `STALE`, THE phase SHALL determine whether the artifact is still needed under the current parameters; if needed, THE phase SHALL re-produce it; if no longer needed, THE phase SHALL leave it on disk unless the active cleanup level permits deletion.
3. THE system SHALL implement source-file change detection in `JobPhase`; WHEN a source mismatch is detected in execute mode without `--force`, THE `JobPhase` SHALL return `FAILED` and all downstream phases SHALL not run.
4. WHEN `--force` is provided and a source mismatch is detected, THE `JobPhase` SHALL set `force_wipe: True` on its result and delete only `job.yaml` and phase parameter files; each downstream phase SHALL detect `force_wipe`, delete its own output directory and phase parameter YAML, then proceed as if starting fresh.
5. WHEN the extraction phase detects that the stored `include`/`exclude` filter differs from the current run parameters, THE extraction phase SHALL re-validate each artifact on disk against the new filter: artifacts that still match SHALL remain `COMPLETE`; artifacts that are now excluded SHALL be classified `STALE`; artifacts that should now be included but are missing SHALL be classified `ABSENT`.
6. WHEN the optimization or encoding phase detects that the stored crop params differ from the current run parameters, THE phase SHALL classify all its existing artifacts as `STALE` and re-produce them; without `--force` it SHALL return `FAILED` instead.
7. WHEN quality targets change between runs, THE encoding phase SHALL re-evaluate each existing encoded artifact against the new targets: artifacts that still pass SHALL remain `COMPLETE`; artifacts that no longer pass SHALL be classified `ARTIFACT_ONLY` (CRF search must continue from existing history).
8. WHEN `--force` is passed to a specific standalone phase command, THE phase SHALL treat all its own artifacts as `ABSENT` and re-execute from scratch.

---

### Requirement 6 — Encoding/Encoded Folder Split

**User Story:** As a developer and end-user, I want CRF search workspace files separated from finalized encoded artifacts, so that the `encoded/` folder contains only clean, selected results and the `encoding/` folder contains the CRF search history.

#### Acceptance Criteria

1. THE system SHALL use `encoding/<strategy.safe_name>/` as the working directory for CRF search attempts (intermediate attempt files, no final chunk sidecar).
2. THE system SHALL use `encoded/<strategy.safe_name>/` as the output directory for finalized artifacts: a hard-link to the winning attempt `.mkv` (preserving the original filename), the per-chunk result sidecar, and the winning attempt's quality graph — all with their original filenames preserved.
3. WHEN a CRF search for a `(chunk_id, strategy)` pair concludes with a winning attempt, THE encoding phase SHALL create a hard-link of the winning attempt `.mkv` from `encoding/<strategy.safe_name>/` into `encoded/<strategy.safe_name>/` using the same filename, and write the result sidecar and the winning attempt's quality graph alongside it.
4. WHEN the encoding phase performs recovery, THE phase SHALL scan `encoded/<strategy.safe_name>/` for complete artifacts (file + sidecar) and `encoding/<strategy.safe_name>/` for CRF history to resume incomplete searches.
5. WHEN the merge phase collects encoded chunks, THE merge phase SHALL read them from `EncodingPhase.result` directly (in pipeline mode) or scan `encoded/<strategy.safe_name>/` (in standalone mode); THE merge phase SHALL NOT scan `encoding/<strategy.safe_name>/`.
6. WHEN `--cleanup` (intermediate level) is active, THE encoding phase SHALL delete the CRF attempt files in `encoding/<strategy.safe_name>/` for each pair immediately after the winning attempt is hard-linked into `encoded/<strategy.safe_name>/`.

---

### Requirement 7 — Optimization Phase as Static Dependency

**User Story:** As a developer, I want the optimization phase to always be a declared dependency of the encoding phase, so that strategy selection is always explicit and the encoding phase never needs to know about the `all-strategies` vs `optimization` distinction.

#### Acceptance Criteria

1. THE encoding phase SHALL declare `OptimizationPhase` as a static dependency, regardless of whether optimization mode or all-strategies mode is active.
2. WHEN running in all-strategies mode, THE `OptimizationPhase` SHALL return all configured strategies without running any test encodes and without emitting any log messages visible to the user.
3. WHEN running in optimization mode, THE `OptimizationPhase` SHALL run test encodes and return the set of strategies whose total encoded size is within the configured tolerance percentage of the best strategy's size.
4. THE system SHALL support a configurable `strategy_selection_tolerance` percentage (default `5`, meaning within 5% of best); when set to `0`, exactly one strategy is returned.
5. THE system SHALL define a `Strategy` dataclass with at minimum: `name: str` (display name, e.g. `slow+h265-aq`), `safe_name: str` (filesystem-safe, e.g. `slow_h265-aq`); THE `OptimizationPhase.result` SHALL contain a `selected_strategies: list[Strategy]` field that the encoding phase reads directly.
6. THE `_optimal_strategy` instance variable on the orchestrator SHALL be removed; strategy selection SHALL live entirely on `OptimizationPhase.result`.
7. THE `OptimizationPhase` SHALL persist per-strategy test results (total encoded size, average CRF) to `optimization.yaml` after each strategy completes, ordered by increasing total size (most efficient first); WHEN the tolerance changes on a subsequent run and all strategy results are already persisted, THE `OptimizationPhase` SHALL re-apply the selection logic from cached results without re-encoding.

---

### Requirement 8 — Live Result Passing in Auto-Pipeline

**User Story:** As a developer, I want the auto-pipeline to pass live phase results to subsequent phases, so that phases do not rescan the filesystem for data that was just produced.

#### Acceptance Criteria

1. WHEN the orchestrator runs a phase and it succeeds, THE orchestrator SHALL pass the completed phase object (with its cached result) as the dependency for the next phase.
2. WHEN a phase receives a dependency with a cached result, THE phase SHALL read input artifacts from `dep.result` without calling `dep.scan()` or globbing the filesystem.
3. WHEN the encoding phase runs in auto-pipeline mode, THE encoding phase SHALL read selected strategies from `OptimizationPhase.result.selected_strategies` directly.

---

### Requirement 9 — Phase-Level Logging

**User Story:** As a developer, I want all phase-specific log messages to originate from within the phase object, so that the orchestrator only logs pipeline-level concerns (start, stop, overall outcome).

#### Acceptance Criteria

1. THE system SHALL ensure that every log message describing a phase's own work, progress, artifact status, or result is emitted from within the phase object's methods.
2. THE orchestrator SHALL only emit log messages for: pipeline start/stop, per-phase boundary markers (phase name header), and overall pipeline outcome.
3. WHEN a phase logs its outcome (completed, reused, failed), THE phase SHALL include the phase name and a human-readable summary in the log message.
4. THE system SHALL remove all phase-specific log messages from `orchestrator.py` that duplicate or describe work done inside a phase.

---

### Requirement 10 — Orchestrator Simplification

**User Story:** As a developer, I want the orchestrator to be a thin driver that constructs and runs phase objects, so that adding or reordering phases requires no changes to the orchestrator's internal logic.

#### Acceptance Criteria

1. THE orchestrator SHALL build a `dict[type[Phase], Phase]` registry of all phase objects at startup, in execution order, and pass it to each phase constructor; THE orchestrator SHALL drive sequential execution by iterating the registry values in insertion order.
2. WHEN executing the pipeline, THE orchestrator SHALL iterate the phase list and call `phase.run(dry_run)` on each, reading `phase.result` for outcome classification.
3. THE orchestrator SHALL NOT contain any phase-specific logic (artifact paths, sidecar formats, crop handling, strategy resolution).
4. WHEN a phase returns a `FAILED` outcome, THE orchestrator SHALL stop the pipeline and log the error at `critical` level.
5. WHEN a phase returns a `DRY_RUN` outcome in dry-run mode, THE orchestrator SHALL stop iteration and log a summary.

---

### Requirement 12 — Cleanup Levels

**User Story:** As a user, I want predictable, non-interactive cleanup behaviour controlled by a single flag, so that the pipeline can run unattended and I can choose how much intermediate storage to retain.

#### Acceptance Criteria

1. THE system SHALL remove the `--keep-all` flag and the interactive cleanup prompt at the end of the pipeline.
2. THE system SHALL introduce a `--cleanup` flag with an optional level argument: no flag means keep everything; `--cleanup` (no argument) means intermediate cleanup; `--cleanup all` means full cleanup on success.
3. WHEN `--cleanup` (intermediate level) is active and a phase marks an artifact as `COMPLETE`, THE phase SHALL delete the intermediate workspace files for that artifact (e.g. CRF attempt files in `encoding/` after the winning attempt is hard-linked to `encoded/`).
4. WHEN `--cleanup all` is active, THE system SHALL apply intermediate cleanup as in criterion 3, AND on full pipeline success SHALL also delete any remaining intermediate directories (`encoding/`, `encoded/`, `chunks/`, `extracted/`), retaining only `audio/` final delivery files and `final/` merged outputs with their sidecars and graphs.
5. THE cleanup logic SHALL be implemented within each phase (intermediate cleanup) or as a post-pipeline step (full cleanup), not in the orchestrator's main loop.
6. THE system SHALL NOT delete `.tmp` files via cleanup — those are always removed during recovery at phase startup.

---

### Requirement 11 — Standalone CLI Consistency

**User Story:** As a developer, I want standalone phase commands to behave consistently with the auto-pipeline, so that running `pyqenc chunk` produces the same job initialisation and recovery behaviour as running `pyqenc auto` up to the chunking phase.

#### Acceptance Criteria

1. WHEN a standalone phase command is invoked, THE CLI SHALL construct the phase object with its full dependency chain (including `JobPhase`) and call `phase.run()`.
2. WHEN `JobPhase.run()` is called in standalone mode, THE `JobPhase` SHALL perform the same job initialisation as in auto-pipeline mode.
3. THE `api.py` standalone functions SHALL construct phase objects and delegate to `phase.run()` rather than calling phase functions directly.
4. WHEN a standalone phase command is run in dry-run mode (no `-y`), THE phase SHALL call `phase.run(dry_run=True)` and report what work would be done without executing it.
5. THE system SHALL remove the `-y N` (max phases) option from the `auto` subcommand; partial pipeline execution SHALL be achieved by invoking the desired phase directly as a standalone command.
