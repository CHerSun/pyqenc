# Implementation Plan

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-20

- [x] 1. Core types and protocol






- [x] 1.1 Add `STALE` to `ArtifactState` enum in `state.py`

  - Add `STALE = "stale"` value
  - Update any exhaustive match/check sites that need the new value
  - _Requirements: 5.1_

- [x] 1.2 Define `Artifact` base dataclass and `PhaseResult` in a new `pyqenc/phase.py` module


  - `Artifact(path, state)` base; `PhaseResult(outcome, artifacts, message, error)` with `is_complete`, `complete`, `pending`, `did_work` derived properties
  - `Phase` Protocol with `name`, `dependencies`, `result`, `scan()`, `run(dry_run)`
  - `CleanupLevel(IntEnum)` with `NONE=0`, `INTERMEDIATE=1`, `ALL=2`
  - `Strategy` frozen dataclass with `name`, `safe_name`, `from_name()` factory
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 12.2_

- [x] 1.3 Add `cleanup: CleanupLevel` field to `PipelineConfig` in `models.py`; remove `keep_all` field


  - _Requirements: 12.1, 12.2_

- [x] 1.4 Write unit tests for `PhaseResult.is_complete`, `pending`, `did_work` derived properties


  - _Requirements: 1.4_

- [x] 2. `Strategy` adoption and `OptimizationParams` extension





- [x] 2.1 Replace string strategy names with `Strategy` objects in `models.py` and `state.py`


  - Add `StrategyTestResult(strategy: Strategy, total_size: int, avg_crf: float)` model
  - Extend `OptimizationParams` with `strategy_results: list[StrategyTestResult]`, `tolerance_pct: float`, `selected: list[Strategy]`
  - Update `OptimizationParams.to_yaml_dict` / `from_yaml_dict` for new fields
  - _Requirements: 7.5, 7.7_

- [x] 2.2 Update `config.py` strategy resolution to return `list[Strategy]` instead of `list[str]`


  - _Requirements: 7.5_

- [x] 3. `JobPhase` object






- [x] 3.1 Implement `JobPhase` in `pyqenc/phases/job.py`

  - `scan()`: load `job.yaml`, return result with `force_wipe=False`; `is_complete` = file exists and source matches
  - `run()`: source mismatch detection; `--force` path sets `force_wipe=True`, deletes `job.yaml` + all phase param YAMLs; creates/updates `job.yaml`; resolves crop (manual → cached → detect)
  - Result carries `job: JobState`, `crop: CropParams | None`, `force_wipe: bool`
  - Move disk space check and pipeline intro logging into `JobPhase.run()`
  - _Requirements: 2.1, 2.2, 2.3, 5.3, 5.4_

- [x] 3.2 Write unit tests for `JobPhase` source mismatch detection and `force_wipe` propagatio
  - _Requirements: 2.2, 5.3, 5.4_

- [x] 4. Phase registry and `_build_registry`





- [x] 4.1 Implement `_build_registry(config) -> dict[type[Phase], Phase]` in `pyqenc/phase.py`


  - Construct all phase objects in execution order, build `{type(p): p for p in phases}`, call `p._inject(registry)` on each
  - _Requirements: 3.6, 10.1_



- [x] 4.2 Add `_inject(registry: dict[type[Phase], Phase]) -> None` to each phase; each phase stores typed deps via `cast()`. DEPRECATED, replaced with direct phases construction.


  - _Requirements: 3.6_

- [x] 5. `ExtractionPhase` object





- [x] 5.1 Implement `ExtractionPhase` in `pyqenc/phases/extraction.py` wrapping existing `extract_streams` logic


  - `_recover()`: check `force_wipe` → delete `extracted/` + `extraction.yaml`; compare stored vs current include/exclude; classify artifacts as `COMPLETE`/`STALE`/`ABSENT`
  - `scan()`: recovery only, no writes
  - `run()`: extract `ABSENT` artifacts; leave `STALE` on disk; persist `extraction.yaml`; emit phase banner + streams table + recovery line + completion summary
  - Remove `standalone` parameter from `extract_streams`
  - _Requirements: 3.3, 4.2, 4.3, 5.5, 9.1, 9.3_

- [x] 6. `ChunkingPhase` object





- [x] 6.1 Implement `ChunkingPhase` in `pyqenc/phases/chunking.py` wrapping existing `chunk_video` logic


  - `_recover()`: check `force_wipe` → delete `chunks/` + `chunking.yaml`; load scene boundaries; scan chunks; classify; repair `ARTIFACT_ONLY` sidecars
  - `scan()`: recovery only
  - `run()`: detect scenes if needed; split pending chunks; emit phase banner + recovery line + completion summary
  - Remove `standalone` parameter from `chunk_video`
  - _Requirements: 3.3, 4.2, 4.3, 9.1, 9.3_

- [x] 7. `OptimizationPhase` object





- [x] 7.1 Implement `OptimizationPhase` in `pyqenc/phases/optimization.py`


  - All-strategies mode: `run()` returns all configured strategies silently (no logging, no test encodes)
  - Optimization mode: check `force_wipe` → delete test artifacts + `optimization.yaml`; check crop mismatch (fail without `--force`, delete + proceed with `--force`); run test encodes; persist `StrategyTestResult` list ordered by size to `optimization.yaml`; apply tolerance to select strategies
  - Tolerance re-application: if all strategy results cached and only tolerance changed, re-select without re-encoding
  - Result carries `selected_strategies: list[Strategy]`, `strategy_results: list[StrategyTestResult]`
  - Emit phase banner + recovery line + per-strategy completion table + optimization summary table
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 9.1, 9.3_


- [x] 7.2 Write unit tests for tolerance re-application from cached results

  - _Requirements: 7.7_

- [x] 8. `encoding/` + `encoded/` folder split





- [x] 8.1 Update `ChunkEncoder._get_output_dir` to write attempts into `encoding/<strategy.safe_name>/`


  - _Requirements: 6.1_

- [x] 8.2 On CRF search convergence: hard-link winning attempt into `encoded/<strategy.safe_name>/`, write result sidecar and winning graph alongside it


  - Use `os.link()` for hard-link; fall back to copy if cross-device
  - _Requirements: 6.2, 6.3_

- [x] 8.3 Update `EncodingPhase._recover()` to scan `encoded/` for COMPLETE artifacts and `encoding/` for CRF history


  - _Requirements: 6.4_

- [x] 8.4 Apply intermediate cleanup: after hard-linking, if `config.cleanup >= CleanupLevel.INTERMEDIATE`, delete attempt files from `encoding/<strategy.safe_name>/` for the completed pair


  - _Requirements: 6.6, 12.3_

- [x] 9. `EncodingPhase` object





- [x] 9.1 Implement `EncodingPhase` in `pyqenc/phases/encoding.py`


  - `_recover()`: check `force_wipe` → delete `encoding/` + `encoded/` + `encoding.yaml`; check crop mismatch; classify `(chunk, strategy)` pairs; re-evaluate `COMPLETE` pairs against current quality targets → downgrade to `ARTIFACT_ONLY` if targets no longer met
  - `scan()`: recovery only
  - `run()`: encode pending pairs; emit phase banner + recovery line + per-chunk completion messages + phase summary
  - Remove `standalone` parameter from `encode_all_chunks`
  - _Requirements: 3.3, 4.2, 5.5, 5.6, 9.1, 9.3_

- [x] 9.2 Write unit tests for quality target re-evaluation (COMPLETE → ARTIFACT_ONLY downgrade)


  - _Requirements: 5.7_

- [x] 10. `AudioPhase` object





- [x] 10.1 Implement `AudioPhase` in `pyqenc/phases/audio.py` wrapping existing audio processing logic


  - `_recover()`: check `force_wipe` → delete `audio/`; scan `audio/` for processed files; classify
  - `scan()`: recovery only
  - `run()`: process pending audio files; emit phase banner + recovery line + completion summary
  - _Requirements: 3.3, 4.2, 9.1, 9.3_


- [x] 11. `MergePhase` object




- [x] 11.1 Implement `MergePhase` in `pyqenc/phases/merge.py` wrapping existing merge logic


  - `_recover()`: check `force_wipe` → delete `final/`; scan `final/` for output + sidecar pairs; classify
  - `scan()`: recovery only
  - `run()`: in pipeline mode read from `EncodingPhase.result`; in standalone mode scan `encoded/`; concatenate, measure quality, write sidecar; emit phase banner + recovery line + completion summary
  - _Requirements: 3.3, 4.2, 6.5, 9.1, 9.3_

- [x] 12. Move recovery logic into phase objects





- [x] 12.1 Move `recover_extraction`, `recover_chunking`, `recover_attempts` from `recovery.py` into their respective phase objects as `_recover()` methods


  - _Requirements: 4.3_

- [x] 12.2 Remove `discover_inputs()` and all `InputsDiscovery` code from `recovery.py`; retain only shared low-level helpers (`_cleanup_tmp_files`, `_parse_chunk_timestamps`)



  - _Requirements: 3.4, 4.4_

- [x] 13. Orchestrator simplification





- [x] 13.1 Rewrite `PipelineOrchestrator.run()` to use `_build_registry` and iterate phase objects



  - Remove all phase-specific logic (`_execute_extraction`, `_execute_chunking`, etc.), `_optimal_strategy` instance var, `_resolve_crop_params`, `_get_crop_params`, `_execute_merge`, `_emit_merge_summary`
  - Orchestrator only: build registry, iterate, check `result.outcome`, log phase boundary markers and overall outcome
  - _Requirements: 8.2, 8.3, 9.2, 9.4, 10.2, 10.3_

- [x] 14. CLI and API updates





- [x] 14.1 Update `cli.py`: replace `--keep-all` with `--cleanup [all]`; remove `-y N` max-phases option; wire `CleanupLevel` into `PipelineConfig`


  - _Requirements: 11.5, 12.1, 12.2_

- [x] 14.2 Update `api.py` standalone functions to call `_build_registry` and `phase.run()` instead of calling phase functions directly


  - _Requirements: 11.3_

- [x] 15. Post-pipeline full cleanup




- [x] 15.1 Implement post-pipeline cleanup step: after full pipeline success, if `config.cleanup >= CleanupLevel.ALL`, delete remaining intermediate directories


  - _Requirements: 12.4, 12.5_

- [x] 16. Logging cleanup





- [x] 16.1 Remove all phase-specific log messages from `orchestrator.py` that duplicate phase-internal logging


  - _Requirements: 9.4_


- [x] 16.2 Ensure each phase emits: banner, key parameters, recovery result line, action plan, per-item completion (no start messages at info), step/strategy summaries, phase completion summary


  - Move any phase-related messages currently in `orchestrator.py` or `api.py` into the appropriate phase object
  - Reuse existing formatting utilities (`fmt_key_value_table`, `fmt_merge_summary_*`, `fmt_chunk_attempt_start`, `fmt_chunk_final`, thick/thin line constants, etc.) where they already match the logging standards; only replace or add where the current output doesn't conform
  - _Requirements: 9.1, 9.2, 9.3_

- [x] 18. Fix MergePhase standalone input discovery







- [x] 18.1 Replace `MergePhase._collect_encoded_chunks()` filesystem scan with `EncodingPhase.scan()`

  - In standalone mode, construct and call `self._encoding.scan()` instead of globbing `encoded/` directly
  - `EncodingPhase.scan()` runs `_recover_encoding_attempts` which applies quality-target re-evaluation and crop mismatch detection — the raw glob does not
  - `MergePhase._get_expected_strategies()` standalone fallback should derive strategies from `EncodingPhase.scan().encoded` rather than `iterdir()` on `encoded/`
  - After this change, `_collect_encoded_chunks()` standalone path reads from `self._encoding.result.encoded` (same as pipeline path); the only difference is whether the result came from a live run or a `scan()` call
  - _Requirements: 3.1, 3.2, 6.5_

- [x] 19. Remove `JobStateManager` entirely






- [x] 19.1 Add `load(path)` / `save(path)` classmethods to each phase param model in `state.py`


  - Add `@classmethod load(cls, path: Path) -> Self | None` and `save(self, path: Path) -> None` to `JobState`, `ExtractionParams`, `ChunkingParams`, `OptimizationParams`, `EncodingParams`
  - `load` reads the YAML file atomically and returns `None` if absent or invalid; `save` writes via `write_yaml_atomic`
  - This makes each model self-sufficient: `ExtractionParams.load(work_dir / "extraction.yaml")` replaces `manager.load_extraction()`
  - _Requirements: 4.1, 4.2_

- [x] 19.2 Replace all `JobStateManager` usages in phases with direct model load/save calls


  - Each phase calls its own param model's `load`/`save` directly; no manager intermediary
  - Move `_find_source_mismatches` logic from `JobStateManager` into `JobPhase._find_source_mismatches` (it already lives there — remove the duplicate in `JobStateManager`)
  - Update all call sites in legacy functions (`chunk_video`, `encode_all_chunks`, `extract_streams`) to use direct model calls
  - Delete `JobStateManager` class and all its methods from `state.py`; remove `_PHASE_PARAM_FILENAMES` list (each phase already handles its own wipe in `_recover(force_wipe=True)`)
  - _Requirements: 4.1, 4.2, 4.3_

- [x] 20. Fix `OptimizationPhase` quality-target change detection







- [x] 20.1 Persist global quality targets in `OptimizationParams`

  - Add `quality_targets: list[str]` field to `OptimizationParams` in `state.py` — serialised as `"metric-statistic:value"` strings; represents the targets active when the last run wrote this file
  - Update `to_yaml_dict` / `from_yaml_dict` on `OptimizationParams` to round-trip the new field
  - Do NOT add per-`StrategyTestResult` targets — quality targets are global, not per-strategy
  - _Requirements: 6.5, 7.5_

- [x] 20.2 Re-evaluate `OptimizationPhase` artifacts against current quality targets in `scan()` and `run()`; invalidate `encoded/` result sidecars on change


  - In `OptimizationPhase.scan()`: after loading `optimization.yaml`, compare persisted `quality_targets` against `self._config.quality_targets`; if they differ, return `DRY_RUN` — `DRY_RUN` is the correct outcome from `scan()` because it means "work is needed but hasn't been done yet"; `FAILED` would be wrong here since nothing has gone wrong, the phase simply needs to run; downstream phases check `dep.result.is_complete` which is `False` for `DRY_RUN`, so they will correctly refuse to proceed until `run()` is called; tolerance-only changes still allow `REUSED` with re-applied selection
  - In `OptimizationPhase.run()`: apply the same quality-target check before the tolerance re-application short-circuit; if targets changed, delete all result sidecars (`<chunk_id>.<res>.yaml`) from `encoded/<strategy>/` for every strategy (leaving attempt files in `encoding/` intact for CRF history replay), then treat all cached `StrategyTestResult` entries as stale and add all strategies to `strategies_to_test`
  - **All-strategies mode**: always write `optimization.yaml` with `strategy_results=[]` and current `quality_targets` before returning; on the next run, if targets changed, perform the same `encoded/` result sidecar deletion before returning — this ensures `EncodingPhase` sees `ARTIFACT_ONLY` pairs regardless of optimization mode
  - Persist the current `quality_targets` into `OptimizationParams` on every save
  - `OptimizationPhase` is the single owner of quality-target change detection and `encoded/` result sidecar invalidation — `EncodingPhase` must not duplicate this check
  - _Requirements: 6.5, 7.5_

- [x] 20.3 Remove `quality_targets` from `EncodingResultSidecar` and `EncodingPhase._recover()`




  - Remove `quality_targets: list[str]` field from `EncodingResultSidecar` in `state.py`; update `to_yaml_dict` / `from_yaml_dict` (field was written but never read by the algorithm)
  - Remove the `_enc_targets_met()` call and quality-target downgrade logic from `_enc_recover_pair()` in `encoding.py` — `OptimizationPhase` now handles this upstream by deleting stale result sidecars; `EncodingPhase._recover()` simply sees `ARTIFACT_ONLY` pairs naturally
  - _Requirements: 6.5, 7.5_

- [x] 21. Fix `AudioPhase` config-change detection in `_recover()`






- [x] 21.1 Add `AudioParams` state model and persist audio conversion config on each run


  - Add `AudioParams` dataclass to `state.py` with fields: `audio_codec: str | None`, `audio_base_bitrate: str | None` — these are the Type B config values that affect the *content* of produced AAC files and must be tracked across runs
  - Do NOT include `audio_convert` — the convert filter is a Type A input: `AudioEngine.build_plan()` with the current filter already defines the expected terminal outputs, so no cross-run tracking is needed
  - Add `to_yaml_dict` / `from_yaml_dict` to `AudioParams`
  - Add `load_audio` / `save_audio` to `JobStateManager` (or use direct load/save if task 19 lands first) writing to `audio.yaml` in the work directory
  - In `AudioPhase._execute_audio()`: write `AudioParams` to `audio.yaml` after successful processing
  - _Requirements: 5.5, 5.6_

- [x] 21.2 Apply convert-filter plan evaluation and codec-change detection in `AudioPhase._recover()`


  - Replace the current "any AAC files → all COMPLETE" heuristic with per-file plan evaluation: run `AudioEngine.build_plan()` with the current `convert_filter` against the `audio/` directory; files that appear as terminal (conversion) outputs in the plan are `COMPLETE`; intermediate-only files that are present but not terminal outputs are `STALE`; expected terminal outputs not yet on disk are `ABSENT`
  - Additionally load `audio.yaml`; if absent, treat all existing files as `ARTIFACT_ONLY` (codec/bitrate config unknown — cannot confirm content validity); if present and `audio_codec` or `audio_base_bitrate` differ from current config, mark all existing artifacts as `STALE` (AAC content must be regenerated)
  - _Requirements: 5.5, 5.6_

- [ ] 17. Spec completion



- [ ] 17.1 Update `Completed` date in this spec and in `requirements.md` and `design.md`
  - _Requirements: all_
