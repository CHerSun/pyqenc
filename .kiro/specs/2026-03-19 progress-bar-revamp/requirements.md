# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-19
- Completed: 2026-03-19

## Introduction

The pipeline currently uses `duration_bar` (in `pyqenc/utils/alive.py`) for duration-based progress in the chunking, encoding, optimization, and metrics phases, and a separate `alive_bar` in manual count mode for the audio phase (`SynchronousRunner` and `AsyncRunner` in `pyqenc/phases/audio.py`). Two problems have been identified:

1. **Reused artifacts are not reflected in progress** — when a chunk or audio task is skipped because its output already exists, the bar either does not advance (making ETA unreliable) or advances in a way that distorts the ETA.
2. **The bar text does not distinguish reused items from genuinely processed ones** — the end-user cannot tell at a glance how many items were skipped vs. succeeded vs. failed.

The goal is to unify all progress bars under a single `ProgressBar` helper that always operates in `alive_bar` manual mode (0.0–1.0 fraction, no unit label). The fraction is computed from whatever total the caller supplies — duration in seconds for duration-based phases, item count for count-based phases. The helper tracks success / skipped / failed counters, reduces the effective total when items are reused (so ETA stays accurate), and displays a clear `✔ {success}  ⏭ {skipped}` summary (with `✘ {failed}` appended when failures occur).

## Glossary

- **System**: The `pyqenc` encoding pipeline.
- **`duration_bar`**: The existing context manager in `pyqenc/utils/alive.py` that will be removed and replaced by `ProgressBar`.
- **`ProgressBar`**: The new unified progress-bar context manager that always uses `alive_bar` in manual mode (0.0–1.0 fraction, no unit label). Fraction arithmetic is driven by the caller's `total` — seconds for duration-based phases, item count for count-based phases.
- **Manual mode**: `alive_bar(manual=True)` — the bar fraction is set explicitly on a 0.0–1.0 scale. In manual mode `alive_bar` always renders as a percentage; no `unit` label is shown.
- **Fraction arithmetic**: The computation of the 0.0–1.0 bar value from the caller's domain units (seconds or item count). `ProgressBar` owns this arithmetic internally.
- **Reused item**: A chunk, audio task, or other pipeline unit whose output already exists and is skipped without re-processing.
- **Failed item**: A pipeline unit whose processing raised an exception or returned a non-success result.
- **ETA**: Estimated time to completion as displayed by `alive_bar`.
- **`advance` callable**: The callable yielded by `ProgressBar` that callers invoke to record progress. Accepts `increment: int | float` and `state: AdvanceState` (default `AdvanceState.SUCCESS`).
- **`AdvanceState`**: An enum with three mutually exclusive values — `SUCCESS`, `SKIPPED`, `FAILED` — that describes the outcome of the completed item.
- **`SynchronousRunner`**: The synchronous audio task executor in `pyqenc/phases/audio.py`.
- **`AsyncRunner`**: The async audio task executor in `pyqenc/phases/audio.py`.

## Requirements

### Requirement 1

**User Story:** As a pipeline operator, I want the progress bar to advance correctly when items are reused, so that the ETA remains accurate throughout the run.

#### Acceptance Criteria

1. WHEN an item is reported as reused via the `advance` callable, THE System SHALL reduce the saved total by the item's weight (the `increment` value passed to `advance`) so that the bar fraction reflects only the remaining real work.
2. WHEN the saved total is reduced to zero or below after a reuse event, THE System SHALL set the bar fraction to 1.0 (complete).
3. WHEN `ProgressBar` is initialised with `total=0`, THE System SHALL open `alive_bar` in indeterminate (spinner) mode; `advance` calls SHALL still update the bar text (success / skipped / failed counters) but SHALL NOT attempt to set the bar fraction.
4. WHEN the `ProgressBar` context exits, THE System SHALL NOT set the bar fraction to any fixed value; the bar SHALL display whatever fraction was last set by `advance` calls, allowing `alive_bar` to render the true final state.

---

### Requirement 2

**User Story:** As a pipeline operator, I want the bar text to show a clear breakdown of succeeded, skipped, and failed counts, so that I can immediately understand the pipeline state at a glance.

#### Acceptance Criteria

1. WHILE the bar is active and no failures have been recorded, THE System SHALL display bar text in the form `✔ {success}  ⏭ {skipped}`.
2. WHEN at least one failure has been recorded, THE System SHALL display bar text in the form `✔ {success}  ⏭ {skipped}  ✘ {failed}`.
3. WHEN `advance` is called with `state=AdvanceState.SKIPPED`, THE System SHALL increment the skipped counter and update the bar text.
4. WHEN `advance` is called with `state=AdvanceState.FAILED`, THE System SHALL increment the failed counter and update the bar text without advancing the bar fraction.
5. WHEN `advance` is called with `state=AdvanceState.SUCCESS` (the default), THE System SHALL increment the success counter and advance the bar fraction by the given increment.
6. WHILE `show_counters=False`, THE System SHALL display bar text in the form `{cumulative:.1f} / {original_total:.1f}` instead of the success/skipped/failed counters, where `original_total` is the `total` value passed at construction.

---

### Requirement 3

**User Story:** As a pipeline operator, I want failed items to not distort the ETA, so that the remaining-time estimate stays meaningful even when some chunks fail.

#### Acceptance Criteria

1. WHEN an item is reported as failed, THE System SHALL NOT advance the bar fraction.
2. WHEN an item is reported as failed, THE System SHALL NOT reduce the saved total.
3. WHEN an item is reported as failed, THE System SHALL update the bar text to include the failure count.

---

### Requirement 4

**User Story:** As a developer, I want a single unified `ProgressBar` helper that covers both duration-based and count-based use cases, so that all phases use a consistent API.

#### Acceptance Criteria

1. THE System SHALL provide a `ProgressBar` context manager in `pyqenc/utils/alive.py` that accepts `total: int | float`, `title: str`, and optional `show_counters: bool` (default `True`), opens `alive_bar` in manual mode (no `unit` — manual mode renders as percentage), and yields an `advance(increment: int | float = 1, state: AdvanceState = AdvanceState.SUCCESS) -> None` callable.
2. THE System SHALL define `AdvanceState` as an enum in `pyqenc/utils/alive.py` with values `SUCCESS`, `SKIPPED`, and `FAILED`.
3. THE System SHALL replace all `duration_bar` usages in `chunking.py`, `encoding.py`, `optimization.py`, and `visualization.py` with `ProgressBar`, and SHALL remove the `duration_bar` function from `pyqenc/utils/alive.py`.
4. THE System SHALL remove the `PROGRESS_DURATION_UNIT` and `PROGRESS_CHUNK_UNIT` constants from `pyqenc/constants.py` as they are no longer needed once `duration_bar` and its `unit` parameter are removed, and SHALL add a `SKIPPED_SYMBOL` constant (value `"⏭"`) to `pyqenc/constants.py` for use in bar text.
5. THE System SHALL update `SynchronousRunner.process` in `pyqenc/phases/audio.py` to use `ProgressBar` in place of the current `alive_bar(len(self.tasks))` call.
6. THE System SHALL update `AsyncRunner.process` in `pyqenc/phases/audio.py` to use `ProgressBar` in place of the current `alive_bar(len(self.tasks))` call.

---

### Requirement 5

**User Story:** As a pipeline operator, I want the audio phase progress bar to correctly reflect reused, failed, and skipped tasks, so that the audio bar is consistent with the rest of the pipeline.

#### Acceptance Criteria

1. WHEN `SynchronousRunner` skips a task because its parent failed, THE System SHALL call `advance` with `state=AdvanceState.FAILED`.
2. WHEN `SynchronousRunner` encounters a task execution failure, THE System SHALL call `advance` with `state=AdvanceState.FAILED`.
3. WHEN `AsyncRunner` skips a task because its parent failed, THE System SHALL call `advance` with `state=AdvanceState.FAILED`.
4. WHEN `AsyncRunner` encounters a task execution failure, THE System SHALL call `advance` with `state=AdvanceState.FAILED`.
5. WHEN a `SynchronousRunner` or `AsyncRunner` task finds that its output file already exists on disk before executing, THE System SHALL skip execution and call `advance` with `state=AdvanceState.SKIPPED`.
6. WHEN `process_audio_streams` detects that existing AAC files are reused (whole-phase reuse), THE System SHALL log the reuse at `info` level; no progress bar is shown for a full-phase reuse (existing behaviour preserved).
