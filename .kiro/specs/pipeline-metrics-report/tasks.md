# Implementation Plan: Pipeline Metrics Report

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-10

## Overview

Implement `MetricsCollector` injection across the full pipeline. All metrics code lives in `pyqenc/metrics.py`. Phases gain a required `collector: MetricsCollector` constructor parameter. `_build_registry` is updated to accept and thread the collector. The orchestrator constructs `YamlMetricsCollector`, registers signal handlers, and calls `flush(partial=False)` on success. `api.py` standalone callers use `NoOpMetricsCollector`.

## Tasks

- [x] 1. Add `hypothesis` to test dependencies and create `pyqenc/metrics.py` skeleton
  - Add `hypothesis>=6.0` to `[dependency-groups] test` in `pyproject.toml`
  - Create `pyqenc/metrics.py` with module docstring, `__all__`, and all imports
  - Define `FLUSH_INTERVAL = 10` and `METRICS_YAML_FILENAME = "metrics.yaml"` constants
  - _Requirements: 1.3, 7.1, 7.2_

- [x] 2. Implement `TimeKey` and `SpaceKey` StrEnums
  - [x] 2.1 Implement `TimeKey(StrEnum)` with all 11 dotted values as specified in the design
    - Values: `"job.probe"`, `"job.crop_detect"`, `"extraction.mkvextract"`, `"chunking.scene_detect"`, `"chunking.split"`, `"audio.processing"`, `"encoding.optimization"`, `"encoding.main"`, `"merge.concat"`, `"merge.quality_measure"`, `"recovery"`
    - _Requirements: 2.4_
  - [x] 2.2 Implement `SpaceKey(StrEnum)` with all 10 dotted values as specified in the design
    - Values: `"source"`, `"extracted.video"`, `"extracted.audio"`, `"extracted.other"`, `"chunks"`, `"audio.intermediate"`, `"audio.final"`, `"encoding.workspace"`, `"encoding.outputs"`, `"final"`
    - _Requirements: 3.2_
  - [x] 2.3 Write unit tests for `TimeKey` and `SpaceKey` enum membership
    - Assert `TimeKey` has exactly 11 members with correct dotted string values
    - Assert `SpaceKey` has exactly 10 members with correct dotted string values
    - _Requirements: 2.4, 3.2_

- [x] 3. Implement Pydantic data models in `pyqenc/metrics.py`
  - [x] 3.1 Implement `ConvergenceUpdate` dataclass, `AttemptStats`, `ConvergenceStats`, `TimeEntry`, `SpaceEntry`, `TimeDistribution`, `SpaceDistribution`, `ConvergenceSection`, `PipelineMetrics` Pydantic models
    - All fields typed per design; `convergence` field is `ConvergenceSection | None = None`
    - Implement `_format_duration(seconds: int) -> str` — omit days component when 0
    - Implement `_format_gb(n: int) -> str` — always GB, 2 decimal places, 1024-based
    - _Requirements: 5.1, 5.2, 5.3_
  - [x] 3.2 Write property test for YAML serialization round-trip (Property 6)
    - **Property 6: YAML serialization round-trip**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
    - Generate random valid `PipelineMetrics` instances via `hypothesis`; serialize to YAML and deserialize back; assert field-by-field equivalence within tolerance
    - Tag: `# Feature: pipeline-metrics-report, Property 6: YAML serialization round-trip`
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 4. Implement `MetricsCollector` Protocol and `NoOpMetricsCollector`
  - [x] 4.1 Implement `MetricsCollector` as a `@runtime_checkable` Protocol with `time(key)`, `record_step(key, elapsed_seconds, convergence_update=None)`, and `flush(partial=True)` methods
    - `time(key: TimeKey) -> ContextManager[None]` — phase-facing surface
    - `record_step(key: TimeKey, elapsed_seconds: float, convergence_update: ConvergenceUpdate | None = None) -> None`
    - `flush(partial: bool = True) -> None` — orchestrator-only, not part of phase-facing surface
    - _Requirements: 6.1, 6.6_
  - [x] 4.2 Implement `NoOpMetricsCollector` satisfying the Protocol — discards all data
    - `time()` returns a no-op context manager (use `contextlib.nullcontext`)
    - `record_step()` and `flush()` are no-ops
    - _Requirements: 6.4_
  - [x] 4.3 Write unit test: `isinstance(NoOpMetricsCollector(), MetricsCollector)` is `True`
    - _Requirements: 6.1_

- [x] 5. Implement `_measure_space` and `ConvergenceAccumulator` internals
  - [x] 5.1 Implement `ConvergenceAccumulator` internal dataclass with Welford fields: `n`, `total`, `min`, `max`, `welford_mean`, `welford_M2`
    - Implement `_compute_convergence(accumulators) -> list[ConvergenceStats] | None`
    - Returns `None` when all accumulators have `n == 0`; population stddev = `sqrt(M2/n)`, `0.0` when `n == 1`
    - Results sorted by strategy name
    - _Requirements: 4.2_
  - [x] 5.2 Implement `_measure_space(work_dir: Path, config: PipelineConfig) -> dict[SpaceKey, int]`
    - `SOURCE`: `config.source_video.stat().st_size`; `EXTRACTED_VIDEO`: sum `*.mkv` in `extracted/`; `EXTRACTED_AUDIO`: sum `*.mka`; `EXTRACTED_OTHER`: all other files in `extracted/` (non-recursive)
    - `CHUNKS`: recursive sum of `chunks/`; `AUDIO_INTERMEDIATE`: `.flac` files in `audio/` (non-recursive); `AUDIO_FINAL`: non-`.flac` files in `audio/` (non-recursive)
    - `ENCODING_WORKSPACE`: recursive sum of `encoding/`; `ENCODING_OUTPUTS`: recursive sum of `encoded/`; `FINAL`: recursive sum of `final/`
    - Missing dirs/files → 0; `OSError` on individual `stat()` → log DEBUG, treat as 0
    - _Requirements: 3.1, 3.2, 3.4, 3.6_
  - [x] 5.3 Write property test for space measurement accuracy (Property 4)
    - **Property 4: Space measurement accuracy**
    - **Validates: Requirements 3.1, 3.3, 3.4**
    - Generate random directory trees with known file sizes using `tmp_path`; assert `_measure_space()` returns exact byte counts per category; total == sum of parts
    - Tag: `# Feature: pipeline-metrics-report, Property 4: Space measurement accuracy`
    - _Requirements: 3.1, 3.3, 3.4_
  - [x] 5.4 Write property test for convergence stats math (Property 5)
    - **Property 5: Convergence stats math**
    - **Validates: Requirements 4.2, 4.1a**
    - Generate random sequences of attempt counts (integers ≥ 1) fed incrementally via `record_step`; assert all `ConvergenceStats` fields match `min/max/sum/mean/population_stddev/len` of input sequences; also assert resume from persisted YAML produces identical results
    - Tag: `# Feature: pipeline-metrics-report, Property 5: Convergence stats math`
    - _Requirements: 4.2, 4.1a_

- [x] 6. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement `YamlMetricsCollector`
  - [x] 7.1 Implement `YamlMetricsCollector.__init__` with `work_dir`, `config`, `force_wipe=False`
    - If `force_wipe=True`: delete existing `metrics.yaml` and start fresh
    - Otherwise: load existing `metrics.yaml` and restore `_time_accum`, `_conv_accumulators`, and `_space_snapshot` from persisted state (resume Welford from `stddev² * n`)
    - On load failure: log WARNING, start fresh
    - _Requirements: 1.1, 1.2, 1.5_
  - [x] 7.2 Implement `time(key: TimeKey)` context manager
    - Records `time.monotonic()` on enter; on exit calls `record_step(key, elapsed)`
    - Context manager catches exceptions and re-raises after recording elapsed
    - _Requirements: 2.1, 6.6_
  - [x] 7.3 Implement `record_step(key, elapsed_seconds, convergence_update=None)`
    - Adds `elapsed_seconds` to `_time_accum[key]`
    - If `convergence_update` is not None: update Welford accumulators for the strategy
    - Increment `_flush_counter`; if `>= FLUSH_INTERVAL` call `_flush_incremental()` and reset counter
    - _Requirements: 1.3, 2.2, 2.2a, 4.1a_
  - [x] 7.4 Implement `_flush_incremental()` — writes time and convergence only, no space scan
    - Builds `PipelineMetrics` with current `_time_accum` and convergence accumulators
    - Sets `partial=True`, updates `time_distribution.updated_at` and `convergence.updated_at` to `datetime.now().strftime("%Y-%m-%d %H:%M:%S")`
    - Uses last known `_space_snapshot` (may be empty on first incremental flush)
    - Writes atomically via `.tmp`-then-rename using `TEMP_SUFFIX`
    - On write failure: log WARNING, do not raise
    - _Requirements: 1.3, 1.5, 5.3_
  - [x] 7.5 Implement `flush(partial: bool = True)` — full flush with space scan
    - Logs `INFO "Measuring disk space for metrics..."` before scanning
    - Calls `_measure_space()`, stores result in `_space_snapshot`
    - Updates `space_distribution.updated_at` to current local time
    - Builds complete `PipelineMetrics` and writes atomically
    - On write failure: log WARNING, do not raise
    - _Requirements: 1.4, 1.5, 3.1, 5.4_
  - [x] 7.6 Write unit tests for `YamlMetricsCollector` lifecycle
    - `force_wipe=True` deletes existing `metrics.yaml` and starts fresh (Req 1.2)
    - Write failure (mocked `Path.replace` raising `OSError`) logs WARNING and does not propagate (Req 1.5)
    - `flush(partial=False)` sets `partial: false`; `flush(partial=True)` sets `partial: true` (Req 5.4)
    - Empty convergence data produces `convergence: null` in YAML (Req 4.4)
    - _Requirements: 1.2, 1.5, 4.4, 5.4_
  - [x] 7.7 Write property test for time accumulation round-trip (Property 1)
    - **Property 1: Time accumulation round-trip**
    - **Validates: Requirements 2.1, 2.2, 2.2a**
    - Generate random `TimeKey` and random list of positive floats; assert `_time_accum[key] == sum(durations)` after all `record_step` calls
    - Tag: `# Feature: pipeline-metrics-report, Property 1: Time accumulation round-trip`
    - _Requirements: 2.1, 2.2, 2.2a_
  - [x] 7.8 Write property test for time distribution math (Property 2)
    - **Property 2: Time distribution math**
    - **Validates: Requirements 2.3, 2.5**
    - Generate random mapping of `TimeKey → float` (non-negative); assert `total_seconds == sum(values)`, each `percent == value / total * 100` (or 0.0 when total is 0)
    - Tag: `# Feature: pipeline-metrics-report, Property 2: Time distribution math`
    - _Requirements: 2.3, 2.5_
  - [x] 7.9 Write property test for breakdown sorted descending (Property 3)
    - **Property 3: Breakdown sorted descending**
    - **Validates: Requirements 2.6, 3.5**
    - Generate random `PipelineMetrics` instances; assert `time_distribution.breakdown` sorted descending by `seconds`; `space_distribution.breakdown` sorted descending by bytes
    - Tag: `# Feature: pipeline-metrics-report, Property 3: Breakdown sorted descending`
    - _Requirements: 2.6, 3.5_

- [x] 8. Update `_build_registry` in `phase.py` and `api.py`
  - [x] 8.1 Update `_build_registry(config, collector: MetricsCollector | None = None)` signature in `phase.py`
    - When `collector` is `None`, construct `NoOpMetricsCollector` internally
    - Pass `collector` as the third positional argument to every phase constructor call in the loop
    - _Requirements: 6.2, 6.3, 6.4_
  - [x] 8.2 Update every `_build_registry` call in `api.py` to pass `NoOpMetricsCollector()`
    - Import `NoOpMetricsCollector` from `pyqenc.metrics`
    - Each standalone function constructs `collector = NoOpMetricsCollector()` and passes it to `_build_registry(config, collector)`
    - _Requirements: 6.4_

- [x] 9. Add `collector: MetricsCollector` parameter to all phase constructors
  - [x] 9.1 Update `JobPhase.__init__` — add `collector: MetricsCollector` as required third parameter; store as `self._collector`
    - _Requirements: 6.2_
  - [x] 9.2 Update `ExtractionPhase.__init__` — add `collector: MetricsCollector`; store as `self._collector`
    - _Requirements: 6.2_
  - [x] 9.3 Update `ChunkingPhase.__init__` — add `collector: MetricsCollector`; store as `self._collector`
    - _Requirements: 6.2_
  - [x] 9.4 Update `OptimizationPhase.__init__` — add `collector: MetricsCollector`; store as `self._collector`
    - _Requirements: 6.2_
  - [x] 9.5 Update `EncodingPhase.__init__` — add `collector: MetricsCollector`; store as `self._collector`
    - _Requirements: 6.2_
  - [x] 9.6 Update `AudioPhase.__init__` — add `collector: MetricsCollector`; store as `self._collector`
    - _Requirements: 6.2_
  - [x] 9.7 Update `MergePhase.__init__` — add `collector: MetricsCollector`; store as `self._collector`
    - _Requirements: 6.2_
  - [x] 9.8 Write unit tests: each phase constructor accepts a `collector` parameter and stores it
    - Instantiate each phase with a `NoOpMetricsCollector`; assert `phase._collector` is the passed instance
    - _Requirements: 6.2_

- [x] 10. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Instrument `JobPhase` with timing calls
  - [x] 11.1 Wrap `VideoMetadata` probing calls in `_create_or_update_job` with `self._collector.time(TimeKey.JOB_PROBE)`
    - _Requirements: 6.5_
  - [x] 11.2 Wrap `detect_crop_parameters` call in `_resolve_crop` with `self._collector.time(TimeKey.JOB_CROP_DETECT)`
    - Only when crop detection actually runs (not when returning manual or cached crop)
    - _Requirements: 6.5_
  - [x] 11.3 Write phase integration test for `JobPhase` timing
    - Use a spy/mock `MetricsCollector`; call `phase.run()`; assert `record_step` was called with `TimeKey.JOB_PROBE`
    - _Requirements: 6.5_

- [x] 12. Instrument `ExtractionPhase` with timing calls
  - [x] 12.1 Wrap `extractor.extract_tracks()` call in `_execute_extraction` with `self._collector.time(TimeKey.EXTRACTION)`
    - _Requirements: 6.5_
  - [x] 12.2 Wrap `_recover()` call in `run()` with manual `time.monotonic()` bookends and `self._collector.record_step(TimeKey.RECOVERY, elapsed)`
    - _Requirements: 6.5, 2.7_
  - [x] 12.3 Write phase integration test for `ExtractionPhase` timing
    - Assert `time()` called with `TimeKey.EXTRACTION` and `TimeKey.RECOVERY`
    - _Requirements: 6.5_

- [x] 12.5 Implement active-timer capture for in-flight `time()` contexts on forced flush
  - [x] 12.5.1 Add `_active_timers: list[tuple[TimeKey, float]]` to `YamlMetricsCollector.__init__`
    - On `_TimingContext.__enter__`: append `(key, t0)` to `_active_timers`
    - On `_TimingContext.__exit__`: remove the entry from `_active_timers` (by identity/index), then accumulate elapsed normally
    - _Requirements: 1.4_
  - [x] 12.5.2 Add `_snapshot_active_timers()` helper that returns `dict[TimeKey, float]` of partial elapsed for all in-flight timers (does not modify `_active_timers` or `_time_accum`)
    - Call this in both `_flush_incremental()` and `flush()` before building metrics: add partial elapsed to a copy of `_time_accum` used only for that build — do NOT mutate `_time_accum` itself (timers are still running)
    - _Requirements: 1.4_
  - [x] 12.5.3 Write unit tests for active-timer capture
    - Assert that `flush()` called while a `time()` context is active includes partial elapsed in the written YAML
    - Assert that after the context exits normally, the final accumulated value equals the full elapsed (not double-counted)
    - _Requirements: 1.4_

- [x] 13. Instrument `ChunkingPhase` with timing calls
  - [x] 13.1 Wrap `detect_scenes()` call in `_execute_chunking` with `self._collector.time(TimeKey.CHUNKING_SCENE_DETECT)`
    - _Requirements: 6.5_
  - [x] 13.2 Wrap the entire chunk-split loop in `split_chunks` with `self._collector.time(TimeKey.CHUNKING_SPLIT)`; call `self._collector.step(TimeKey.CHUNKING_SPLIT)` after each successful split — pass `collector` down from `ChunkingPhase._execute_chunking` to `split_chunks`
    - _Requirements: 6.5, 2.2a_
  - [x] 13.3 Wrap `_recover()` call in `run()` with `record_step(TimeKey.RECOVERY, elapsed)`
    - _Requirements: 6.5, 2.7_
  - [x] 13.4 Write phase integration test for `ChunkingPhase` timing
    - Assert `record_step` called with `TimeKey.CHUNKING_SCENE_DETECT`, `TimeKey.CHUNKING_SPLIT`, and `TimeKey.RECOVERY`
    - _Requirements: 6.5_

- [x] 14. Instrument `AudioPhase` with timing calls
  - [x] 14.1 Wrap the full async engine execution in `run()` with `self._collector.time(TimeKey.AUDIO)`
    - _Requirements: 6.5_
  - [x] 14.2 Wrap `_recover()` call in `run()` with `record_step(TimeKey.RECOVERY, elapsed)`
    - _Requirements: 6.5, 2.7_
  - [x] 14.3 Write phase integration test for `AudioPhase` timing
    - Assert `record_step` called with `TimeKey.AUDIO` and `TimeKey.RECOVERY`
    - _Requirements: 6.5_

- [x] 15. Instrument `OptimizationPhase` with timing calls
  - [x] 15.1 Wrap the entire optimization loop in `_encode_strategy_test_chunks` with `self._collector.time(TimeKey.ENCODING_OPTIMIZATION)`; call `self._collector.step(TimeKey.ENCODING_OPTIMIZATION, convergence_update=ConvergenceUpdate(strategy=strategy.name, attempt_count=attempt_number))` after each test-chunk attempt converges
    - Pass `collector` down from `OptimizationPhase.run()` to the async encode helper
    - _Requirements: 6.5, 2.2a, 4.1a_
  - [x] 15.2 Wrap the param-load / recovery section in `run()` with `record_step(TimeKey.RECOVERY, elapsed)`
    - _Requirements: 6.5, 2.7_
  - [x] 15.3 Write phase integration test for `OptimizationPhase` timing
    - Assert `record_step` called with `TimeKey.ENCODING_OPTIMIZATION` (with `convergence_update`) and `TimeKey.RECOVERY`
    - _Requirements: 6.5_

- [ ] 16. Instrument `EncodingPhase` with timing calls
  - [ ] 16.1 Wrap the entire encoding loop in `ChunkEncoder` with `self._collector.time(TimeKey.ENCODING_MAIN)`; call `self._collector.step(TimeKey.ENCODING_MAIN, convergence_update=ConvergenceUpdate(strategy=strategy, attempt_count=attempt_number))` after each chunk/strategy pair converges (after `_finalize_winning_attempt`)
    - Pass `collector` down from `EncodingPhase.run()` to `ChunkEncoder`
    - _Requirements: 6.5, 2.2a, 4.1a_
  - [ ] 16.2 Wrap `_recover_encoding_attempts()` call in `run()` with `record_step(TimeKey.RECOVERY, elapsed)`
    - _Requirements: 6.5, 2.7_
  - [ ] 16.3 Write phase integration test for `EncodingPhase` timing
    - Assert `record_step` called with `TimeKey.ENCODING_MAIN` (with `convergence_update`) and `TimeKey.RECOVERY`
    - _Requirements: 6.5_

- [ ] 17. Instrument `MergePhase` with timing calls
  - [ ] 17.1 Wrap ffmpeg concat call per strategy in `_execute_merge` with `self._collector.time(TimeKey.MERGE_CONCAT)`
    - _Requirements: 6.5_
  - [ ] 17.2 Wrap `_measure_quality()` call per strategy in `_execute_merge` with `self._collector.time(TimeKey.MERGE_QUALITY_MEASURE)`
    - _Requirements: 6.5_
  - [ ] 17.3 Wrap `_recover()` call in `run()` with `record_step(TimeKey.RECOVERY, elapsed)`
    - _Requirements: 6.5, 2.7_
  - [ ] 17.4 Write phase integration test for `MergePhase` timing
    - Assert `record_step` called with `TimeKey.MERGE_CONCAT`, `TimeKey.MERGE_QUALITY_MEASURE`, and `TimeKey.RECOVERY`
    - _Requirements: 6.5_

- [ ] 18. Implement `--no-metrics` CLI flag and `PipelineConfig.no_metrics` field
  - [ ] 18.1 Add `no_metrics: bool = False` field to `PipelineConfig`
    - Default `False` so existing behaviour is unchanged
    - _Requirements: 8.1_
  - [ ] 18.2 Add `--no-metrics` argument to the CLI argument parser
    - `action="store_true"`, `default=False`
    - `help="Suppress metrics.yaml output (metrics are still collected internally but not written to disk)"`
    - Wire into `PipelineConfig` construction: `no_metrics=args.no_metrics`
    - _Requirements: 8.1, 8.6_

- [ ] 19. Update `PipelineOrchestrator` to construct and manage `YamlMetricsCollector`
  - [ ] 19.1 In `PipelineOrchestrator.__init__` or `run()`, branch on `config.no_metrics`: construct `NoOpMetricsCollector()` when `True`, otherwise construct `YamlMetricsCollector(work_dir=config.work_dir, config=config, force_wipe=...)` and pass the result to `_build_registry(config, collector)`
    - Determine `force_wipe` from `JobPhase` result after it runs, or pass `False` initially and let `YamlMetricsCollector` handle resume
    - _Requirements: 1.1, 1.6, 6.3, 8.2, 8.3_
  - [ ] 19.2 Register `signal.signal(SIGINT, ...)`, `signal.signal(SIGTERM, ...)`, and `signal.signal(CTRL_C_EVENT, ...)` (Windows) handlers that call `collector.flush(partial=True)` before re-raising — only when `config.no_metrics` is `False`
    - Register `atexit.register(collector.flush, partial=True)` as safety net — only when `config.no_metrics` is `False`
    - _Requirements: 1.4, 6.7, 6.8, 8.5_
  - [ ] 19.3 After all phases complete successfully, call `collector.flush(partial=False)` and log INFO with path to `metrics.yaml` — only when `config.no_metrics` is `False`
    - This is the only place `partial=False` is set
    - _Requirements: 5.4, 5.5, 8.3_
  - [ ] 19.4 Write unit tests for orchestrator collector construction and signal handler registration
    - Assert `atexit` handler is registered and `flush(partial=False)` is called on successful completion when `config.no_metrics=False`
    - Assert `NoOpMetricsCollector` is used, no signal handlers registered, and no `flush` calls made when `config.no_metrics=True`
    - _Requirements: 1.4, 6.7, 6.8, 8.2, 8.5_

- [ ] 20. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 21. Review spec against other specs and update cross-spec summaries
  - Compare `pipeline-metrics-report` spec dates and content against other specs in `.kiro/specs/`
  - Add a summary section to the top of this spec and any related specs noting what was superseded or changed
  - Add `- Completed: <date>` to this spec's header
  - _Requirements: (agent-specs.md rule)_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- `hypothesis` must be added to `pyproject.toml` test deps before property tests can run
- `partial=False` is set only in task 18.3 — the orchestrator's explicit success flush
- Space measurement runs only inside `flush()`, never inside `_flush_incremental()`
- `flush()` logs `INFO "Measuring disk space for metrics..."` before scanning so the user sees why exit is delayed
- All datetime strings use `"%Y-%m-%d %H:%M:%S"` (space separator, no T, no timezone)
- Per-section `updated_at` timestamps: time/convergence updated on every incremental flush; space only on full flush
- Property tests live in `tests/test_metrics_properties.py`; unit tests in `tests/test_metrics.py`; phase integration tests in `tests/test_metrics_integration.py`
