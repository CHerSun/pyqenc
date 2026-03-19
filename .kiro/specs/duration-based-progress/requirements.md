# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-17
- Completed: 2026-03-17

## Introduction

The pyqenc pipeline currently displays progress bars using chunk counts (items) as the unit of measure. This is misleading because chunks vary significantly in duration — a single 5-minute chunk and a 10-second chunk both advance the bar by one unit, making the ETA and throughput readout meaningless. This feature replaces all chunk-count-based `alive_bar` progress displays with duration-based progress (float seconds of video processed), giving the user an accurate, time-meaningful view of how much of the video has been processed.

The affected phases are:
- **Chunking** — `split_chunks_from_state` in `phases/chunking.py`
- **Optimization** — `find_optimal_strategy` in `phases/optimization.py`
- **Encoding** — `encode_all_chunks` / `_encode_chunks_parallel` in `phases/encoding.py`

The scene-detection spinner in `chunking.py` is indeterminate and is out of scope. The per-chunk metrics bar in `visualization.py` is already duration-based but uses an inline `manual=True` pattern with a local `_advance` closure; this feature will unify that pattern with the shared helper so all duration-based bars are consistent.

## Glossary

- **alive_bar**: The progress bar widget from the `alive-progress` library used throughout the pipeline.
- **Chunk**: A contiguous video segment produced by the chunking phase, identified by a timestamp range. Each chunk has a `start_timestamp` and `end_timestamp` (float seconds).
- **Chunk duration**: `end_timestamp - start_timestamp` for a given `ChunkMetadata` instance (float seconds).
- **Duration-based progress**: A progress display where the total and increments are expressed in seconds of video content rather than in item counts.
- **PROGRESS_CHUNK_UNIT**: The constant `" chunks"` currently used as the `unit` argument to `alive_bar`. It will be replaced by `" s"` (seconds).
- **Total duration**: The sum of all chunk durations for the current phase scope (e.g. all chunks × strategies for encoding).
- **Manual mode**: The `alive_bar` parameter `manual=True`, which accepts a fraction in `[0.0, 1.0]` instead of an integer increment. Used when the bar total is expressed in seconds and fractional advances are needed.
- **`update_bar`**: The helper in `pyqenc/utils/alive.py` that wraps `alive_bar` calls and tracks failed-chunk warnings.
- **`PROGRESS_DURATION_UNIT`**: A new constant (replacing `PROGRESS_CHUNK_UNIT` in progress contexts) with value `" s"` to label the seconds unit on progress bars.
- **`duration_bar` context manager**: A shared helper (to be added to `pyqenc/utils/alive.py`) that opens an `alive_bar` in `manual=True` mode with a seconds-based total, and exposes an `advance(seconds: float)` callable so callers never touch the raw bar fraction arithmetic.

## Requirements

### Requirement 1

**User Story:** As a user running the chunking phase, I want the progress bar to show seconds of video split rather than number of chunks, so that the ETA and throughput reflect actual video time processed.

#### Acceptance Criteria

1. WHEN the chunking phase splits video segments, THE System SHALL display an `alive_bar` whose total equals `video_meta.duration_seconds` (the full source video duration, already probed before the loop).
2. WHEN a chunk is successfully split or skipped (already done), THE System SHALL advance the chunking progress bar by that chunk's duration in seconds.
3. THE System SHALL label the chunking progress bar unit as `" s"` (seconds).

---

### Requirement 2

**User Story:** As a user running the optimization phase, I want the progress bar to show seconds of video tested rather than number of chunks, so that I can gauge how much content has been evaluated across all strategies.

#### Acceptance Criteria

1. WHEN the optimization phase begins, THE System SHALL compute the total progress bar scope as `(sum of test-chunk durations) × (number of strategies)` in seconds.
2. WHEN a test chunk finishes encoding for any strategy, THE System SHALL advance the optimization progress bar by that chunk's duration in seconds.
3. THE System SHALL label the optimization progress bar unit as `" s"` (seconds).

---

### Requirement 3

**User Story:** As a user running the encoding phase, I want the progress bar to show seconds of video encoded rather than number of chunks, so that the displayed throughput and ETA are meaningful.

#### Acceptance Criteria

1. WHEN the encoding phase begins, THE System SHALL compute the total progress bar scope as `(sum of all chunk durations) × (number of strategies)` in seconds.
2. WHEN a chunk finishes encoding (success or failure), THE System SHALL advance the encoding progress bar by that chunk's duration in seconds.
3. THE System SHALL label the encoding progress bar unit as `" s"` (seconds).

---

### Requirement 4

**User Story:** As a developer maintaining the codebase, I want the `update_bar` helper and related constants to support duration-based increments, so that all progress bar call sites remain consistent and DRY.

#### Acceptance Criteria

1. THE System SHALL update the `update_bar` helper in `pyqenc/utils/alive.py` to accept a `float` increment (seconds) instead of an `int` (chunk count).
2. THE System SHALL add a `PROGRESS_DURATION_UNIT` constant with value `" s"` to `pyqenc/constants.py`.
3. THE System SHALL remove or deprecate the `PROGRESS_CHUNK_UNIT` constant from all progress-bar call sites (the constant itself may remain for backward compatibility but MUST NOT be used in any `alive_bar` `unit` argument).
4. WHEN `update_bar` is called with `increment=0.0`, THE System SHALL not advance the bar but MAY still update the bar text.

---

### Requirement 5

**User Story:** As a developer, I want all duration-based `alive_bar` usages — including the existing one in `visualization.py` — to share a single helper, so that the manual-mode fraction arithmetic is not duplicated across modules.

#### Acceptance Criteria

1. THE System SHALL provide a `duration_bar` context manager in `pyqenc/utils/alive.py` that accepts `total_seconds: float`, `title: str`, and optional `unit: str` (defaulting to `PROGRESS_DURATION_UNIT`), opens an `alive_bar` in `manual=True` mode, and yields an `advance(seconds: float) -> None` callable.
2. WHEN `advance` is called, THE System SHALL update the bar fraction as `min(1.0, cumulative_seconds / total_seconds)`.
3. WHEN the `duration_bar` context exits normally, THE System SHALL set the bar to `1.0` (complete).
4. THE System SHALL refactor `visualization.py` `evaluate_chunk` to use `duration_bar` instead of its current inline `manual=True` pattern.
