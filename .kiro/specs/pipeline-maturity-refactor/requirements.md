# Requirements Document

<!-- markdownlint-disable MD024 -->

## Introduction

This document specifies requirements for maturing the pyqenc pipeline codebase. The work covers four interconnected areas: (1) consolidating all video metadata into proper typed classes and eliminating bare `Path` usage for intermediate data; (2) making `ProgressTracker` accept typed status updates and ensuring it is flushed on crash or external kill; (3) splitting scene detection into two independently resumable sub-phases; (4) fixing scene detection correctness issues; and (5) removing the legacy module tree entirely, integrating any still-needed logic into the main codebase.

## Glossary

- **VideoMetadata**: A typed class holding all properties of a video file (path, duration, fps, resolution, frame count, start frame offset), with transparent lazy-loading of all probe-derived fields.
- **ChunkVideoMetadata**: A subclass of VideoMetadata that adds a `chunk_id: str` field. The chunk's starting frame offset within the source video is represented by the inherited `start_frame` field.
- **ProgressTracker**: The persistent JSON-based state manager in `pyqenc/progress.py`.
- **PipelineState**: The top-level dataclass serialized by ProgressTracker into `progress.json`.
- **Scene Detection Sub-phase**: The first half of chunking — running a scene detector and persisting the list of scene boundaries.
- **Chunk Splitting Sub-phase**: The second half of chunking — splitting the video at persisted scene boundaries and recording chunk metadata.
- **Legacy Modules**: The directory tree under `pyqenc/legacy/` (pymkvextract, pymkvcompare, pymkva2, metrics_visualization).
- **PySceneDetect**: The third-party Python library currently used for scene detection (`scenedetect` package).
- **ContentDetector**: The PySceneDetect detector class used to find scene boundaries based on content changes.
- **ffmpeg**: External video processing tool used for splitting, encoding, and probing.
- **mkvtoolnix**: External tool suite (mkvmerge, mkvextract) used for MKV container operations.
- **CRF**: Constant Rate Factor — the quality parameter passed to video encoders.
- **AttemptInfo**: Dataclass recording a single encoding attempt (CRF, metrics, success flag).
- **PhaseStatus**: Enum with values NOT_STARTED, IN_PROGRESS, COMPLETED, FAILED.
- **signal handler**: An OS-level callback registered to intercept SIGTERM / SIGINT (or Windows equivalents) so the process can flush state before exiting.

## Requirements

### Requirement 1

**User Story:** As a developer, I want all video file data represented as typed `VideoMetadata` objects with transparent lazy-loading, so that metadata is co-located with the file reference, never fetched twice, and callers do not need to know how or when it was obtained.

#### Acceptance Criteria

1. THE Pipeline SHALL define a `VideoMetadata` class in `pyqenc/models.py` that holds `path: Path`, `duration_seconds: float | None`, `frame_count: int | None`, `fps: float | None`, `resolution: str | None`, and `start_frame: int` (defaulting to 0), where all probe-derived fields are exposed as properties that fetch and cache their value on first access without any explicit call from the caller.
2. THE Pipeline SHALL define a `ChunkVideoMetadata` class that extends `VideoMetadata` with a `chunk_id: str` field, using the inherited `start_frame-end_frame` format using its fields to represent the chunk's starting frame offset within the source video and ending frame.
3. WHEN any pipeline phase needs video file properties (duration, fps, resolution, frame count), THE Pipeline SHALL obtain them from the corresponding `VideoMetadata` or `ChunkVideoMetadata` instance; callers SHALL NOT need to know whether the value was cached or freshly fetched.
4. THE Pipeline SHALL populate `VideoMetadata` fields opportunistically from data already available in intermediate ffmpeg or ffprobe runs (e.g. frame count from a null-output encode, duration and fps from ffprobe stream info), so that no redundant probing calls are made.
5. THE Pipeline SHALL pass the same `VideoMetadata` instance through all phases that operate on the same file, so that cached values are reused without re-initialization.
6. WHEN `ProgressTracker` is initialized with an existing `progress.json`, THE Pipeline SHALL compare the persisted metadata fields against the live `VideoMetadata` values and log a warning if any field differs, indicating that the source file may have changed externally. In this case work item must be flagged as not started to redo and reach consistent result.
7. THE Pipeline SHALL remove the separate `SourceVideoMetadata` and `ChunkMetadata` dataclasses from `pyqenc/models.py` once all call sites are migrated to the new classes.
8. THE Pipeline SHALL update `ProgressTracker` serialization and deserialization to use `VideoMetadata` and `ChunkVideoMetadata` in place of the old dataclasses.
9. IF a metadata field cannot be determined (e.g. ffprobe fails), THEN THE Pipeline SHALL store `None` for that field and log a warning rather than raising an exception.

### Requirement 2

**User Story:** As a developer, I want `ProgressTracker` to accept typed status update objects, so that updates are self-describing and the JSON state file always reflects the full typed state.

#### Acceptance Criteria

1. THE Pipeline SHALL define a `PhaseUpdate` Pydantic model in `pyqenc/models.py` containing `phase: str`, `status: PhaseStatus`, and `metadata: PhaseMetadata | None`, where `PhaseMetadata` is a typed Pydantic model covering all known phase metadata shapes (e.g. `crop_params` for extraction, `scene_boundaries` for chunking).
2. THE Pipeline SHALL define a `ChunkUpdate` dataclass containing `chunk_id: str`, `strategy: str`, and `attempt: AttemptInfo`.
3. WHEN `ProgressTracker.update_phase` is called, THE Pipeline SHALL accept a `PhaseUpdate` instance as its primary argument (while keeping backward-compatible keyword overloads if needed).
4. WHEN `ProgressTracker.update_chunk` is called, THE Pipeline SHALL accept a `ChunkUpdate` instance as its primary argument.
5. THE Pipeline SHALL ensure that every field of `PhaseUpdate` and `ChunkUpdate` is serialized into `progress.json` without data loss.

### Requirement 3

**User Story:** As a developer, I want `ProgressTracker` to flush its in-memory state to disk when the process crashes or is killed externally, so that no progress is lost on unexpected termination.

#### Acceptance Criteria

1. WHEN `ProgressTracker` is initialized, THE Pipeline SHALL register signal handlers for SIGINT and SIGTERM (and on Windows, the equivalent console control events) that call `flush()` before allowing the process to exit.
2. WHEN an unhandled exception propagates to the top-level entry point, THE Pipeline SHALL call `ProgressTracker.flush()` before re-raising or exiting.
3. THE Pipeline SHALL use `atexit` registration as an additional safety net to call `flush()` on normal interpreter shutdown.
4. IF `flush()` is called and there are no pending updates, THE Pipeline SHALL complete without writing to disk and without raising an exception.
5. THE Pipeline SHALL log a warning-level message when a flush is triggered by a signal or unhandled exception, indicating that the process is terminating.

### Requirement 4

**User Story:** As a developer, I want scene detection and chunk splitting to be two independently resumable sub-phases, so that re-running the pipeline after a crash does not repeat expensive scene detection work.

#### Acceptance Criteria

1. THE Pipeline SHALL split the current `chunk_video` function into two distinct operations: `detect_scenes_to_state` and `split_chunks_from_state`.
2. WHEN `detect_scenes_to_state` completes, THE Pipeline SHALL persist the list of detected scene boundary timestamps and frame numbers into `PipelineState` via `ProgressTracker` before returning.
3. WHEN the chunking phase starts and scene boundaries are already persisted in `PipelineState`, THE Pipeline SHALL skip `detect_scenes_to_state` and proceed directly to `split_chunks_from_state`.
4. WHEN `split_chunks_from_state` completes for a chunk, THE Pipeline SHALL record that chunk's `ChunkVideoMetadata` (including `start_frame` offset) in `PipelineState` immediately.
5. WHEN the chunking phase starts and some chunks are already split and recorded in `PipelineState`, THE Pipeline SHALL skip those chunks and only split the remaining ones.
6. THE Pipeline SHALL expose the two sub-phases as separate callable functions with clear docstrings, so they can be invoked independently from tests or the CLI.

### Requirement 5

**User Story:** As a developer, I want scene detection to reliably produce scene boundaries and chunks, so that the pipeline does not silently produce zero chunks or miss scene cuts.

#### Acceptance Criteria

1. WHEN `detect_scenes_to_state` runs and detects zero scenes, THE Pipeline SHALL log a warning and treat the entire video as a single scene (one chunk covering the full duration).
2. WHEN `split_chunks_from_state` runs, THE Pipeline SHALL verify that each output chunk file exists and has a non-zero file size after splitting; IF a chunk file is missing or empty, THEN THE Pipeline SHALL log a critical error and mark that chunk as FAILED in `PipelineState`.
3. THE Pipeline SHALL pass scene boundaries to the ffmpeg splitter using the exact timestamp values returned by the detector, without rounding or truncation that could cause off-by-one frame errors.
4. WHEN applying crop parameters during splitting, THE Pipeline SHALL pass the crop filter as a proper ffmpeg `-vf` argument and verify the output dimensions match the expected cropped dimensions.
5. THE Pipeline SHALL log the number of detected scenes and the number of resulting chunks at info level after each sub-phase completes.

### Requirement 7

**User Story:** As a developer, I want chunk files named after their frame range and encoded attempts named with their CRF value, so that file names are stable across restarts and immediately convey their content without consulting the progress file.

#### Acceptance Criteria

1. THE Pipeline SHALL name each source chunk file using the pattern `chunk.<start_frame_padded>-<end_frame_padded>.mkv`, where both frame numbers are zero-padded to the same width derived from the total source frame count (e.g. `chunk.000000-000319.mkv`). This should match chunk id (without chunk prefix). Padding to 6 positions total seems reasonable, videos often range in 100000 to 200000 frames total.
2. THE Pipeline SHALL derive the end frame number as `start_frame + frame_count - 1` so the name encodes an inclusive range.
3. WHEN the chunking phase resumes after a restart, THE Pipeline SHALL identify already-split chunks by matching existing file names against the expected frame-range pattern, without relying on sequential counters.
4. THE Pipeline SHALL name each encoded attempt file by appending the resolution, attempt suffix, and CRF to the source chunk name, using the pattern `chunk.<start>-<end>.<width>x<height>.attempt_<N>.crf<CRF>.mkv` (e.g. `chunk.000000-000319.1920x800.attempt_1.crf20.mkv`), stored under the strategy subfolder. The resolution reflects the actual output dimensions after cropping, preventing conflicts when crop parameters change between runs.
5. THE Pipeline SHALL update `ChunkVideoMetadata` to derive `chunk_id` from the frame-range name rather than a sequential integer, so that the id is stable and unique regardless of insertion order.

### Requirement 6

**User Story:** As a developer, I want the legacy module tree removed from the codebase, so that there is a single authoritative implementation for each concern.

#### Acceptance Criteria

1. THE Pipeline SHALL migrate any logic from `pyqenc/legacy/pymkvextract/` that is actively used by `pyqenc/phases/extraction.py` into `pyqenc/phases/extraction.py` or a new utility module, and remove the import of `MKVTrackExtractor` and `streams_filter_plain_regex` from the legacy path.
2. THE Pipeline SHALL migrate any logic from `pyqenc/legacy/pymkvcompare/` that is actively used by quality measurement into the main codebase (e.g. `pyqenc/quality.py`), removing the legacy import.
3. THE Pipeline SHALL migrate any logic from `pyqenc/legacy/pymkva2/` that is actively used by `pyqenc/phases/audio.py` into the main codebase, removing the legacy import.
4. THE Pipeline SHALL migrate any logic from `pyqenc/legacy/metrics_visualization/` that is actively used by plotting or reporting into the main codebase (e.g. `pyqenc/utils/`), removing the legacy import.
5. WHEN all legacy imports have been removed, THE Pipeline SHALL delete the entire `pyqenc/legacy/` directory tree.
6. THE Pipeline SHALL ensure all existing tests pass after legacy removal, with no import errors referencing `pyqenc.legacy`.
