# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-09
- Completed: 2026-09-11

## Cross-Spec Notes

### What this spec supersedes

| Superseded requirement | Original spec | What changed |
|---|---|---|
| `Phase` protocol exposes `scan()` as a no-side-effect classification pass distinct from `run()` | `phase-object-model` | `scan()` is **removed** from the `Phase` protocol entirely. A phase's `run()` (via internal recovery) is the single entry point; on an already-complete phase `run()` is inherently side-effect-free and returns `REUSED`. |
| `_ensure_dependencies(execute: bool)` calls `dep.scan()` when `execute=False` and `dep.run()` when `execute=True` | `phase-object-model`, `phase-recovery-refactor` | Dependencies are **always** resolved via `dep.run(dry_run=...)`; the `execute` / scan branch is gone. |
| `PipelineOrchestrator` iterates the full phase registry, calling `phase.run()` on every phase in order | `phase-object-model`, `pipeline-maturity-refactor` | Replaced by a **slim runner** that runs a single *target* (terminal) phase; dependency resolution lives in the phases. The runner never iterates to drive execution. |
| Post-pipeline `ALL` cleanup performed by the orchestrator, which reaches into extraction internals (`VideoArtifact` / `AudioArtifact`) to selectively prune `extracted/` | `phase-object-model` Req 12 | Deep cleanup is delegated to each phase via a `finalize()` hook. The runner decides *when* (terminal run, success, `ALL`); each phase deletes only its **own** artifacts. |
| Process-global active-collector registry (`register_active_collector` / `flush_active_collector` / module-level `_active_collector`) | `pipeline-metrics-report` | The run-scoped collector is **owned by the runner**; the process-global singleton is removed. Interrupt/cancel flushes via a set-based interrupt-flush registry (`flush_all_metrics`) each `YamlMetricsCollector` registers itself into. |
| Phase banners emitted as the first line of `run()`, before dependency resolution | `phase-object-model` | Banner is emitted by the phase **after** dependency resolution succeeds (dependencies neither `FAILED` nor `PENDING`), once, immediately before the phase engages its own recovery/work — for every non-skipped outcome. |

> The timeline is recoverable from the `Created` dates in each spec's header and, where absent, from filesystem timestamps. This spec is the most recent (2026-09-09) and takes precedence where it conflicts with the specs above.

## Introduction

The pipeline has two execution models today that have drifted apart:

1. The **orchestrator** (`auto` command) iterates the whole phase registry and calls `phase.run()` on each phase in order.
2. The **partial subcommands** (`extract`, `chunk`, `encode`, `audio`, `merge`) each call `phase.run()` on a single *terminal* phase, relying on that phase's `_ensure_dependencies` to recursively run upstream phases.

Model (2) is already the desired UX: the user picks the *work they want* (e.g. `audio`, `merge`), and the tool runs that terminal phase plus exactly its prerequisites, each once, with results cached and reused. This spec makes model (2) the single, uniform execution model for **every** command — including `auto` (whose terminal phase is `MergePhase`) — and removes the orchestrator's registry iteration.

Consolidating on one model exposes several cleanups that this spec also addresses:

1. **`scan()` is redundant.** It exists only to let a phase classify artifacts without triggering other phases' side-effects. Under the new model, prerequisites *should* run (respecting `dry_run`), and `run()` on an already-complete phase is side-effect-free by construction. `scan()` is removed from the protocol.
2. **Banners interleave.** Six of seven phases emit their banner as the first line of `run()`, *before* running dependencies — so a terminal phase's banner prints above its dependencies' banners. Banners must be emitted by the phase after its dependencies are resolved, right before it does its own work.
3. **Cleanup ownership is wrong.** The orchestrator performs `ALL` cleanup by reaching into extraction internals. Cleanup belongs to the phases: `INTERMEDIATE` during each phase's `run()`, `ALL` (the deep cross-phase sweep) via a phase `finalize()` hook broadcast by the runner only when the run reaches the true terminal.
4. **Metrics use a process-global singleton.** The active-collector registry breaks under an in-process multi-run server. The collector must be run-scoped and owned by the runner.
5. **Run summary is orchestrator-only.** Partial commands get no uniform run summary. The runner should produce a uniform summary from each phase's cached result outcome, for every command.

The end target beyond CLI is a client-server solution, so the design must not bake CLI assumptions (signal handling, process globals) into the core.

## Glossary

- **Runner**: The slim, phase-agnostic driver that replaces `PipelineOrchestrator`. Given a *target phase class*, it: runs that phase (dependency resolution happens inside the phases), owns the run-scoped metrics collector and its flush lifecycle, produces a uniform run summary, and broadcasts `finalize()` for deep cleanup when appropriate. It does not iterate the registry to drive execution and knows nothing phase-specific.
- **Target phase**: The terminal phase a command asks for. `auto` → `MergePhase`; `encode` → `EncodingPhase`; `chunk` → `ChunkingPhase`; `audio` → `AudioPhase`; `extract` → `ExtractionPhase`; `merge` → `MergePhase`.
- **Terminal-most phase**: `MergePhase` for the full video pipeline. Only a run whose target is the terminal-most phase is eligible for `ALL` deep cleanup.
- **Dependency resolution**: A phase ensuring its upstream phases have produced a cached result by calling `dep.run(dry_run=...)` on each dependency lacking one. Recursive; each phase runs at most once per registry instance.
- **`finalize(context)`**: New method on the `Phase` protocol. Called by the runner after a successful run to let each phase perform end-of-run housekeeping. It receives a **finalize context** carrying pre-resolved decision flags (today: whether to perform deep/`ALL` cleanup). Today its sole responsibility is `ALL`-level deep cleanup of that phase's own artifacts; the name and context are intentionally broad to accommodate future end-of-run concerns without changing the signature.
- **Finalize context**: A small typed object passed to `finalize()`. The runner computes each decision **once** and stores it as a resolved flag (e.g. `deep_cleanup: bool` = run succeeded AND target is terminal-most AND requested level is `ALL`). Phases read the flag directly and do not re-derive it from level + terminal indicator.
- **`INTERMEDIATE` cleanup (rolling)**: A phase deleting its own transient scratch **as it becomes safe to delete, during the run** — the primary goal is to minimize peak disk usage *while the phase runs*, not merely to tidy up at the end. It never deletes a consumable artifact a downstream phase might need. Available for all commands. Example: once the encoding phase has fully produced the winning attempt for a chunk, it deletes that chunk's losing/intermediate attempts immediately, so at most one chunk's worth of extra attempts exists on disk at any time while all completed chunks occupy only their winning-artifact footprint.
- **`ALL` cleanup**: The cumulative deep level. Implies `INTERMEDIATE` (in-phase) **plus** a final cross-phase sweep, via `finalize()`, of consumable artifacts that downstream phases might have needed. Only meaningful — and only performed — on a successful terminal-most run.
- **Run-scoped collector**: A `MetricsCollector` instance created and owned by the runner for exactly one run, threaded into the phase registry. Replaces the process-global active-collector registry.

---

## Requirements

### Requirement 1: Remove `scan()` from the phase protocol

**User Story:** As a developer, I want a single phase entry point, so that there is no confusion between "classify only" and "execute", and no self-referential scan subsystem to maintain.

#### Acceptance Criteria

1. THE `Phase` protocol SHALL NOT declare a `scan()` method.
2. EACH concrete phase SHALL remove its `scan()` method.
3. WHERE a phase previously called `dep.scan()` for a dependency without a cached result, the phase SHALL instead call `dep.run(dry_run=<current dry_run>)`.
4. THE `_ensure_dependencies` method of each phase SHALL NOT take an `execute` parameter and SHALL resolve every dependency uniformly via `dep.run(dry_run=...)`.
5. WHERE `MergePhase` previously called `EncodingPhase.scan()` to populate encoding results for quality-target re-evaluation and crop-mismatch detection, it SHALL obtain the same result via `EncodingPhase.run(dry_run=...)`, and that re-evaluation SHALL be performed by `EncodingPhase`'s recovery path.
6. WHEN `run()` is invoked (with no cached result) on a phase whose wanted artifacts are already all `COMPLETE` on disk, THE phase SHALL perform no file writes or deletions and SHALL return outcome `REUSED`.
7. THE `Phase.result` attribute SHALL cache the result of the `run()` call and SHALL be reused for the remainder of the current run.
8. WHEN `run()` is invoked on a phase that already has a cached `self.result`, THE phase SHALL return that cached result verbatim — performing no dependency resolution, no recovery, no work, no banner emission, and no outcome re-classification.
9. WITHIN a single run (one registry instance), EACH phase SHALL perform its dependency resolution, recovery, and own work at most once, regardless of how many downstream phases depend on it.

### Requirement 2: Dependency success is required; failures propagate uniformly

**User Story:** As a user, I want a phase to run only when all its prerequisites succeeded, so that I get a clear failure with the responsible dependencies rather than a phase running on incomplete inputs.

#### Acceptance Criteria

1. A phase SHALL engage its own work only WHEN every dependency it resolved returned a complete result (`COMPLETED` or `REUSED`).
2. WHEN any dependency did not complete successfully, THE phase SHALL NOT perform its own work and SHALL return a `FAILED` outcome.
3. THE resulting failure SHALL clearly identify every dependency that failed (by phase name), not merely the first one encountered.
4. THE dependency-failure handling SHALL be uniform across all phases — the same detection, logging level, and failure-propagation behavior for every phase.
5. WHEN a dependency fails, THE phase SHALL emit a warning/error that lists the failed dependencies before returning its own `FAILED` result.
6. THE failure SHALL propagate up the dependency chain so that the terminal phase (and thus the runner) reports `FAILED`.

### Requirement 3: Slim runner replaces registry-iterating orchestrator

**User Story:** As a developer, I want one uniform way to run any command, so that `auto` and the partial subcommands behave consistently and the runner stays ignorant of phase specifics.

#### Acceptance Criteria

1. THE runner SHALL accept a target phase class and execute the run by invoking `run(dry_run=...)` on that single target phase.
2. THE runner SHALL NOT iterate the phase registry to drive phase execution.
3. THE runner SHALL rely on the target phase's dependency resolution to run all prerequisite phases exactly once.
4. EVERY command (`auto`, `extract`, `chunk`, `encode`, `audio`, `merge`) SHALL execute through the same runner, differing only in the target phase class and the registry shape (e.g. `video_required=False` for `audio`).
5. THE runner SHALL propagate the `dry_run` flag unchanged to the target phase.
6. THE `PipelineOrchestrator` class SHALL be removed, and `PipelineResult` SHALL be produced by the runner (or an equivalent uniform result type).
7. THE public API functions SHALL construct and drive the runner rather than calling `phase.run()` directly or constructing a `PipelineOrchestrator`.

### Requirement 4: Uniform run summary from phase outcomes

**User Story:** As a user, I want a consistent end-of-run summary for every command, so that I always see which phases did work, reused artifacts, or need work.

#### Acceptance Criteria

1. AFTER a run completes, THE runner SHALL produce a summary derived solely from each phase's cached `result.outcome` across the registry.
2. THE runner SHALL classify phases by their `PhaseResult.outcome` across every possible outcome value — executed (`COMPLETED`), reused (`REUSED`), pending/needing-work (`PENDING`), and failed (`FAILED`) — with no outcome value silently omitted from the summary.
3. THE runner SHALL NOT read phase-specific internals to build the summary; it SHALL use only the common `PhaseResult` surface.
4. THE summary SHALL be emitted uniformly for all commands, not only `auto`.
5. WHERE a phase produces final output file paths (e.g. `MergePhase`), reporting those specific paths SHALL remain the responsibility of that phase, not the runner's uniform summary.
6. WHEN a run is a dry-run, THE summary SHALL report `PENDING` phases (those with wanted work remaining) distinctly from completed/reused phases.
7. WHEN a phase fails, THE summary SHALL identify the failed phase and its outcome, and THE runner SHALL surface the failure in the run result.

### Requirement 5: Phase-owned cleanup with cumulative levels

**User Story:** As a user, I want intermediate scratch cleaned up as phases run and a deep cleanup only when the whole job is done, so that partial runs never lose artifacts I still need.

#### Acceptance Criteria

1. THE cleanup levels SHALL be cumulative: `NONE` < `INTERMEDIATE` < `ALL`, where `ALL` implies all `INTERMEDIATE` behavior.
2. WHEN cleanup level is `INTERMEDIATE` or higher, EACH phase SHALL delete its own transient scratch on a **rolling** basis during its own `run()` — as soon as each piece of scratch is no longer needed — so that peak disk usage during the phase is minimized rather than deferred to the end.
3. WHERE a phase produces its output artifacts incrementally (e.g. per chunk), THE phase SHALL delete the transient inputs/intermediates of each unit as soon as that unit's final artifact is fully produced, so that at most one in-progress unit's transient footprint exists on disk at any time.
4. A phase's rolling (`INTERMEDIATE`) cleanup SHALL NOT delete any consumable artifact that a downstream phase might require; deletion of consumable artifacts SHALL be deferred to `finalize()`.
5. THE `Phase` protocol SHALL declare a `finalize(context)` method that lets a phase perform end-of-run housekeeping, receiving a finalize context carrying pre-resolved decision flags computed once by the runner (today: a single `deep_cleanup` flag). Its sole current responsibility is deleting that phase's own artifacts when the deep-cleanup flag is set.
6. EACH phase SHALL delete only its **own** artifacts in `finalize()`; the runner SHALL NOT name or reason about any phase-specific artifact or directory.
7. THE extraction phase's `finalize()` SHALL delete its own artifacts according to the effective cleanup level; because all extraction artifacts are reproducible from source, no extraction artifact is exempt from `ALL`-level deletion.

### Requirement 6: `ALL` cleanup gated to the terminal-most run

**User Story:** As a user, I want `--cleanup all` to be handled gracefully on partial commands, so that a convenient flag never destroys artifacts I still need and never penalizes me for using it.

#### Acceptance Criteria

1. THE runner SHALL compute a single `deep_cleanup` flag once — true only when the run completed successfully, the target phase is the terminal-most phase, and the requested cleanup level is `ALL` — and pass it in the finalize context.
2. THE runner SHALL invoke `finalize(context)` on phases only WHEN the run completed successfully, carrying the pre-computed `deep_cleanup` flag; phases perform deep cleanup only when that flag is set.
3. WHEN cleanup level `ALL` is requested but the target phase is not the terminal-most phase, THE runner SHALL downgrade the effective cleanup to `INTERMEDIATE` (i.e. `deep_cleanup` is false) and SHALL emit a clear warning explaining that full cleanup is not possible for this command and was downgraded.
4. THE runner SHALL NOT reject or error on `--cleanup all` for a partial command; it SHALL always resolve the situation itself via downgrade-and-warn.
5. WHEN the terminal phase (or any phase in the run) fails so the run does not complete successfully, THE `deep_cleanup` flag SHALL be false and THE runner SHALL NOT perform `ALL` deep cleanup.
6. WHEN the run is a dry-run, THE `deep_cleanup` flag SHALL be false and THE runner SHALL NOT perform `ALL` deep cleanup.

### Requirement 7: Run-scoped metrics collector owned by the runner

**User Story:** As a developer building toward a server, I want per-run metrics with no process-global state, so that concurrent or sequential runs never corrupt each other's metrics.

#### Acceptance Criteria

1. THE runner SHALL create and own a run-scoped `MetricsCollector` (or `NoOpMetricsCollector` when metrics are disabled) for exactly one run.
2. THE process-global active-collector registry (`register_active_collector`, `flush_active_collector`, and the module-level `_active_collector`) SHALL be removed.
3. THE runner SHALL thread its run-scoped collector into the phase registry as the sole collector for that run.
4. THE runner SHALL own the flush lifecycle: periodic incremental flushes during the run, a final flush on successful completion, and a flush on failure.
5. THE metrics collector for CLI runs SHALL remain per-work-directory and resumable across reruns (appending to the existing `metrics.yaml`).
6. WHEN a run is a dry-run, THE runner SHALL NOT write `metrics.yaml`.
7. THE interrupt/cancel path SHALL flush the current run's collector by reference to the runner (or its owned collector), not via a process-global lookup.
8. THE metrics collector lifecycle SHALL be independent of `JobPhase`; `JobPhase` SHALL remain concerned with processing parameters only.

### Requirement 8: Phase-emitted banners after dependency resolution

**User Story:** As a user, I want each phase's banner to appear right before that phase does its own work, so that banners and phase output never interleave on the console.

#### Acceptance Criteria

1. EACH phase SHALL emit its banner AFTER its dependencies have been resolved and confirmed complete, immediately before it engages its own recovery/work.
2. EACH phase SHALL emit its banner exactly once per `run()` invocation.
3. A phase SHALL emit its banner for every non-skipped outcome, including reuse-from-cache and dry-run.
4. WHEN a phase is completely skipped (e.g. `OptimizationPhase` disabled via `--no-optimize` or reduced to a single strategy, or a phase absent from the active registry), THE phase SHALL NOT emit a banner.
5. THE `OptimizationPhase` SHALL evaluate its skip decision before the banner and SHALL emit the banner exactly once across all non-skip branches (reuse, tolerance re-apply, and work).
6. WHEN a dependency is resolved during another phase's dependency resolution, THE dependency's banner SHALL be emitted at the point that dependency begins its own work, not before the phase that triggered it.

### Requirement 9: Behavior preservation

**User Story:** As a user, I want the refactor to change structure without changing what the pipeline produces, so that source fidelity and existing results are unaffected.

#### Acceptance Criteria

1. THE set of artifacts produced by each command SHALL be unchanged by this refactor.
2. THE dependency execution order for every command SHALL be unchanged.
3. THE `dry_run` reporting behavior (stop at first phase needing work, report without writing) SHALL be preserved.
4. THE quality-target re-evaluation and crop-mismatch detection previously triggered via `scan()` SHALL still occur through the equivalent `run()`/recovery path.
5. THE partial-elapsed-timer capture on interrupt (so in-flight timing is not lost) SHALL be preserved under the run-scoped collector.
6. THE source frame rate, audio sample rate, color/HDR metadata, timestamps, and container properties SHALL remain untouched by this refactor (no new ffmpeg invocations or filter changes are introduced).

### Requirement 10: `PhaseOutcome` represents work-state only, not run mode

**User Story:** As a developer, I want `PhaseOutcome` to describe only the state of a phase's work — never whether the run was a preview — so that completeness and dry-run mode are not conflated in one enum.

#### Acceptance Criteria

1. THE `PhaseOutcome` enum SHALL contain only work-state values: `COMPLETED`, `REUSED`, `PENDING`, and `FAILED`. The `DRY_RUN` value SHALL be removed.
2. THE `PENDING` value SHALL mean "the phase has wanted work remaining (one or more artifacts are `ABSENT` or `PARTIAL`)", independent of whether the run is a dry-run or an execute run.
3. WHERE outcome-derivation helpers previously returned `DRY_RUN` for artifacts in an incomplete state, THEY SHALL return `PENDING` and SHALL NOT take the run mode into account.
4. THE `PhaseResult.is_complete` property SHALL be `True` only for `COMPLETED` and `REUSED`; `PENDING` and `FAILED` SHALL both be not-complete, with no special-casing of a mode value.
5. WHEN a phase is invoked with `dry_run=True` and has wanted work remaining, THE phase SHALL return outcome `PENDING`.
6. WHEN a phase is invoked with `dry_run=False` (execute) and completes successfully, THE phase SHALL return `COMPLETED` or `REUSED` and SHALL NOT return `PENDING`; a `PENDING` outcome from an execute run SHALL be treated as a failure-to-progress by the runner.
7. THE run mode (dry-run vs execute) SHALL be owned by the runner (via the `dry_run` flag it already threads), not encoded into any phase's outcome.
8. WHERE the runner reports "phases needing work", it SHALL derive that bucket from `PENDING` outcomes combined with its own knowledge that the run was a dry-run.
