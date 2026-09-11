# Implementation Plan — Phase Terminal Runner

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-09
- Completed: 2026-09-11

## Overview

This plan replaces the registry-iterating `PipelineOrchestrator` with a slim, phase-agnostic `Runner` that runs a single target phase; removes `scan()` from the phase protocol (dependency resolution always goes through `dep.run(dry_run=...)`); adds a `finalize(ctx)` hook so each phase owns its own `ALL`-level deep cleanup; moves rolling `INTERMEDIATE` cleanup inside each phase `run()`; makes the metrics collector run-scoped (deleting the process-global registry); moves banner emission to after dependency resolution; and decomposes `PhaseOutcome` so it carries work-state only (`DRY_RUN` → `PENDING`, run mode owned by the runner).

Tasks are sequenced foundational-first: the enum decomposition and protocol changes (`models.py`, `phase.py`) are the base; then the shared dependency-resolution helper; then per-phase `run()`/`finalize()` updates; then the new `Runner` and deletion of `orchestrator.py`; then metrics de-globalisation; then `api.py`/`cli.py` wiring; then tests; then the cross-spec review.

Implementation language: Python 3.13. Structural renames use the rope refactoring MCP where a pure identifier rename applies; string values, docstrings, and comments are updated manually. Tests use pytest with Hypothesis for property-based tests and assert observable behavior only, never internal state. Verify with `uv run ruff check` and `uv run python -m pytest` after each group.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1", "2"]},
    {"wave": 2, "tasks": ["3"]},
    {"wave": 3, "tasks": ["4"]},
    {"wave": 4, "tasks": ["5"]},
    {"wave": 5, "tasks": ["6"]},
    {"wave": 6, "tasks": ["7"]},
    {"wave": 7, "tasks": ["8"]},
    {"wave": 8, "tasks": ["9"]},
    {"wave": 9, "tasks": ["10"]}
  ]
}
```

Sequencing: tasks 1 (enum decomposition) and 2 (protocol change) are the independent foundation and can proceed in parallel (wave 1). Task 3 rewrites each phase `run()` onto both. Task 4 layers cleanup onto the new phase shape. Task 5 builds the runner and removes the orchestrator once phases no longer need it. Metrics de-globalisation (6) then wiring (7) follow, then tests (8), verification (9), and the cross-spec review + completion (10).

## Tasks

- [x] 1. Decompose `PhaseOutcome`: work-state only (`DRY_RUN` → `PENDING`)
  - [x] 1.1 Rename the enum member and make it mode-free
    - In `pyqenc/models.py`, rename `PhaseOutcome.DRY_RUN` to `PhaseOutcome.PENDING` using the rope MCP `rename_symbol` tool so the identifier and all references/imports update project-wide in one operation; do NOT hand-edit each callsite
    - Manually change the enum VALUE string from `"dry_run"` to `"pending"` (rope renames the identifier, not the string value)
    - Update the `PhaseOutcome` docstring: `PENDING` means "wanted work remains (an artifact is `ABSENT` or `PARTIAL`)", independent of run mode; the enum describes work-state only, never whether the run was a preview
    - Run `uv run ruff check pyqenc/models.py --fix`
    - _Requirements: 10.1, 10.2, 10.7_

  - [x] 1.2 Make outcome-derivation helpers and `is_complete` mode-free
    - In `pyqenc/phases/merge.py` and `pyqenc/phases/extraction.py`, change each `_outcome_from_artifacts` to return `PENDING` (not the old `DRY_RUN`) when any artifact is `ABSENT`/`PARTIAL`; return `COMPLETED`/`REUSED` (by `did_work`) when all are `COMPLETE`; the helper takes no run-mode input
    - Confirm `PhaseResult.is_complete` remains `outcome in (COMPLETED, REUSED)` — now a clean partition with `PENDING` and `FAILED` both not-complete, no mode special-case
    - Run `uv run ruff check pyqenc/phases/merge.py pyqenc/phases/extraction.py --fix`
    - _Requirements: 10.2, 10.3, 10.4_

- [x] 2. `Phase` protocol: remove `scan()`, add `finalize(ctx)` and `FinalizeContext`
  - [x] 2.1 Add `FinalizeContext` and update the `Phase` protocol in `pyqenc/phase.py`
    - Add a frozen dataclass `FinalizeContext` with a single field `deep_cleanup: bool`, docstringed as a pre-resolved decision computed once by the runner (phases read it, never re-derive)
    - Remove the `scan()` method from the `Phase` `Protocol`; add `finalize(self, ctx: FinalizeContext) -> None` documented as end-of-run housekeeping whose current sole job is deleting the phase's own artifacts when `ctx.deep_cleanup` is set
    - Update the `phase.py` module docstring to reflect the single `run()` entry point and the new `finalize` hook
    - Run `uv run ruff check pyqenc/phase.py --fix`
    - _Requirements: 1.1, 5.5, 6.1_

  - [x] 2.2 Add the shared `resolve_dependencies` helper in `pyqenc/phase.py`
    - Add a module-level helper `resolve_dependencies(phase, *, dry_run) -> list[str]` that, for each `dep` in `phase.dependencies`, calls `dep.run(dry_run=dry_run)` when `dep.result is None`, then appends `dep.name` to a failed list when the dep is missing a result or not `is_complete`; returns the list of failed dependency names (empty = proceed)
    - Docstring it as the uniform dependency walk + failure aggregation used by every phase (Req 2)
    - _Requirements: 1.3, 1.4, 2.1, 2.3, 2.4_

- [x] 3. Rewrite every phase to the uniform `run()` skeleton (remove `scan()`, add `finalize`)
  - [x] 3.1 Establish the uniform `run()` order and `_ensure_dependencies` wrapper per phase
    - For each phase (`job`, `extraction`, `probe`, `chunking`, `optimization`, `encoding`, `merge`, `audio`), delete the `scan()` method entirely and move its `if self.result is not None: return self.result` guard to the TOP of `run()` (before dependency resolution and before the banner) — this is the in-run memoization guard (Property 1)
    - Rewrite each phase's `_ensure_dependencies` to take `dry_run` (drop the `execute` parameter): verify required deps are wired, call `resolve_dependencies(self, dry_run=dry_run)`, and when the returned failed list is non-empty log ONE error naming every failed dependency and return the phase's own typed `FAILED` result via its existing `_failed(...)` constructor; otherwise return `None`
    - Replace every former `dep.scan()` / `if execute: dep.run() else: dep.scan()` branch with the single `resolve_dependencies` walk
    - Update each phase's dry-run return path to use `PENDING` (was `DRY_RUN`) when wanted work remains
    - _Requirements: 1.2, 1.3, 1.4, 1.6, 2.1, 2.2, 2.5, 2.6, 10.5_

  - [x] 3.2 Move each phase banner to after dependency resolution, emitted once
    - In each phase `run()`, place `emit_phase_banner(...)` AFTER `_ensure_dependencies` returns success and BEFORE the phase's own recovery/work; ensure it is emitted for every non-skipped outcome (work, reuse, dry-run) and exactly once per `run()` call
    - Confirm the memoization guard (task 3.1) precedes the banner so a re-reached dependency never re-banners
    - _Requirements: 8.1, 8.2, 8.3, 8.6_

  - [x] 3.3 Restructure `OptimizationPhase` skip/banner ordering
    - In `pyqenc/phases/optimization.py`, evaluate the skip decision first (`optimize` disabled OR single strategy → `_run_all_strategies`, no banner); after the memoization guard and skip check, emit the `OPTIMIZATION` banner exactly ONCE, shared across the reuse, tolerance-reapply, and work branches (remove the duplicated per-branch banner emissions)
    - _Requirements: 8.4, 8.5_

  - [x] 3.4 Bring `MergePhase` to the uniform `_recover`-driven pattern (no `scan()`, no driving another phase's `run()`)
    - `MergePhase` must recover its own internal artifact state exclusively through its own `_recover()` — exactly like every other phase. `run()` calls `_recover()` to get the merge artifact statuses; it must NOT use `scan()` (removed) and must NOT call another phase's `.run()` to recover state
    - The uniform `run()` order is already in place (memoization guard → `_ensure_dependencies` → banner → `_recover` → work). `_ensure_dependencies` already resolves `EncodingPhase` once via the shared `resolve_dependencies` walk (and additionally guards that every `EncodingPhase.result.encoded` artifact is `COMPLETE`), so by the time `_recover()` runs, `self._encoding.result` is guaranteed populated
    - Delete `_ensure_encoding_result()` and its two call sites. `_get_expected_strategies` and `_collect_encoded_chunks` must simply READ the already-cached `self._encoding.result.encoded` — this is the list of winning encoding attempts (artifacts with `state == COMPLETE`), which is the exact input the merge acts on. Keep the existing defensive `if self._encoding.result is None: return [] / {}` guard in each helper (it must not re-drive `.run()`)
    - The quality-target re-evaluation and crop-mismatch detection are owned by `EncodingPhase._recover()` and already applied during that single dependency `run()`; `MergePhase` consumes the resulting cached winning-attempt states and does not re-derive them
    - Update the docstrings on `_get_expected_strategies` / `_collect_encoded_chunks` that mention "standalone mode calls `EncodingPhase.run()`" — that path no longer exists; they always read the cached dependency result
    - _Requirements: 1.5, 9.4_

- [x] 4. Add rolling `INTERMEDIATE` cleanup and `finalize(ctx)` deep cleanup to each phase
  - [x] 4.1 Implement rolling intermediate cleanup inside phase `run()`
    - In `pyqenc/phases/encoding.py`, when `cleanup >= INTERMEDIATE`, delete a chunk's losing/intermediate attempts immediately after that chunk's winning attempt is fully produced (rolling, per-unit) so at most one in-progress chunk's extra footprint exists on disk; never delete the winning artifact
    - Audit the other phases: where any phase currently defers its own transient-scratch deletion to the end of `run()`, move it to the earliest safe point; ensure rolling cleanup never deletes a consumable a downstream phase needs
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 4.2 Implement `finalize(ctx)` on every phase
    - Add `finalize(self, ctx)` to each phase: when `ctx.deep_cleanup` is True, delete only that phase's OWN artifacts — `EncodingPhase` deletes `encoding/` and `encoded/`; `ChunkingPhase` deletes `chunks/`; `ExtractionPhase` deletes its extracted artifacts (all reproducible from source, none exempt); `Job`/`Probe`/`Optimization`/`Audio`/`Merge` are no-ops or delete only their own scratch
    - Each `finalize` catches `OSError` per artifact and logs a WARNING; a cleanup failure never raises
    - _Requirements: 5.5, 5.6, 5.7, 6.2_

- [x] 5. Create the `Runner` and delete `orchestrator.py`
  - [x] 5.1 Add `RunResult` and `Runner` in `pyqenc/runner.py`
    - Create `pyqenc/runner.py` with a `RunResult` dataclass (`success`, `outcomes`, `phases_executed`, `phases_reused`, `phases_needing_work`, `phases_failed`, `output_files`, `error`) and a `Runner` holding `registry`, `target`, `collector`, `work_dir`, `cleanup`, `no_metrics`, `is_terminal_most`
    - `Runner.run(dry_run)` runs ONLY the target via `self._registry[self._target].run(dry_run=...)` (no registry iteration to drive execution); if `not dry_run and result.outcome == PENDING`, convert to a failed result ("phase did not progress to completion"); set `run_ok = result.is_complete`
    - Move `_collect_output_files` from `orchestrator.py` into `runner.py` (final-directory artifact filter) and expose a `flush_metrics()` pass-through to `collector.flush()`
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 10.6_

  - [x] 5.2 Implement the uniform summary and cleanup gating in `Runner`
    - Add `_build_summary()` that reads `phase.name` + `phase.result.outcome` for every phase with a cached result and buckets across ALL `PhaseOutcome` values (`COMPLETED`/`REUSED`/`PENDING`/`FAILED`), dropping none; take `output_files` from the target phase's result only
    - Compute `deep_cleanup` ONCE (`run_ok and not dry_run and is_terminal_most and cleanup >= ALL`); when `cleanup >= ALL and not is_terminal_most`, log the downgrade warning (no rejection); on success and not dry-run, broadcast `phase.finalize(FinalizeContext(deep_cleanup=...))` to every registry phase
    - Log the uniform run summary for every command; keep final-file reporting in the phase, not the summary
    - _Requirements: 3.4, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 5.3 Delete `pyqenc/orchestrator.py`
    - Remove `PipelineOrchestrator`, `PipelineResult`, `_run_post_pipeline_cleanup`, and `_cleanup_extracted`; confirm nothing outside tests imports them (their behavior now lives in `Runner` + phase `finalize`)
    - _Requirements: 3.6, 5.5, 5.6_

- [x] 6. De-globalise metrics
  - [x] 6.1 Remove the process-global active-collector registry from `pyqenc/metrics.py`
    - Delete `_active_collector`, `register_active_collector`, `flush_active_collector`, and their two `__all__` entries; leave the `MetricsCollector` protocol, `YamlMetricsCollector` (incremental flush + `_snapshot_active_timers`), and `NoOpMetricsCollector` unchanged
    - Correct the stale `PipelineMetrics` docstring line that references `parallelism` "written separately by the pipeline orchestrator"
    - Run `uv run ruff check pyqenc/metrics.py --fix`
    - _Requirements: 7.1, 7.2_

- [x] 7. Wire `api.py` and `cli.py` to the runner
  - [x] 7.1 Rebuild `pyqenc/api.py` around a shared `_drive` builder returning `RunResult`
    - Add an internal `_drive(config, source, work_dir, target, *, force, cleanup, no_metrics, dry_run, crop_params=None, video_required=True)` that validates source, ensures work_dir, constructs the run-scoped collector (`YamlMetricsCollector` unless `no_metrics`), builds the registry via `_build_registry`, constructs the `Runner` with `is_terminal_most=(target is MergePhase)`, and returns `runner.run(dry_run=dry_run)`
    - Make `run_pipeline`, `extract_streams`, `chunk_video`, `process_audio` (`video_required=False`), `encode_chunks`, `merge_final` thin wrappers selecting the target class; each returns `RunResult`; remove the old `_run_phase` and the direct `PipelineOrchestrator` construction
    - Ensure incremental flush during the run, final flush on success, flush on failure are owned by the runner (Req 7.4); no `metrics.yaml` write on dry-run
    - _Requirements: 3.4, 3.7, 7.1, 7.3, 7.4, 7.5, 7.6, 7.8_

  - [x] 7.2 Update `pyqenc/cli.py` handlers, add the collector-owned interrupt-flush registry, and wire the SIGINT path
    - Switch each command handler (`_cmd_auto`/`extract`/`chunk`/`encode`/`audio`/`merge`) to check `RunResult.success` (was `result.is_complete`); update `_cmd_merge`'s per-file listing to read `result.output_files` (the final/ paths on `RunResult`) instead of `result.artifacts`
    - In `pyqenc/metrics.py`: add a set-based interrupt-flush registry symmetric with ffmpeg's `_live_procs`/`kill_all_ffmpeg` — a module-level `threading.Lock` + `set[YamlMetricsCollector]` and a `flush_all_metrics()` function that snapshots the set under the lock and calls `.flush()` on each (each call wrapped in try/except, never raises). Export `flush_all_metrics` in `__all__`. `YamlMetricsCollector.__init__` registers `self` into the set (under the lock); add an idempotent `close()` method that removes `self` from the set (under the lock). Add `close()` to the `MetricsCollector` protocol and a no-op `close()` to `NoOpMetricsCollector` (which does NOT register). `flush()` already snapshots in-flight timers, so no flush-logic change is needed.
    - In `pyqenc/runner.py`: after the existing final-flush block, always call `self._collector.close()` (on every path — success, failure, dry-run — so the collector unregisters from the interrupt registry). REMOVE the now-dead `Runner.flush_metrics()` method (the collector owns its own interrupt flush; nothing reaches through the runner).
    - In `pyqenc/cli.py` SIGINT handler: replace the removed `flush_active_collector()` call with `flush_all_metrics()` (import from `pyqenc.metrics`). Keep the handler's `kill_all_ffmpeg()` → `flush_all_metrics()` → `logger.warning` → `os._exit(130)` order. No active-runner holder, no single slot. `signal`/`os._exit` stay in cli.py; `metrics.py` imports no `signal`.
    - _Requirements: 7.1, 7.2, 7.7, 9.5_

- [ ] 8. Tests (observable behavior; each targets a concrete regression)
  - [ ]* 8.1 `scan()` removal and cross-run recovery
    - Given a fully-complete work dir, `phase.run(dry_run=False)` writes/deletes nothing and returns `REUSED`; changing a quality target then running merge still applies re-evaluation via `run()`
    - **Validates: Requirements 1.6, 9.1, 9.4**

  - [ ]* 8.2 In-run memoization (Property 1)
    - **Property: a shared dependency runs at most once per registry**
    - Within one registry, invoking a shared dependency's `run()` twice returns the identical cached result object, emits its banner only once, and does not re-resolve its dependencies
    - **Validates: Requirements 1.6, 3.1, 3.2, 3.3, 8.2**

  - [ ]* 8.3 Dependency-failure propagation (Property 3)
    - **Property: a phase with a failed dependency fails, naming every failed dep**
    - A phase whose dependency fails returns `FAILED`, its error names EVERY failed dependency, and the terminal `RunResult.success` is `False`
    - **Validates: Requirements 2.2, 2.3, 2.6**

  - [ ]* 8.4 Uniform runner across commands
    - Each command (`extract`/`chunk`/`encode`/`audio`/`merge`/`auto`) produces the same artifacts through the runner as before; the runner drives only the target phase
    - **Validates: Requirements 3.1, 3.3, 3.4, 9.1, 9.2**

  - [ ]* 8.5 Summary totality (Property 8)
    - **Property: every phase outcome maps to exactly one summary bucket**
    - A mixed run (COMPLETED, REUSED, one PENDING, one FAILED) surfaces every phase in the correct bucket with none dropped
    - **Validates: Requirements 4.1, 4.2**

  - [ ]* 8.6 Cleanup gating and safety (Property 4)
    - **Property: deep cleanup only on successful terminal-most execute run**
    - `encode --cleanup all` on a non-terminal target logs the downgrade warning, keeps `chunks/`/`encoded/`, calls no deep cleanup; `auto --cleanup all` on success removes `chunks/`/`encoding/`/`encoded/` and extraction artifacts; a failed `auto --cleanup all` preserves artifacts; dry-run never deep-cleans
    - **Validates: Requirements 6.1, 6.3, 6.5, 6.6**

  - [ ]* 8.7 Rolling intermediate cleanup bound (Property 5)
    - **Property: peak transient footprint is bounded to one in-progress unit**
    - With `--cleanup` (intermediate), after N chunks encode only one chunk's extra attempts exist on disk at any observed point
    - **Validates: Requirements 5.2, 5.3**

  - [ ]* 8.8 No process-global metrics state (Property 9) and interrupt flush
    - **Property: no module-global collector reference remains**
    - Importing `pyqenc.metrics` shows the removed names are gone; the SIGINT-path flush writes `metrics.yaml` with partial timing via the runner-owned collector
    - **Validates: Requirements 7.1, 7.2, 9.5**

  - [ ]* 8.9 Banner ordering (Property 7)
    - Running a terminal phase emits dependency banners before the terminal banner, each exactly once; a skipped Optimization (`--no-optimize`) emits no banner
    - **Validates: Requirements 8.1, 8.2, 8.4, 8.6**

  - [ ]* 8.10 Outcome is work-state only; PENDING never final on execute (Property 11)
    - **Property: PENDING never survives a successful execute run**
    - A phase with pending work returns `PENDING` in both modes before work runs; after a successful execute run no phase's cached outcome is `PENDING`; `is_complete` is True only for `COMPLETED`/`REUSED`
    - **Validates: Requirements 10.1, 10.2, 10.4, 10.6**

  - [x] 8.11 Migrate and clean up the existing test surface to the new API (observable-behavior only)
    - This is a thorough test-surface review, NOT just a compile-fix. Governing rules (project standard): tests assert OBSERVABLE BEHAVIOR only — never internal state, never private methods/attributes, never monkeypatching internals; each test must correspond to a concrete bug it prevents (state the bug); do NOT test defaults or trivial getters; no bloat/duplicate assertions; construct objects through their real public constructors/registry, drive through `run()` / the public API, and assert on the returned `PhaseResult`/`RunResult` and on-disk artifacts.
    - Baseline (captured): 2 collection-error files + 23 failures, all test-side (no production regressions). Dispositions:
      - DELETE `tests/test_metrics_orchestrator.py` — tests the deleted `PipelineOrchestrator` and removed collector globals; superseded by the runner design + PBTs 8.8/8.9.
      - MIGRATE `tests/e2e/test_complete_pipeline.py` — drive the full pipeline via the public API (`run_pipeline`/`api._drive`) or `Runner` instead of `PipelineOrchestrator`; keep genuine e2e dry-run/execute coverage, drop any internals assertions.
      - `tests/unit/test_job_phase.py`: DELETE the 3 `TestJobPhaseScan` tests (`scan()` removed); MIGRATE `test_dry_run_absent_returns_dry_run` to assert `PENDING` (rename accordingly). Audit the rest of the file for internals-poking / default-testing and trim.
      - `tests/unit/test_optimization_phase.py`: DELETE `test_scan_returns_all_strategies_silently` (`scan()` removed); all-strategies "no banner" behavior is PBT 8.9's job. Audit the file for internals-poking and trim.
      - REWRITE `tests/unit/test_probe_phase.py` — stop using `ProbePhase.__new__` + hand-set private attrs + monkeypatched `_ensure_dependencies`; build `ProbePhase` via its real constructor with a real registry (real `JobPhase`/`ExtractionPhase` producing their results, or their real typed results), drive `run()`, and assert only on `ProbePhaseResult` + on-disk `probe.yaml`. Keep the real-behavior guards (FAILED-on-no-video, REUSED-when-cached, crop-override-bypasses-reuse, COMPLETED-persists-probe.yaml); drop implementation-detail assertions (e.g. `isinstance ExtendedVideoMetadata`, trivial `error len>0`).
      - REWRITE `tests/test_pts_preservation_properties.py` — stop calling `phase._recover(execute=...)` (private, removed param) and stop constructing phases in ways that skip `__init__`; exercise timestamp/PTS classification through the public `run()`/result surface. Preserve the genuine property (timestamp artifact wanted/present classification, frame-count/PTS preservation) as observable behavior.
      - FIX `tests/test_metrics_integration.py` — the chunking-timing test passes a bare `Mock()` chunk lacking `.path`; drive real chunk metadata through the public surface (or a real `ChunkMetadata`) rather than poking `_execute_chunking`. Keep the observable guarantee (a metrics step is recorded when chunks are actually split).
    - Also sweep the WHOLE `tests/` tree (not only failing files) for the same anti-patterns introduced by earlier specs: `.scan(` calls, `PhaseOutcome.DRY_RUN`, `_ensure_dependencies(execute=...)`, direct private-method/attr access on phases, and default-value tests — migrate or delete per the rules above.
    - Verify with `uv run ruff check tests` and `uv run python -m pytest` (full suite must collect with no import errors and pass).
    - _Requirements: 9.1, 9.2, 9.4_

  - [x] 8.12 Remove the dead `execute` parameter from phase `_recover()` and rewrite the last two internals-poking tests
    - Production dead-code (leftover from `scan()` removal): every phase now calls `self._recover(force_wipe=..., execute=True)` from `run()`; `execute=False` (the old read-only scan mode) has NO remaining production caller. Remove the `execute` parameter entirely from `_recover` on all 5 phases that have it (`extraction`, `merge`, `encoding`, `chunking`, `audio`) and update their single `run()` call sites to `self._recover(force_wipe=...)`. The `.tmp`-cleanup/force-wipe steps that were previously gated on `execute` now always run (they were always run in production anyway, since `execute=True` was the only production value) — confirm no logic depended on `execute=False` other than the deleted scan path.
    - REWRITE `tests/unit/test_extraction_pts.py` — replace every `ExtractionPhase.__new__` + hand-set private attrs + `phase._recover(execute=...)` with real construction via the registry (real `JobPhase` with a pre-set COMPLETED `JobPhaseResult`, then `ExtractionPhase(config, registry, video_required=..., collector=...)`) driven through `phase.run(dry_run=...)`, asserting on the public `ExtractionPhaseResult` (+ on-disk `extracted/`), mirroring the pattern established in the rewritten `tests/test_pts_preservation_properties.py`. Mock only the external `MKVTrackExtractor`. Preserve the genuine behaviors each test guards (timestamp/stream artifact classification, force-wipe deleting artifacts, filter selection); drop internals-poking and any default/trivial assertions.
    - REWRITE `tests/unit/test_merge_mkvmerge.py` — replace `MergePhase.__new__` + private-attr/`_recover`/`_execute_merge` poking with a real `MergePhase` built via the registry with pre-set COMPLETED dependency results (mirroring the rewritten Property 4 in `tests/test_pts_preservation_properties.py`), driven through `merge.run(dry_run=...)`; mock only external `subprocess.run` (mkvmerge) / `get_frame_count`. Preserve the mkvmerge options-building / concat behaviors under test as observable outcomes; drop internals-poking.
    - Verify `uv run ruff check tests pyqenc` and `uv run python -m pytest` (full suite green, no collection errors); confirm ZERO `Phase.__new__`, `._recover(` calls, or `execute=` in tests, and ZERO `execute` params on `_recover` in pyqenc.
    - _Requirements: 1.6, 9.1, 9.4_

- [x] 9. Full verification
  - [x] 9.1 Run the full quality gate and a real dry-run/execute smoke check
    - Run `uv run ruff check pyqenc tests` and `uv run python -m pytest`; fix any regressions
    - Smoke-check each subcommand in dry-run against a sample from `steering/environment.md` (source in `D:\_encoding\source\*.mkv`, `--work-dir D:\_encoding\pyqenc`) confirming banners appear once and in dependency order and the uniform summary prints; do NOT use pipes (preserves the progress bar)
    - _Requirements: 8.1, 8.6, 9.1, 9.2, 9.3, 9.6_

  - [x] 9.2 Fix dry-run: JobPhase gates only its write, distinguish PENDING vs FAILED in the dependency walk, and make the banner rule uniform (bug found by 9.1 smoke check)
    - BUG (regressions the smoke check caught): (1) on a fresh work-dir every DRY-RUN cascaded to `FAILED`/`exit 1`, because `JobPhase.run(dry_run=True)` returned `PENDING` (job.yaml absent) and the shared `resolve_dependencies` bucketed `PENDING` with `FAILED`; (2) even after distinguishing them, a dry-run that stops at Job is useless to the user — Job is run SETUP, not pipeline work; (3) `OptimizationPhase` emitted its banner even when its dependencies were pending/failed, unlike every other phase (non-uniform banner).
    - FIX A — JobPhase dry-run gates ONLY the write: in `pyqenc/phases/job.py`, dry-run must still do everything read-only (source-mismatch check, load cached job.yaml, self-heal/invalidation, probe source metadata) and return a fully-populated in-memory `JobState` with an `is_complete` outcome (`REUSED` when an existing valid job.yaml was loaded, `COMPLETED` when it probed fresh) — only the `job.yaml` WRITE is skipped under `dry_run`. Remove the dry-run `PENDING`/"Would create job.yaml" branch. Net effect: Job is "done enough" for a dry-run so the chain proceeds and each downstream phase reports ITS OWN state.
    - FIX B — distinguish PENDING vs FAILED in the shared walk (chain the same status): change `resolve_dependencies(phase, *, dry_run)` in `pyqenc/phase.py` to return a small frozen `DependencyStatus(failed: list[str], pending: list[str])` (failed = outcome FAILED or missing result; pending = outcome PENDING, dependency order). Each phase's `_ensure_dependencies(dry_run)`: (a) any FAILED dep → log ONE ERROR `"<Phase> cannot run — failed dependencies: <names>"`, return typed `FAILED` (chains FAILED); (b) else any PENDING dep → log ONE INFO `"<Phase> dry-run is impossible — work still pending at: <names>"`, return typed `PENDING` (chains PENDING, no error); (c) else `None`. Names capitalized. Add a `_pending(reason)` constructor mirroring `_failed(error)` where the module helper exists (merge, optimization, audio, chunking); build PENDING inline where FAILED is inline (extraction, probe, encoding). No count-based dedup — rely on top-to-bottom ordered logs. (Note: with FIX A, Job no longer emits PENDING, so on a fresh dry-run Extraction is the first phase to report itself PENDING — the useful signal.)
    - FIX C — uniform banner rule (supersedes part of task 3.3): the banner is emitted by a phase ONLY immediately before it begins ACTUAL WORK. No banner when the phase short-circuits with no work: FAILED/PENDING dependency short-circuit, OR the OptimizationPhase all-strategies skip (`--no-optimize` / single strategy → returns all-strategies result, no work). Reorder `OptimizationPhase.run()` to the uniform skeleton: memoization guard → skip check (`_run_all_strategies`, no banner) → `_ensure_dependencies(dry_run)` (may return FAILED/PENDING, NO banner) → load persisted + param-change detection → emit the single `OPTIMIZATION` banner (deps now complete; cached-reuse / tolerance-reapply / real-encode all count as work → banner, per Req 8.3) → the cached/tolerance/work branches. This moves dependency resolution BEFORE the banner and the cached-reuse/tolerance branches (previously they resolved from optimization.yaml without live deps and banner'd first — that was the non-uniformity). Chaining/PENDING-FAILED distinction is untouched; only banner placement + dep-resolution order change.
    - The runner's execute-mode guard (target `PENDING` on `dry_run=False` → failure-to-progress) is unchanged.
    - Re-run dry-run smoke checks for every subcommand on a FRESH work-dir: confirm no ERROR/CRITICAL, Job does NOT block the preview, the FIRST phase that would do real work reports itself PENDING (INFO), banners appear only before actual work (none for pending/skipped phases — including Optimization), the uniform summary reports phases-needing-work, exit 0. Also confirm a genuine FAILED dependency (e.g. source mismatch without --force in execute) still chains FAILED with ERROR.
    - Update/extend tests (observable behavior): fresh-work-dir dry-run of a terminal command → `RunResult.success is False`, non-empty `phases_needing_work`, empty `phases_failed`; JobPhase dry-run on a fresh dir returns an `is_complete` result carrying a populated `job` and writes NO job.yaml; a genuine dependency failure still chains `FAILED`. Full suite green.
    - Out-of-band bugfix (pre-existing, surfaced by the smoke check because dry-run now reaches Extraction recovery): `SubtitleStream.file_extension` mapped `substation`→ssa but not ffmpeg's modern `ass`/`ssa` codec names, raising `ValueError: Unknown subtitle codec: ass` and crashing extraction recovery on ASS-subtitled sources. Added `ass`→`.ass` and `ssa`/`substation`→`.ssa` mapping in `pyqenc/phases/extraction.py`. The `-f ass` routing was already present in `_SUBTITLE_FFMPEG_FORMAT`.
    - _Requirements: 2.2, 2.3, 4.6, 8.1, 8.3, 8.4, 9.1, 9.3_


- [x] 10. Cross-spec review and completion
  - [x] 10.1 Reconcile this spec against related specs and record superseded items
    - Review `phase-object-model`, `phase-recovery-refactor`, `pipeline-maturity-refactor`, `pipeline-metrics-report`, `metrics-two-tier`, `pipeline-ux-improvements`, and `artifact-state-refactor`; confirm the Cross-Spec Notes table in this spec's requirements and design is accurate, and add a short superseding summary to the top of any of those specs whose content this spec changes (scan removal, orchestrator removal, `DRY_RUN`→`PENDING`, cleanup ownership, metrics de-globalisation, banner placement)
    - Recover the timeline from `Created`/`Completed` dates or filesystem timestamps
    - Add a `- Completed: <ISO date>` entry under this spec's requirements, design, and tasks headers
    - _Requirements: 9.1, 9.2_

## Notes

- Optional property-based tests are marked with `[ ]*` (tasks 8.1-8.10); they are Hypothesis/pytest tests validating the Correctness Properties and may be run selectively.
- Structural identifier renames (e.g. `DRY_RUN` → `PENDING`) use the rope refactoring MCP; enum string VALUES, docstrings, and comments are updated manually afterward. Verify with a project-wide search, then `uv run ruff check` and `uv run python -m pytest`.
- This is a structural refactor: no ffmpeg command, filter, artifact layout, or source-property behavior changes (Req 9). Any observed change to produced artifacts or source fidelity is a regression.
- Do NOT pipe pipeline output when smoke-testing — piping breaks the `alive_progress` display for the end user.
- The `PhaseOutcome` string value changes from `dry_run` to `pending`; per pre-alpha policy no persisted-compatibility shim is kept.