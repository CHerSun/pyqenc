# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-16
- Completed: 2026-03-16

## Introduction

This document specifies requirements for the next maturity pass on the pyqenc pipeline. The work covers seven interconnected areas:

1. **Typed data model consolidation** — eliminate the duplicate `ChunkInfo` dataclass and bare `Path` usage by promoting `ChunkMetadata` (renamed from `ChunkVideoMetadata`) to carry all chunk fields including timestamp offsets, introducing `AudioMetadata` for audio files, and defining `AttemptMetadata` for encoded attempt artifacts.
2. **CropParams flow** — wire crop detection through extraction, encoding, and metrics. `CropParams` is stored on `VideoMetadata.crop_params` and persisted in `PipelineState`. `None` is only valid before detection has run; after detection the value is always a `CropParams` instance (possibly all-zero if no borders found).
3. **Timestamp-based chunk naming** — fully commit to timestamp-based chunk names. Remove the dead frame-based `_chunk_name` call. Move `CHUNK_NAME_PATTERN` and glob masks to `constants.py`.
4. **Encoded attempt naming** — remove the attempt number from encoded chunk filenames; use only CRF. Encode to a temporary file (same stem with `.tmp` suffix) and rename only after encoding succeeds.
5. **Merge phase correctness** — when optimization was enabled, merge only the single optimal strategy result; when disabled, merge all strategy results. Verify frame count and temporal order before merging.
6. **Final metrics on merge** — after merging, run the same quality metrics evaluation on the final merged video and save results into the final result folder.
7. **Constants and display symbols** — define all magic numbers, special characters, separator lines, status symbols, glob patterns, and filename patterns as named constants in `pyqenc/constants.py`.

## Glossary

- **VideoMetadata**: Pydantic model in `pyqenc/models.py` holding `path: Path`, lazy-loaded probe fields, and `crop_params: CropParams | None` (populated after extraction; `None` only before detection has run).
- **ChunkMetadata**: Renamed from `ChunkVideoMetadata`. Subclass of `VideoMetadata` adding `chunk_id: str`, `start_timestamp: float`, and `end_timestamp: float`. Replaces the duplicate `ChunkInfo` dataclass entirely.
- **AudioMetadata**: New Pydantic model holding `path: Path`, `codec: str | None`, `channels: int | None`, `language: str | None`, and `duration_seconds: float | None`. Replaces bare `Path` for audio files.
- **AttemptMetadata**: New Pydantic model holding `path: Path`, `chunk_id: str`, `strategy: str`, `crf: float`, `resolution: str`, and `file_size_bytes: int`. Represents a completed encoded attempt artifact on disk.
- **CropParams**: Pydantic model holding `top`, `bottom`, `left`, `right` pixel offsets. After extraction, always a concrete instance — empty (all zeros) means no cropping needed, `None` means detection has not yet run.
- **ExtractionResult**: Dataclass returned by `extract_streams`; `video: VideoMetadata` (single stream, with `crop_params` populated), `audio_files: list[AudioMetadata]`.
- **ProgressTracker**: Persistent JSON-based state manager in `pyqenc/progress.py`.
- **PipelineState**: Top-level Pydantic model serialized by `ProgressTracker`; `source_video: VideoMetadata` carries `crop_params` so it is persisted alongside other source metadata.
- **QualityEvaluator**: Class in `pyqenc/utils/visualization.py` that runs ffmpeg metric filters and returns `QualityEvaluation`.
- **ChunkingMode**: Enum (`LOSSLESS` / `REMUX`) controlling how chunks are split.
- **CRF**: Constant Rate Factor — quality parameter passed to video encoders.
- **TEMP_SUFFIX**: Constant `.tmp` used to mark in-progress encoded files.
- **metrics sidecar**: A JSON file `<attempt_stem>.metrics.json` written atomically alongside each completed encoded attempt `.mkv` file. Contains `targets_met` (bool), `crf` (float), and `metrics` (dict). Its presence means quality evaluation has already been performed for that attempt; its absence means the attempt file is complete but metrics have not yet been measured.
- **optimal_strategy**: The single encoding strategy selected by the optimization phase and persisted in `PipelineState`.
- **LINE_WIDTH**: Named constant controlling the width of all horizontal separator lines.
- **THIN_LINE / THICK_LINE**: Named constants for horizontal separator strings, constructed once from `LINE_WIDTH`.
- **CHUNK_GLOB_PATTERN**: Named constant in `constants.py` for the glob mask used to discover chunk files.
- **CHUNK_NAME_PATTERN**: Named constant in `constants.py` for the regex used to validate and parse chunk file stems.
- **ffmpeg**: External video processing tool used for splitting, encoding, and probing.
- **mkvmerge**: External tool from mkvtoolnix used for MKV container muxing.

## Requirements

### Requirement 1

**User Story:** As a developer, I want all inter-phase data passed as typed objects rather than bare `Path` values, so that file references always carry their metadata and the type system enforces correct usage.

#### Acceptance Criteria

1. THE Pipeline SHALL rename `ChunkVideoMetadata` to `ChunkMetadata` and add `start_timestamp: float` and `end_timestamp: float` fields; THE Pipeline SHALL remove the duplicate `ChunkInfo` dataclass from `pyqenc/phases/chunking.py` and `pyqenc/phases/encoding.py`, replacing all usages with `ChunkMetadata`.
2. WHEN `chunk_video` or `_chunk_video_tracked` returns a `ChunkingResult`, THE Pipeline SHALL populate `ChunkingResult.chunks` as `list[ChunkMetadata]` with all fields populated from `ProgressTracker` state.
3. WHEN `encode_all_chunks` or `find_optimal_strategy` receives chunks, THE Pipeline SHALL accept `list[ChunkMetadata]` and use `chunk.path` for ffmpeg input and `chunk.chunk_id` for progress tracking.
4. THE Pipeline SHALL define `AudioMetadata` in `pyqenc/models.py` with `path: Path`, `codec: str | None`, `channels: int | None`, `language: str | None`, and `duration_seconds: float | None`; `ExtractionResult.audio_files` SHALL be `list[AudioMetadata]`.
5. THE Pipeline SHALL define `AttemptMetadata` in `pyqenc/models.py` with `path: Path`, `chunk_id: str`, `strategy: str`, `crf: float`, `resolution: str`, and `file_size_bytes: int`; `ChunkEncodingResult.encoded_file` SHALL reference an `AttemptMetadata` instance.
6. THE Pipeline SHALL update `ExtractionResult` to return a single `video: VideoMetadata` (not a list) since the pipeline targets one video stream; `crop_params` SHALL be accessed via `video.crop_params` rather than as a separate field.
7. THE Pipeline SHALL remove all bare `Path` parameters from public phase function signatures where a typed object already carries the path.

### Requirement 2

**User Story:** As a developer, I want crop parameters detected during extraction to flow correctly through every subsequent phase, so that encoded chunks and quality metrics always use the same crop geometry.

#### Acceptance Criteria

1. WHEN `extract_streams` completes crop detection or receives manual crop parameters, THE Pipeline SHALL store the result as `CropParams` on `source_video.crop_params`; after detection `crop_params` SHALL never be `None` — it SHALL be an empty `CropParams` (all zeros) if no borders were found.
2. WHEN `ProgressTracker` serializes `PipelineState`, THE Pipeline SHALL include `crop_params` in the `source_video` JSON so it survives restarts without re-running detection.
3. WHEN the encoding phase starts, THE Orchestrator SHALL read `CropParams` from `tracker._state.source_video.crop_params` and pass it to `encode_all_chunks` and `find_optimal_strategy`.
4. WHEN `ChunkEncoder._encode_with_ffmpeg` encodes a chunk attempt, THE Pipeline SHALL apply `CropParams.to_ffmpeg_filter()` as a `-vf` argument when `crop_params` is set and non-empty.
5. WHEN `QualityEvaluator.evaluate_chunk` computes metrics for a chunk attempt, THE Pipeline SHALL pass the same `CropParams` as `crop_reference` so the reference video is cropped to the same dimensions as the encoded output before comparison.
6. WHEN the chunking phase runs, THE Pipeline SHALL NOT apply any crop filter — chunks are split at original resolution regardless of `CropParams`.
7. WHEN the pipeline resumes from a previous run and `CropParams` has changed, THE Pipeline SHALL log a warning, invalidate any persisted scene boundaries in `PipelineState`, and treat all encoded chunk attempts as stale so they are re-encoded with the new crop geometry.
8. WHEN the chunking phase produces a chunk via ffmpeg, THE Pipeline SHALL call `chunk_meta.populate_from_ffmpeg_output(stderr_lines)` using the ffmpeg stderr from the split command, so that chunk duration and resolution are populated without an extra probe call.

### Requirement 3

**User Story:** As a developer, I want chunk files named after their timestamp range rather than frame range, so that naming is consistent for variable-framerate sources and stable across restarts.

#### Acceptance Criteria

1. THE Pipeline SHALL name each source chunk file using the timestamp pattern implemented in `_chunk_name_duration`, producing names using `TIME_SEPARATOR_SAFE` and `TIME_SEPARATOR_MS` constants from `pyqenc/constants.py`.
2. THE Pipeline SHALL assign `ChunkMetadata.chunk_id` from the scene boundary timestamps before splitting, so the id is known before the file is created; the chunk file is then written with that id as its stem.
3. WHEN the chunking phase resumes after a restart, THE Pipeline SHALL identify already-split chunks by matching existing file stems against `CHUNK_NAME_PATTERN` from `pyqenc/constants.py`.
4. THE Pipeline SHALL define `CHUNK_NAME_PATTERN` (regex) and `CHUNK_GLOB_PATTERN` (glob mask) as named constants in `pyqenc/constants.py`, grouped together under a clear comment block; no module SHALL define these inline.
5. THE Pipeline SHALL remove the dead `_chunk_name` (frame-based) call from `split_chunks_from_state` and `_chunk_video_stateless` — only `_chunk_name_duration` SHALL be used.

### Requirement 4

**User Story:** As a developer, I want encoded chunk attempt files named only by their CRF value (not attempt number), so that a file's presence on disk unambiguously indicates a complete, successful encode at that CRF.

#### Acceptance Criteria

1. THE Pipeline SHALL name each encoded attempt file using the pattern `<chunk_id>.<width>x<height>.crf<CRF>.mkv`, stored under the strategy subfolder — with no attempt number in the name.
2. WHEN `ChunkEncoder` begins encoding an attempt, THE Pipeline SHALL write to a temporary file whose stem is the intended final stem with `TEMP_SUFFIX` appended and rename it to the final name only after ffmpeg exits with code 0 and the output file is non-empty.
3. WHEN `ChunkEncoder` checks for an existing encoding at a given CRF, THE Pipeline SHALL scan the strategy output directory for a file matching `<chunk_id>.<resolution>.crf<CRF>.mkv` and treat its presence as proof of a complete encode — no progress tracker lookup is required for this check.
4. IF a file with the final CRF-based name already exists on disk, THEN THE Pipeline SHALL skip re-encoding that attempt, reuse the existing file, and log an info-level message identifying the reused file by name and CRF value so the user can see which attempts were recovered.
5. THE Pipeline SHALL clean up any leftover `.tmp` files in strategy output directories at the start of each encoding phase run, logging a warning for each one found.
6. THE Pipeline SHALL define the encoded attempt filename pattern and the attempt glob mask as named constants in `pyqenc/constants.py`.

### Requirement 10

**User Story:** As a developer, I want each completed encoded attempt to have a metrics sidecar file recording quality results and pass/fail status, so that pipeline restarts can recover metrics without re-running expensive quality evaluation.

#### Acceptance Criteria

1. WHEN `QualityEvaluator.evaluate_chunk` completes for an encoded attempt, THE Pipeline SHALL write a sidecar file `<attempt_stem>.metrics.json` atomically (write to `.tmp`, then rename) alongside the attempt `.mkv` file, containing at minimum: `targets_met` (bool), `crf` (float), and `metrics` (dict of measured metric values keyed by `<metric>_<statistic>`).
2. WHEN `ChunkEncoder` finds an existing attempt `.mkv` file on disk and a corresponding `.metrics.json` sidecar is present, THE Pipeline SHALL read the sidecar to obtain `targets_met` and `metrics` without re-running quality evaluation, and SHALL log an info-level message identifying the reused sidecar by filename.
3. WHEN `ChunkEncoder` finds an existing attempt `.mkv` file on disk but no corresponding `.metrics.json` sidecar is present, THE Pipeline SHALL re-run quality evaluation on the existing file and write the sidecar before continuing the CRF search — the attempt SHALL NOT be re-encoded.
4. THE Pipeline SHALL treat a sidecar file as valid only when it contains all metric keys required by the current quality targets; IF any required key is missing, THEN THE Pipeline SHALL re-run quality evaluation and overwrite the sidecar.
5. THE Pipeline SHALL use the sidecar `crf` field (not the filename) as the authoritative CRF value when reconstructing attempt history from disk, so that floating-point formatting differences between runs do not cause spurious cache misses.

### Requirement 5

**User Story:** As a developer, I want the merge phase to produce the correct set of output files depending on whether optimization was enabled, so that the pipeline always outputs exactly what was requested.

#### Acceptance Criteria

1. WHEN the optimization phase was enabled and completed successfully, THE Merge Phase SHALL merge only the single optimal strategy's encoded chunks into one output file.
2. WHEN the optimization phase was not enabled, THE Merge Phase SHALL merge all available strategies' encoded chunks, producing one output file per strategy.
3. BEFORE merging, THE Pipeline SHALL sort chunks by filename (which encodes the start timestamp lexicographically) to guarantee correct temporal order.
4. WHEN the total frame count of all chunks for a strategy does not equal the source frame count, THE Pipeline SHALL log a warning with the expected and actual counts and continue with the merge rather than aborting.
5. THE Pipeline SHALL log the chunk count and total frame count at info level before starting each strategy merge.

### Requirement 6

**User Story:** As a developer, I want the merge phase to measure final output quality and save metrics and plots alongside the output file, so that the quality of the final result is verifiable without re-running the pipeline.

#### Acceptance Criteria

1. WHEN a strategy's output file is successfully merged, THE Pipeline SHALL run `QualityEvaluator.evaluate_chunk` comparing the merged output against the original source video, using `source_video.crop_params` as `crop_reference`.
2. THE Pipeline SHALL save the metrics output, statistics summary, and unified plot into a subdirectory named `final_metrics_<safe_strategy>` inside the final output directory.
3. THE Pipeline SHALL log a summary of the final metrics at info level, including pass/fail status for each quality target.
4. IF quality metric measurement fails for a strategy, THEN THE Pipeline SHALL log a warning and continue — a metrics failure SHALL NOT cause the merge phase to fail.
5. THE Pipeline SHALL use the same `subsample_factor` from `PipelineConfig` for final metrics measurement as is used for chunk-level metrics.

### Requirement 7

**User Story:** As a developer, I want all magic numbers, special characters, display symbols, and artifact discovery patterns defined as named constants in `pyqenc/constants.py`, so that visual formatting and file discovery are consistent across the codebase and can be changed in one place.

#### Acceptance Criteria

1. THE Pipeline SHALL define every horizontal separator line as a named constant in `pyqenc/constants.py` using a single consistent character and a single `LINE_WIDTH` constant; no module SHALL construct separator strings inline using `"-" * N`, `"=" * N`, or any other inline repetition.
2. THE Pipeline SHALL define every status and result symbol (`✔`, `✅`, `✘`, `❌`, `⚠`) as a named constant in `pyqenc/constants.py`; no module SHALL embed these Unicode characters as string literals outside of `constants.py`.
3. THE Pipeline SHALL define every filesystem-safe special character used in filenames (`TIME_SEPARATOR_SAFE`, `TIME_SEPARATOR_MS`, `RANGE_SEPARATOR`, bracket characters) as a named constant in `pyqenc/constants.py`; no module SHALL embed these characters as string literals outside of `constants.py`.
4. THE Pipeline SHALL define all glob patterns and regex patterns used for artifact discovery (`CHUNK_GLOB_PATTERN`, `CHUNK_NAME_PATTERN`, `ENCODED_ATTEMPT_GLOB_PATTERN`, `ENCODED_ATTEMPT_NAME_PATTERN`) as named constants in `pyqenc/constants.py`, grouped together under a clear comment block.
5. THE Pipeline SHALL ensure `THIN_LINE = "─" * LINE_WIDTH` and `THICK_LINE = "═" * LINE_WIDTH` with no other width or character variant used anywhere in the codebase for horizontal separators.

### Requirement 8

**User Story:** As a developer, I want the work directory to be bound to a single source file, so that re-using a work directory for a different source file never silently produces corrupt or mixed results.

#### Acceptance Criteria

1. THE Pipeline SHALL treat the work directory as exclusively bound to the source file that was first processed in it; the binding is established by the `source_video` entry in `progress.json`.
2. THE Pipeline SHALL compare source file identity using at minimum: filename, file size in bytes, and video resolution; IF any of these differ, THEN the source file is considered changed.
3. WHEN the pipeline starts in dry-run mode and the loaded `progress.json` records a different source file than the one currently provided, THE Pipeline SHALL log a clear warning describing each differing field and take no further action — no state or artifacts are modified in dry-run mode.
4. WHEN the pipeline starts in execute mode (`-y`) and the loaded `progress.json` records a different source file than the one currently provided, THE Pipeline SHALL log a critical-level message describing the mismatch and stop execution without modifying any state or artifacts.
5. WHERE the `--force` flag is provided alongside execute mode, THE Pipeline SHALL log a warning describing each differing field, delete all intermediate artifacts in the work directory, reset all persisted state, and then continue with normal pipeline execution for the new source file.
6. THE Pipeline SHALL expose `--force` as a CLI flag distinct from `-y`; using `--force` without `-y` SHALL have no effect.

### Requirement 9

**User Story:** As a developer, I want quality metric runs to display a live progress bar so the user can see how long the measurement will take and that the process is not hung.

#### Acceptance Criteria

1. WHEN `QualityEvaluator.evaluate_chunk` launches ffmpeg metric subprocesses, THE Pipeline SHALL display an `alive_bar` progress bar whose total is estimated as `3 * duration_seconds` (seconds of wall-clock work) and whose title identifies the chunk or file being measured.
2. THE Pipeline SHALL read ffmpeg `-progress pipe:1` output asynchronously and advance the progress bar based on the `out_time_ms` field reported by ffmpeg, so that progress reflects actual encoding position rather than elapsed time.
3. THE Pipeline SHALL flush ffmpeg stderr asynchronously in a separate coroutine to prevent the subprocess from blocking on a full stderr pipe.
4. WHEN the ffmpeg process exits, THE Pipeline SHALL mark the progress bar as complete regardless of whether the estimated total was reached.
5. IF `duration_seconds` is not available, THE Pipeline SHALL display an indeterminate spinner rather than a percentage bar.
