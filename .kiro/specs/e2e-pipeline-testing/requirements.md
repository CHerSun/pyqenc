# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-15


## Introduction

This document specifies requirements for end-to-end testing of the pyqenc quality-based encoding pipeline on a real video file. The goal is to validate that the full automatic pipeline runs correctly from extraction through merge, that each phase completes successfully, and that the pipeline correctly detects and resumes from partial work across all phases — including the two-sub-phase chunking split (scene detection done but splitting not yet done), and partial encoding (some chunks encoded, others not).

The spec has been updated to reflect the `ffv1-lossless-chunking` implementation: chunking now defaults to FFV1 lossless re-encode (`ChunkingMode.LOSSLESS`), crop is no longer applied during chunking (it is applied during encoding and metrics calculation only), and disk space estimates are mode-aware.

## Glossary

- **Pipeline**: The full sequence of phases: Extraction → Chunking → Encoding → Audio → Merge.
- **Phase**: One discrete step in the pipeline (extraction, chunking, encoding, audio, merge).
- **Resumption**: The ability of a phase to detect already-completed work and skip it, performing only the remaining work.
- **Scene Detection Sub-phase**: The first half of chunking — running PySceneDetect and persisting scene boundaries to state.
- **Chunk Splitting Sub-phase**: The second half of chunking — splitting the video at persisted boundaries.
- **Partial Encoding**: A state where some chunks have been encoded to the target quality but others have not.
- **Work Directory**: The directory `D:\_current\pyqenc1` used for all intermediate files.
- **Source Video**: The file `D:\_current\О чём говорят мужчины Blu-Ray (1080p) (1).mkv`.
- **Automatic Cropping**: Black border detection during extraction; crop filter applied during encoding and metrics calculation (not during chunking).
- **progress.json**: The state file written by ProgressTracker to the work directory.
- **CRF**: Constant Rate Factor — the quality parameter passed to video encoders.
- **VMAF**: Video Multi-method Assessment Fusion — the primary quality metric.
- **ChunkingMode**: Enum with values `LOSSLESS` (FFV1 all-intra, default) and `REMUX` (stream-copy). Controls how chunks are split.
- **FFV1**: A lossless all-intra codec used in `LOSSLESS` chunking mode; produces frame-perfect splits but ~5x larger chunk files.

## Requirements

### Requirement 1

**User Story:** As a developer, I want the full pipeline to run end-to-end on the sample video, so that I can verify the complete workflow produces a valid output file.

#### Acceptance Criteria

1. WHEN the pipeline is invoked with `pyqenc auto` on the source video with `--work-dir D:\_current\pyqenc1` and `-y`, THE Pipeline SHALL complete all five phases (extraction, chunking, encoding, audio, merge) without a fatal error.
2. WHEN the pipeline completes, THE Pipeline SHALL produce at least one output MKV file in `D:\_current\pyqenc1\final\`.
3. WHEN automatic cropping is enabled (default), THE Pipeline SHALL detect black borders during extraction and log the detected crop parameters at info level; THE Pipeline SHALL apply the crop filter during encoding and metrics calculation, not during chunking.
4. WHEN each phase completes, THE Pipeline SHALL log a success message at info level indicating the phase name and a summary of work done (e.g. number of chunks, streams, files).
5. WHEN the pipeline finishes, THE Pipeline SHALL log the path(s) of the final output file(s).
6. WHEN `--remux-chunking` is not passed, THE Pipeline SHALL use `ChunkingMode.LOSSLESS` and log "Chunking mode: lossless FFV1" before splitting begins.

### Requirement 2

**User Story:** As a developer, I want each phase to correctly detect and reuse already-completed work on restart, so that re-running the pipeline after a full successful run does not repeat any work.

#### Acceptance Criteria

1. WHEN the pipeline is run a second time after a successful full run, THE Pipeline SHALL report all phases as reused/complete and SHALL NOT re-execute any phase.
2. WHEN the extraction phase is run and extracted files already exist in `extracted/`, THE Pipeline SHALL log that files are being reused and SHALL NOT invoke mkvextract again.
3. WHEN the chunking phase is run and all chunks already exist in `chunks/` with valid scene boundaries in `progress.json`, THE Pipeline SHALL skip both scene detection and chunk splitting.
4. WHEN the encoding phase is run and all chunks are already encoded to the target quality, THE Pipeline SHALL report all chunks as reused and SHALL NOT invoke ffmpeg for encoding.
5. WHEN the audio phase is run and processed audio files already exist in `audio/`, THE Pipeline SHALL reuse them without re-processing.
6. WHEN the merge phase is run and the final output file already exists in `final/`, THE Pipeline SHALL reuse it without re-merging.

### Requirement 3

**User Story:** As a developer, I want the chunking phase to correctly resume when scene detection is complete but chunk splitting has not started, so that scene detection work is never repeated unnecessarily.

#### Acceptance Criteria

1. WHEN `progress.json` contains scene boundaries for the chunking phase but no chunk files exist in `chunks/`, THE Pipeline SHALL skip scene detection and proceed directly to chunk splitting.
2. WHEN the chunking phase resumes from the scene-detection-done state, THE Pipeline SHALL log "Scene boundaries already in state (N) -- skipping detection."
3. WHEN chunk splitting completes after resumption, THE Pipeline SHALL produce the same set of chunk files as a fresh run would produce.

### Requirement 4

**User Story:** As a developer, I want the encoding phase to correctly resume when some chunks are already encoded but others are not, so that partial encoding progress is preserved across restarts.

#### Acceptance Criteria

1. WHEN some chunk files exist in `encoded/<strategy>/` and meet the quality target, and others do not, THE Pipeline SHALL encode only the missing or non-qualifying chunks.
2. WHEN the encoding phase resumes, THE Pipeline SHALL log the count of reused chunks and the count of chunks that need encoding.
3. WHEN the encoding phase completes after partial resumption, THE Pipeline SHALL produce encoded files for all chunks under the strategy directory.
4. IF a chunk's encoded file exists but its quality metrics do not meet the current target, THEN THE Pipeline SHALL re-encode that chunk.

### Requirement 5

**User Story:** As a developer, I want the automated e2e test suite to correctly instantiate the pipeline with `ChunkingMode.REMUX` for speed, so that tests pass without requiring full FFV1 encodes.

#### Acceptance Criteria

1. THE automated e2e tests in `tests/e2e/test_complete_pipeline.py` SHALL construct `PipelineConfig` with `chunking_mode=ChunkingMode.REMUX` to avoid slow FFV1 re-encodes during testing.
2. THE automated e2e tests SHALL construct `PipelineOrchestrator` with only `config` and `tracker` arguments (no `ConfigManager` — the constructor does not accept a third argument).
3. WHEN `PipelineConfig` is constructed without an explicit `chunking_mode`, THE Pipeline SHALL default to `ChunkingMode.LOSSLESS`; tests that rely on fast execution SHALL pass `chunking_mode=ChunkingMode.REMUX` explicitly.

### Requirement 6

**User Story:** As a developer, I want the entire automated test suite to be green after the e2e-pipeline-testing updates, so that CI reflects a correct baseline.

#### Acceptance Criteria

1. WHEN `pytest` is run against the full test suite, THE Test Suite SHALL report zero failing tests.
2. THE unit tests in `tests/unit/` SHALL pass without requiring external tools (ffmpeg, mkvtoolnix) beyond what is already mocked.
3. THE integration tests in `tests/integration/` SHALL pass with the updated `ChunkingMode.REMUX` fixtures where applicable.
4. THE e2e tests in `tests/e2e/` SHALL pass in dry-run mode without requiring the real source video or a full pipeline execution.

### Requirement 7

**User Story:** As a developer, I want the test procedure to be documented as a step-by-step script, so that the validation can be repeated consistently.

#### Acceptance Criteria

1. THE Test Plan SHALL document the exact CLI commands to run for each test scenario.
2. THE Test Plan SHALL specify what log output or file artifacts to check to confirm each scenario passed.
3. THE Test Plan SHALL cover: full run (lossless mode), full run (remux mode), full restart (all reused), chunking partial restart (scenes done, no chunks), encoding partial restart (half chunks encoded).
