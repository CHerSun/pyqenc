# Implementation Plan

<!-- markdownlint-disable MD024 -->

- [x] 1. Add `output_file` parameter to ffmpeg runner and update all callers





  - Add mandatory `output_file: Path | list[Path] | None` to `run_ffmpeg_async` and `run_ffmpeg` in `pyqenc/utils/ffmpeg_runner.py`
  - When `output_file` is a `Path` or `list[Path]`: validate each path appears in `cmd` (raise `ValueError` if not), replace with `<stem>.tmp` sibling, rename on success, delete on failure
  - Update all callers in `pyqenc/phases/extraction.py`, `pyqenc/phases/chunking.py`, `pyqenc/phases/encoding.py`, `pyqenc/models.py` to pass `output_file`; remove any existing `.tmp`-then-rename logic from those callers
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

- [x] 2. Implement `write_yaml_atomic` utility and `ArtifactState` enum





  - Create `pyqenc/utils/yaml_utils.py` with `write_yaml_atomic(path: Path, data: dict) -> None`
  - Add `ArtifactState` enum (`ABSENT`, `ARTIFACT_ONLY`, `COMPLETE`) to `pyqenc/state.py`
  - _Requirements: 2.5, 3.1_

- [x] 3. Implement `JobStateManager` and data models





  - Create `pyqenc/state.py` with `JobState`, `ChunkingParams`, `OptimizationParams`, `EncodingParams`, `MetricsSidecar`, `EncodingResultSidecar`, `ChunkSidecar` models
  - Implement `JobStateManager` with typed `load_job`/`save_job`, `validate`, `load_chunking`/`save_chunking`, `load_optimization`/`save_optimization`, `load_encoding`/`save_encoding` methods
  - `validate` implements dry-run / execute / `--force` source-binding logic
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 9.1, 9.2, 9.4_

- [x] 4. Implement extraction phase recovery





  - Add `recover_extraction(work_dir: Path, job: JobState) -> ExtractionRecovery` in `pyqenc/phases/recovery.py`
  - Scan `extracted/` for artifact files; classify as `ABSENT` or `COMPLETE` (`.tmp` protocol guarantees consistency)
  - Update `extract_streams` to call recovery first; skip extraction if `COMPLETE`; write `extraction.yaml` marker is NOT needed (no params to store)
  - Add `.tmp` file cleanup at phase start
  - _Requirements: 3.2, 4.1, 4.2, 4.3, 7.7_

- [x] 5. Implement chunking phase recovery and chunk sidecars





  - Add `recover_chunking(work_dir: Path, job: JobState) -> ChunkingRecovery` in `pyqenc/phases/recovery.py`
  - Load scene boundaries from `chunking.yaml`; compare expected chunk stems vs present files; classify each chunk as `ABSENT`, `ARTIFACT_ONLY` (no sidecar), or `COMPLETE`
  - For `ARTIFACT_ONLY` chunks: probe file and write `<chunk_stem>.yaml` sidecar
  - Update `chunk_video` / `split_chunks_from_state` to call recovery first; skip already-present chunks; write `<chunk_stem>.yaml` after each successful split; write `chunking.yaml` after scene detection
  - Add `.tmp` file cleanup at phase start
  - _Requirements: 2.2, 3.3, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 7.7_

- [x] 6. Implement `recover_attempts` shared utility





  - Add `recover_attempts(work_dir, chunk_ids, strategies, quality_targets) -> PhaseRecovery` in `pyqenc/phases/recovery.py`
  - For each `(chunk_id, strategy)` pair: check encoding result sidecar (`<chunk_id>.<res>.yaml`) → classify pair; if `ARTIFACT_ONLY`, scan attempt files and reconstruct `CRFHistory` from per-attempt sidecars
  - Return `PhaseRecovery` with `pairs`, `pending`, `did_work`
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 7. Update encoding phase: per-attempt sidecars and encoding result sidecars





  - Update `ChunkEncoder.encode_chunk` to write `<attempt_stem>.yaml` (all metrics + `targets_met` for human inspection) after each quality evaluation, replacing `.metrics.json`
  - Write `<chunk_id>.<res>.yaml` encoding result sidecar when CRF search converges
  - Update `_load_history_from_sidecars` to use new YAML format and reconstruct `CRFHistory` from per-attempt sidecars
  - Add `.tmp` file cleanup at phase start
  - _Requirements: 6a.1, 6a.2, 6a.3, 6a.4, 6b.1, 6b.2, 6b.3, 6b.4, 8.1, 8.2_

- [x] 8. Integrate recovery into optimization and encoding phases





  - Update `find_optimal_strategy` to call `recover_attempts` first; skip `COMPLETE` pairs; resume `ARTIFACT_ONLY` pairs from recovered `CRFHistory`; write `optimization.yaml` with crop and `test_chunks`; update with `optimal_strategy` on convergence
  - Update `encode_all_chunks` (or equivalent) to call `recover_attempts` first; skip `COMPLETE` pairs; resume `ARTIFACT_ONLY` pairs; write `encoding.yaml` with crop at phase start
  - Pre-validate crop params against `optimization.yaml` / `encoding.yaml` before recovery
  - _Requirements: 2.3, 2.4, 3.4, 3.5, 3.6, 3.7_

- [x] 9. Implement inputs discovery for standalone phase execution






  - Add `discover_inputs(phase, work_dir, job) -> InputsDiscovery` in `pyqenc/phases/recovery.py`
  - Each phase scans its prerequisite output dir and classifies each expected input as `ArtifactState`
  - Return critical error if any input is not `COMPLETE`
  - Wire into each phase entry point; auto-pipeline orchestrator bypasses discovery by passing outputs directly
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 11.1, 11.2, 11.3, 11.4, 11.5_

- [x] 10. Replace `ProgressTracker` with `JobStateManager` in orchestrator and remove legacy code





  - Update `PipelineOrchestrator.__init__` to accept `JobStateManager` instead of `ProgressTracker`
  - Remove all `tracker.update_phase`, `tracker.update_chunk`, `tracker.save_state` calls from orchestrator and phase modules
  - Delete `pyqenc/progress.py` (`ProgressTracker`) and remove `PipelineState`, `PhaseState`, `PhaseUpdate`, `PhaseMetadata`, `PhaseStatus` from `pyqenc/models.py` once all callers are migrated
  - _Requirements: 9.3, 9.5_

- [x] 10.1 Write unit tests for `ArtifactState` classification in `recover_attempts`


  - Test all three pair states (`ABSENT`, `ARTIFACT_ONLY`, `COMPLETE`) with and without sidecars
  - _Requirements: 6.2, 6.3_

- [x] 10.2 Write unit tests for `write_yaml_atomic` and ffmpeg runner `output_file` validation


  - Verify `.tmp` cleanup on failure; verify `ValueError` when output path not in cmd
  - _Requirements: 2.5, 7.1, 7.2_

- [x] 10.3 Write unit tests for `JobStateManager.validate` (all three modes)



  - Test dry-run warning, execute critical stop, `--force` wipe-and-continue
  - _Requirements: 1.2_

- [ ] 11. Run full automatic pipeline and ensure new logic is working, phase parameters and sidecars of all types are created. `uv run pyqenc auto` onto `"D:\_current\О чём говорят мужчины Blu-Ray (1080p) (1).mkv"` file using `D:\_current\pyqenc1` workdir and `-y` flag. No pipes - they break alive_progress. Check warnings. Check duration-based reporting. Check full plain run. If reruns are needed - check phase recovery. Sleep & watch the process output every 5 minutes.





- [ ] 12. Update Completed date on design.md and requirements.md of this spec.
