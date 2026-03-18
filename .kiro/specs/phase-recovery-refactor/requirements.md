# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-17

## Introduction

This document specifies requirements for refactoring the pipeline's state persistence and per-phase recovery logic. The current `progress.json` file mixes job parameters, source metadata, and phase progress into one large file that is rewritten on every update — making it fragile, hard to read, and difficult to reason about. The refactor replaces it with a small YAML job file (storing only stable job parameters) and per-phase YAML parameter files (storing the stable parameters used when a phase ran, for validation on restart). Encoding artifacts are always written to `.tmp` files first and renamed only on success, so the presence of a final artifact file is proof of its consistency. Each phase always runs and performs a recovery step first — scanning the filesystem, validating artifacts against stored parameters, and determining what work remains. Where recovery logic is shared across phases (notably chunk attempt discovery and CRF history reconstruction), it is extracted into a reusable utility.

The key design principle: **phase parameter files are not "phase complete" flags**. A phase always runs and recovers from what it finds on disk. The parameter file stores stable values (like crop params) that were active when the phase last ran; if those values have changed since the last run, the phase invalidates the affected artifacts and re-does the work.

## Glossary

- **Job file**: A YAML file (`job.yaml`) in the work directory storing stable, run-invariant parameters: source video metadata (path, size, duration, fps, resolution, frame count, crop params). No progress or phase status is stored here.
- **Phase parameter file**: A per-phase YAML file in the work directory (e.g. `extraction.yaml`, `chunking.yaml`, `optimization.yaml`) storing the stable parameters that were active when the phase last ran. Its presence does NOT mean the phase is complete — it is used during the **parameter pre-validation** step to decide whether existing artifacts are still valid.
- **Parameter pre-validation**: The first sub-step of phase recovery. Compares current run parameters (e.g. crop params, source video identity) against the values stored in the phase parameter file. If they differ, affected artifacts are invalidated before artifact recovery begins.
- **Inputs discovery**: A step performed before artifact recovery. Scans the expected input directory for artifacts produced by the prerequisite phase and validates them. Enables a phase to run standalone without requiring the full pipeline chain.
- **Artifact recovery**: The second sub-step of phase recovery. Scans the filesystem for existing artifacts, runs sanity checks on each (e.g. duration match, file non-empty), and builds the work-remaining set.
- **Sanity check**: A per-artifact validation during artifact recovery that compares a measured property of the artifact against an expected value (e.g. extracted video duration vs. source duration, sum of chunk durations vs. source duration).
- **Artifact**: A file produced by a phase (e.g. extracted stream `.mkv`, extracted audio `.mka`, chunk `.mkv`, encoded attempt `.mkv`). An artifact is considered consistent if and only if it was written via the `.tmp`-then-rename protocol.
- **`.tmp` protocol**: The convention of writing all output to a `<target>.tmp` file first and renaming to the final name only after the producing process exits successfully with a non-empty output. The presence of the final name (without `.tmp`) is proof of consistency.
- **Extraction recovery**: The artifact recovery sub-step for the extraction phase. Scans the `extracted/` directory for existing stream files, validates them (duration sanity check), and determines whether extraction needs to run.
- **Chunks recovery**: The artifact recovery sub-step for the chunking phase, and also a prerequisite check for the optimization and encoding phases. Scans the `chunks/` directory for existing chunk files, validates them against scene boundaries in `chunking.yaml`, and returns the list of valid chunks available for downstream phases.
- **Artifact state**: A three-value enum describing the recovery state of a single artifact:
  - `ABSENT` — the artifact file does not exist (not yet produced, or invalidated by a parameter change).
  - `ARTIFACT_ONLY` — the artifact file is present and consistent (written via `.tmp` protocol), but its sidecar is missing or incomplete (e.g. a chunk without a chunk sidecar, or an encoded attempt without a valid metrics sidecar).
  - `COMPLETE` — the artifact file is present and its sidecar is present and contains all required data (e.g. a chunk with a valid chunk sidecar, or an encoded attempt with a valid metrics sidecar containing all required metric keys).
  Recovery logic uses this enum to decide what work remains: `ABSENT` → produce the artifact; `ARTIFACT_ONLY` → produce only the sidecar; `COMPLETE` → no work needed.
- **CRF history**: The record of all CRF values attempted for a chunk+strategy pair, reconstructed from metrics sidecar files on disk during attempts recovery.
- **Attempts recovery**: The artifact recovery sub-step for the optimization and encoding phases. For each `(chunk_id, strategy)` pair independently, scans the encoded output directory for existing attempt files and their metrics sidecars, determines the `ArtifactState` of each attempt, reconstructs `CRFHistory`, and determines which pairs still need work.
- **Metrics sidecar**: A YAML file `<attempt_stem>.yaml` written atomically alongside each completed encoded attempt `.mkv` file. Contains `crf` (float) and `metrics` (dict of ALL measured metric values). No `targets_met` field — pass/fail is always re-evaluated from `metrics` against current quality targets.
- **Encoding result sidecar**: A YAML file `<chunk_id>.<res>.yaml` written in the strategy directory when the CRF search for a `(chunk_id, strategy)` pair concludes. Contains: winning attempt filename, winning CRF, quality targets active at the time, and measured metrics for the winning attempt. Its presence means the pair is `COMPLETE`.
- **Chunk sidecar**: A YAML file `<chunk_stem>.yaml` written atomically alongside each chunk `.mkv` file in the `chunks/` directory. Contains chunk metadata: `chunk_id`, `start_timestamp`, `end_timestamp`, `duration_seconds`, `frame_count`, `fps`, `resolution`. Its presence means the chunk was written successfully and its metadata is known without re-probing.
- **ProgressTracker**: The existing class in `pyqenc/progress.py` that will be replaced by the new `JobStateManager`.
- **JobStateManager**: The new class replacing `ProgressTracker`. Manages reading and writing `job.yaml` and all phase parameter YAML files.
- **JobState**: The new model replacing `PipelineState`. Contains only source video metadata and crop params — no phase status, no chunk tracking.
- **ExtractionParams**: Not applicable — extraction has no run-variable parameters, so no phase parameter file is written for extraction.
- **ChunkingParams**: Phase parameter file model for chunking (`chunking.yaml`). Contains: detected scene boundaries. Crop params are NOT stored here since chunking does not apply or depend on cropping.
- **OptimizationParams**: Phase parameter file model for optimization (`optimization.yaml`). Contains: crop params active when optimization ran, selected test chunk IDs, and (once determined) the optimal strategy name.
- **EncodingParams**: Phase parameter file model for encoding (`encoding.yaml`). Contains: crop params active when encoding ran.
- **CropParams**: Pydantic model holding `top`, `bottom`, `left`, `right` pixel offsets. `None` means detection has not yet run; an all-zero instance means no cropping is needed.
- **ChunkMetadata**: Pydantic model for a video chunk, subclass of `VideoMetadata`, adding `chunk_id`, `start_timestamp`, `end_timestamp`.
- **ffmpeg**: External video processing tool used for splitting, encoding, and probing.
- **mkvmerge**: External tool from mkvtoolnix used for MKV container muxing.

## Requirements

### Requirement 1

**User Story:** As a developer, I want a small, human-readable YAML job file that stores only stable source video parameters, so that I can inspect the job state at a glance without wading through progress data.

#### Acceptance Criteria

1. THE JobStateManager SHALL write a `job.yaml` file in the work directory containing only: source video path, file size in bytes, duration in seconds, fps, resolution, frame count, and crop params (omitted when `None` — meaning detection has not yet run).
2. WHEN the pipeline starts (in auto-pipeline mode or when any individual phase is invoked directly), THE JobStateManager SHALL load `job.yaml` if it exists and validate that the source video path, file size, and resolution match the current run; IF any field differs in dry-run mode, THE JobStateManager SHALL log a warning and take no further action; IF any field differs in execute mode without `--force`, THE JobStateManager SHALL log a critical-level message and stop execution; WHERE `--force` is provided in execute mode, THE JobStateManager SHALL log a warning, delete all intermediate artifacts and phase parameter files, and continue with the new source.
3. THE JobStateManager SHALL write `job.yaml` in YAML format using the `pyyaml` library; THE JobStateManager SHALL NOT write or read any JSON state file for job parameters.
4. WHEN `job.yaml` is written, THE JobStateManager SHALL use the shared `write_yaml_atomic` utility so a crash during writing never leaves a partial file.
5. THE JobStateManager SHALL NOT store any phase status, phase progress, or chunk tracking data in `job.yaml`.
6. WHEN the extraction phase runs with auto-detect crop params (no manual crop specified) and `job.yaml` already contains a `crop_params` value, THE Pipeline SHALL reuse the persisted crop params and skip crop detection; crop detection SHALL only run when `crop_params` is absent from `job.yaml` and no manual crop is specified.
7. WHEN the optimization or encoding phase starts and no manual crop params are specified, THE Pipeline SHALL read crop params from `job.yaml`; IF `job.yaml` contains no `crop_params` value, THE Pipeline SHALL log a critical-level message instructing the user to either specify crop params manually or re-run the extraction phase, and stop execution.

### Requirement 2

**User Story:** As a developer, I want each phase to write a YAML parameter file storing the stable parameters it used, so that subsequent runs can validate whether existing artifacts are still valid for the current parameters.

#### Acceptance Criteria

1. WHEN the extraction phase runs, THE Pipeline SHALL NOT write any phase parameter file since extraction has no run-variable parameters.
2. WHEN the chunking phase runs, THE Pipeline SHALL write `chunking.yaml` in the work directory containing: the list of detected scene boundaries (each with `frame` and `timestamp_seconds`). Crop params are NOT stored here since chunking does not apply or depend on cropping.
3. WHEN the optimization phase selects test chunks, THE Pipeline SHALL write `optimization.yaml` in the work directory containing: the crop params active for this run and the list of selected test chunk IDs (`test_chunks`); WHEN the optimal strategy is determined, THE Pipeline SHALL update `optimization.yaml` to also include the `optimal_strategy` field.
4. WHEN the encoding phase starts, THE Pipeline SHALL write `encoding.yaml` in the work directory containing the crop params active for this run.
5. THE Pipeline SHALL write all phase parameter files and YAML sidecars through a shared `write_yaml_atomic(path, data)` utility that uses the `.tmp`-then-rename protocol internally; callers pass the final target path and data dict and do not manage temp files themselves.
6. THE Pipeline SHALL write phase parameter files in YAML format using the `pyyaml` library; THE Pipeline SHALL NOT use JSON for phase parameter files.

### Requirement 3

**User Story:** As a developer, I want each phase to always execute its full logic with a two-step recovery first — parameter pre-validation then artifact recovery — so that the pipeline can resume correctly from any interrupted state without consulting a central progress file.

#### Acceptance Criteria

1. WHEN a phase starts, THE Pipeline SHALL call a dedicated recovery function for that phase; the recovery function SHALL execute in two steps: (a) **parameter pre-validation** — compare current run parameters against the stored phase parameter file and invalidate affected artifacts early if they differ; (b) **artifact recovery** — scan the filesystem for existing artifacts, run sanity checks on each, and return a structured result describing what work remains. The phase then executes its full work plan, treating each validated existing artifact as an already-completed step and performing work only for steps where no valid artifact exists. The recovery result SHALL include a flag indicating whether any actual work was performed, so the orchestrator can set the correct `PhaseOutcome` (`REUSED` when all artifacts were valid, `COMPLETED` when new work was done).
2. THE extraction phase recovery has no parameter pre-validation step (extraction has no run-variable parameters). Source file identity is validated by `JobStateManager` at startup — before any phase runs, in both auto-pipeline and standalone phase modes — via `job.yaml` (Req 1.2). Extraction proceeds directly to artifact recovery.
3. THE chunking phase recovery has no parameter pre-validation step (chunking has no run-variable parameters). It proceeds directly to artifact recovery: check for existing chunk files and scene boundaries in `chunking.yaml`.
4. THE optimization phase recovery SHALL pre-validate: crop params in `optimization.yaml` against the current source crop params; IF they differ, THE Pipeline SHALL log a critical-level message describing the mismatch and stop execution; WHERE the `--force` flag is provided, THE Pipeline SHALL delete all optimization attempt artifacts and re-select test chunks.
5. THE encoding phase recovery SHALL pre-validate: crop params in `encoding.yaml` against the current source crop params; IF they differ, THE Pipeline SHALL log a critical-level message describing the mismatch and stop execution; WHERE the `--force` flag is provided, THE Pipeline SHALL delete all encoded attempt artifacts for all strategies.
6. AFTER parameter pre-validation (where applicable), each phase SHALL perform artifact recovery: chunking compares expected chunk stems vs present chunk files; optimization and encoding check each attempt's metrics sidecar for completeness. Extraction and chunk artifacts are accepted as-is when present (`.tmp` protocol guarantees consistency).
7. WHEN a sanity check fails for a specific artifact, THE Pipeline SHALL log a warning identifying the artifact and the failed check, treat that artifact as invalid, and include it in the work-remaining set; THE Pipeline SHALL NOT abort the entire phase for a single artifact failure.

### Requirement 4

**User Story:** As a developer, I want the extraction phase recovery to determine whether extraction needs to run, so that existing valid artifacts are reused without re-extracting.

#### Acceptance Criteria

1. WHEN the extraction phase recovery function finds existing extracted artifact files (written via the `.tmp` protocol), THE Pipeline SHALL treat them as valid and skip re-extraction; no duration re-probing is needed since the `.tmp` protocol guarantees consistency.
2. WHEN the extraction phase recovery function finds no extracted artifact files, THE Pipeline SHALL treat extraction as needing a full run.
3. WHEN the extraction phase recovery function determines that extraction is complete and valid, THE Pipeline SHALL log an info-level message confirming reuse and listing the artifact files found.

### Requirement 5

**User Story:** As a developer, I want the chunking phase recovery to determine which chunks still need to be split, so that already-split chunks are reused and only missing chunks are re-split.

#### Acceptance Criteria

1. THE chunking phase recovery has no parameter pre-validation step. It proceeds directly to artifact recovery: check for `chunking.yaml` and existing chunk files on disk.
2. WHEN `chunking.yaml` is present with scene boundaries, THE chunking phase recovery SHALL compare the set of expected chunk stems (derived from scene boundary timestamps) against the set of chunk files present on disk; chunks present on disk are treated as valid (`.tmp` protocol guarantees consistency); missing chunks are added to the work-remaining set.
3. WHEN the chunking phase recovery finds `chunking.yaml` with valid scene boundaries, THE Pipeline SHALL load the scene boundaries and skip scene detection.
4. WHEN `chunking.yaml` is absent, THE Pipeline SHALL treat all chunks as needing work (scene detection must run first).
5. WHEN a chunk file is successfully written, THE Pipeline SHALL write a chunk sidecar YAML file `<chunk_stem>.yaml` alongside it containing: `chunk_id`, `start_timestamp`, `end_timestamp`, `duration_seconds`, `frame_count`, `fps`, and `resolution`.
6. WHEN the chunking phase recovery finds a chunk file on disk with a corresponding chunk sidecar, THE Pipeline SHALL read the sidecar to obtain chunk metadata without re-probing the file.
7. WHEN the chunking phase recovery finds a chunk file on disk without a corresponding chunk sidecar, THE Pipeline SHALL probe the chunk file to obtain its metadata and write the chunk sidecar.

### Requirement 6

**User Story:** As a developer, I want a shared attempts recovery procedure used by both the optimization and encoding phases, so that chunk attempt discovery and CRF history reconstruction are consistent and not duplicated.

#### Acceptance Criteria

1. THE Pipeline SHALL implement a `recover_attempts` function (or equivalent method) that accepts a work directory, a list of chunk IDs, and a list of strategies; THE Pipeline SHALL call this function from both the optimization and encoding phase recovery steps.
2. FOR each `(chunk_id, strategy)` pair, THE Pipeline SHALL determine the `ArtifactState` based on the encoding result sidecar (`<chunk_id>.<res>.yaml` in the strategy directory): (a) `COMPLETE` — encoding result sidecar present AND the referenced attempt file exists on disk AND re-evaluating the sidecar metrics against current quality targets passes; (b) `ARTIFACT_ONLY` — no valid encoding result sidecar (absent, stale, or attempt file missing), but at least one attempt `.mkv` file exists; the CRF search is in progress; (c) `ABSENT` — no attempt files at all; the search has not started.
3. WHEN `recover_attempts` processes a pair in `ARTIFACT_ONLY` state, THE Pipeline SHALL scan all attempt `.mkv` files for that pair, read their per-attempt sidecars to reconstruct `CRFHistory`, and return the history so the CRF search can resume from where it left off.
4. WHEN `recover_attempts` processes a pair in `COMPLETE` state, THE Pipeline SHALL read the encoding result sidecar to obtain the winning CRF and metrics; THE Pipeline SHALL re-evaluate whether the result still meets the current quality targets (targets may have changed since the sidecar was written).
5. THE `recover_attempts` function SHALL validate that each discovered attempt file still exists on disk before including it in the recovered history; IF the file is missing, THE Pipeline SHALL log a warning and skip that entry.

### Requirement 6a

**User Story:** As a developer, I want a per-attempt metrics sidecar that stores all measured metric values (not just user-targeted ones), so that the CRF history is reusable when quality targets change.

#### Acceptance Criteria

1. WHEN `QualityEvaluator.evaluate_chunk` completes for an encoded attempt, THE Pipeline SHALL write a per-attempt sidecar `<attempt_stem>.yaml` containing: `crf` (float), `targets_met` (bool, for human inspection only), and `metrics` (dict of ALL measured metric values keyed by `<metric>_<statistic>`, not filtered to current targets).
2. THE per-attempt sidecar `targets_met` field is for human inspection only — the algorithm SHALL always re-evaluate pass/fail from `metrics` against the current quality targets at recovery time, never from the persisted `targets_met`.
3. WHEN `ChunkEncoder` finds an existing attempt `.mkv` file and a corresponding per-attempt sidecar, THE Pipeline SHALL read all metrics from the sidecar and re-evaluate pass/fail against the current quality targets.
4. WHEN `ChunkEncoder` finds an existing attempt `.mkv` file but no per-attempt sidecar, THE Pipeline SHALL re-run quality evaluation and write the sidecar.

### Requirement 6b

**User Story:** As a developer, I want a per-chunk encoding result sidecar written when the CRF search concludes, so that recovery can immediately determine whether a chunk+strategy pair is fully done without re-scanning all attempt files.

#### Acceptance Criteria

1. WHEN `ChunkEncoder` determines the winning CRF for a `(chunk_id, strategy)` pair (CRF search converged), THE Pipeline SHALL write an encoding result sidecar `<chunk_id>.<res>.yaml` in the strategy directory containing: the winning attempt filename, the winning CRF, the quality targets that were active, and only the targeted metric values for the winning attempt. `chunk_id` and `strategy` are NOT stored — they are derived from the filename and directory placement.
2. THE encoding result sidecar SHALL be written using `write_yaml_atomic` so a crash during writing never leaves a partial file.
3. WHEN `recover_attempts` finds an encoding result sidecar for a pair, THE Pipeline SHALL treat that pair as `COMPLETE` and skip re-scanning attempt files for CRF history reconstruction.
4. WHEN the encoding result sidecar references an attempt file that no longer exists on disk, THE Pipeline SHALL log a warning, delete the encoding result sidecar, and treat the pair as `ARTIFACT_ONLY` so the CRF search can resume.

### Requirement 7

**User Story:** As a developer, I want the unified ffmpeg runner to enforce the `.tmp`-then-rename protocol for all file-producing calls, so that the presence of a final artifact file is always proof of its consistency and future developers cannot accidentally bypass the protocol.

#### Acceptance Criteria

1. THE `run_ffmpeg_async` (and its sync wrapper `run_ffmpeg`) SHALL add a mandatory `output_file: Path | list[Path] | None` parameter; callers MUST pass the intended output `Path` (or list of `Path`s for multi-output commands) when ffmpeg produces file output, or `None` explicitly when no file output is expected (e.g. frame counting, metadata probing, null-encode).
2. WHEN `output_file` is a `Path` or `list[Path]`, THE runner SHALL scan `cmd` for each provided path; IF any path is not found in `cmd`, THE runner SHALL raise `ValueError` immediately before launching the subprocess, so the bug is caught at the call site.
3. WHEN `output_file` is a `Path` or `list[Path]`, THE runner SHALL replace every occurrence of each output path in `cmd` with a sibling temp file named `<stem>.tmp` (no original suffix, so glob patterns for artifacts and `.tmp` files are cleanly separable) before launching the subprocess.
4. WHEN ffmpeg exits with code 0 and all temp files are non-empty, THE runner SHALL rename each temp file to its corresponding final output path; WHEN a rename fails due to a cross-device move, THE runner SHALL fall back to copy-then-delete and log a debug message.
5. WHEN ffmpeg exits with a non-zero code or any temp file is missing or empty, THE runner SHALL delete all temp files (if they exist) and return a failed `FFmpegRunResult`; THE runner SHALL NOT leave partial `.tmp` files on disk.
6. ALL existing callers of `run_ffmpeg` / `run_ffmpeg_async` SHALL be updated to pass the correct `output_file` argument; callers in `pyqenc/phases/extraction.py`, `pyqenc/phases/chunking.py`, and `pyqenc/phases/encoding.py` that currently implement their own `.tmp`-then-rename logic SHALL remove that logic entirely after the runner handles it.
7. WHEN a phase starts, THE Pipeline SHALL scan its output directory for any leftover `.tmp` files from a previous interrupted run and delete them, logging a warning for each one found.
8. THE Pipeline SHALL treat the presence of a final artifact file (without `.tmp` suffix) as proof that the file was written completely and successfully; THE Pipeline SHALL NOT perform additional integrity checks (e.g. re-probing duration) when a sidecar is also present.

### Requirement 8

**User Story:** As a developer, I want metrics plots written alongside encoded attempts, so that they are easy to find alongside their artifact.

#### Acceptance Criteria

1. WHEN `QualityEvaluator.evaluate_chunk` produces a metrics plot, THE Pipeline SHALL write the plot as `<attempt_stem>.png` alongside the attempt `.mkv` file (not in a separate subdirectory).
2. WHEN `ChunkEncoder` finds an existing attempt `.mkv` file but no `.yaml` sidecar (including the case where only a legacy `.metrics.json` sidecar exists), THE Pipeline SHALL re-run quality evaluation and write the new `.yaml` sidecar.

### Requirement 9

**User Story:** As a developer, I want the `JobStateManager` to replace `ProgressTracker` as the single source of truth for job and phase state, so that there is no ambiguity about where state is stored or how it is accessed.

#### Acceptance Criteria

1. THE Pipeline SHALL implement `JobStateManager` in `pyqenc/state.py` (new file) with typed methods for each YAML file it manages: `load_job` / `save_job` for `job.yaml`, and typed load/save methods for each phase parameter model (`load_chunking_params` / `save_chunking_params`, `load_optimization_params` / `save_optimization_params`, `load_encoding_params` / `save_encoding_params`); THE `JobStateManager` SHALL NOT expose a generic string-keyed file accessor.
2. THE `JobStateManager` SHALL expose typed accessors for each phase parameter file (`extraction_params`, `chunking_params`, `optimization_params`, `encoding_params`) returning the appropriate typed model or `None` if the file does not exist.
3. THE Pipeline SHALL update `PipelineOrchestrator` to accept a `JobStateManager` instead of a `ProgressTracker`; THE Pipeline SHALL remove all references to `ProgressTracker` from the orchestrator and phase modules.
4. WHEN `JobStateManager` is initialized with a work directory that does not yet exist, THE `JobStateManager` SHALL create the directory on first write.
5. THE Pipeline SHALL remove `ProgressTracker` and `PipelineState` from the codebase once all callers have been migrated to `JobStateManager`; no deprecated code SHALL remain after this refactor is complete.

### Requirement 10

**User Story:** As a developer, I want the pipeline to be fool-proofed against users running phases out of order or with incomplete prerequisites, so that an incomplete or inconsistent state is detected and reported clearly rather than silently producing wrong results.

#### Acceptance Criteria

1. WHEN a phase starts and its prerequisite phase has not produced a complete set of valid artifacts (all artifacts must be in `COMPLETE` state — `ARTIFACT_ONLY` or `ABSENT` are not acceptable as prerequisites), THE Pipeline SHALL log a critical-level message naming the missing prerequisite and stop execution; `--force` SHALL NOT override this check — incomplete prerequisites require the user to re-run the prerequisite phase.
2. WHEN the encoding phase starts and inputs discovery finds that not all expected chunks are present and valid (`COMPLETE`), THE Pipeline SHALL refuse to proceed and instruct the user to run the chunking phase or the full auto-pipeline.
3. WHEN the encoding phase starts with optimization enabled and `optimization.yaml` does not contain an `optimal_strategy` value, THE Pipeline SHALL refuse to proceed and instruct the user to run the optimization phase first.
4. WHEN the optimization phase starts and inputs discovery finds that not all expected chunks are present and valid (`COMPLETE`), THE Pipeline SHALL refuse to proceed and instruct the user to run the chunking phase or the full auto-pipeline.
5. WHEN the merge phase starts and the encoding phase has not produced a complete set of encoded chunks (at least one chunk+strategy pair still `ABSENT`), THE Pipeline SHALL refuse to proceed and instruct the user to complete the encoding phase first.
6. THE Pipeline SHALL log a clear, actionable error message for each prerequisite failure, identifying which phase is incomplete, what is missing, and what the user should do to resolve it.

### Requirement 11

**User Story:** As a developer, I want each phase to be able to discover its input artifacts independently from the filesystem when run standalone, so that a user can run a single phase directly without having to run the full pipeline from the start.

#### Acceptance Criteria

1. WHEN a phase is invoked standalone (not as part of the auto-pipeline), THE Pipeline SHALL perform **inputs discovery** before artifact recovery: scan the expected input directory for the artifacts produced by the prerequisite phase and determine the `ArtifactState` of each expected input.
2. WHEN the auto-pipeline orchestrator invokes a phase, THE Pipeline MAY pass the prerequisite phase outputs directly to the next phase, bypassing inputs discovery; the orchestrator is responsible for ensuring the outputs are complete before passing them.
3. WHEN inputs discovery finds that all expected input artifacts are in `COMPLETE` state, THE Pipeline SHALL proceed to parameter pre-validation and artifact recovery.
4. WHEN inputs discovery finds any expected input artifact that is not in `COMPLETE` state (`ARTIFACT_ONLY` or `ABSENT`), THE Pipeline SHALL log a critical-level message describing which inputs are incomplete and stop execution, instructing the user to run the prerequisite phase first.
5. THE inputs discovery result SHALL be passed to the phase's own artifact recovery step so the phase can cross-reference its own artifacts against the discovered inputs.
