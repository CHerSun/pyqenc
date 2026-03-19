# Implementation Plan

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-19
- Completed: 2026-03-19

- [x] 1. Update `pyqenc/constants.py`





  - Remove `PROGRESS_DURATION_UNIT` and `PROGRESS_CHUNK_UNIT` constants
  - Add `SKIPPED_SYMBOL = "⏭"` constant with docstring
  - _Requirements: 4.4_

- [x] 2. Implement `AdvanceState` enum and `ProgressBar` in `pyqenc/utils/alive.py`





  - Add `AdvanceState(Enum)` with `SUCCESS`, `SKIPPED`, `FAILED` values
  - Implement `ProgressBar(total, title, show_counters=True)` context manager with internal `_remaining`, `_cumulative`, `_success`, `_skipped`, `_failed` state
  - `advance(increment=1, state=AdvanceState.SUCCESS)` reduces `_remaining` on SKIPPED, skips fraction update on FAILED, advances fraction on SUCCESS
  - Bar text uses `SUCCESS_SYMBOL_MINOR`, `SKIPPED_SYMBOL`, `FAILURE_SYMBOL_MINOR` from constants; `show_counters=False` shows `{cumulative:.1f} / {original_total:.1f}`
  - Indeterminate mode when `total <= 0`: open `alive_bar` without `manual=True`; `advance` updates text only
  - Remove `duration_bar`, `update_bar` functions and `PROGRESS_DURATION_UNIT` import
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 4.1, 4.2_

- [x] 2.1 Write unit tests for `ProgressBar` in `tests/unit/`


  - SUCCESS increments advance fraction correctly
  - SKIPPED increments reduce `_remaining` and recompute fraction; fraction reaches 1.0 when `_remaining` hits 0
  - FAILED increments do not change fraction or `_remaining`
  - `total=0` opens indeterminate mode without crashing
  - Bar text format with and without failures; `show_counters=False` format
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 3.1, 3.2_

- [x] 3. Migrate `pyqenc/phases/chunking.py` to `ProgressBar`





  - Replace `duration_bar` import with `ProgressBar` import from `pyqenc.utils.alive`
  - Replace `duration_bar(total_seconds, title="Chunking")` call with `ProgressBar(total_seconds, title="Chunking")`
  - `advance` calls remain unchanged (all SUCCESS by default)
  - _Requirements: 4.3_

- [x] 4. Migrate `pyqenc/phases/encoding.py` to `ProgressBar`





  - Replace `duration_bar`, `update_bar` imports with `ProgressBar`, `AdvanceState`
  - Replace `duration_bar(total_seconds, title="Encoding")` with `ProgressBar(total_seconds, title="Encoding")`
  - Update `bar` parameter type annotation in `_encode_chunks_parallel` to `Callable[[int | float, AdvanceState], None] | None`
  - Replace `update_bar(bar, increment=0.0)` (reference missing) with `advance(chunk.end_timestamp - chunk.start_timestamp, AdvanceState.FAILED)`
  - Replace `update_bar(bar, increment=chunk.end_timestamp - chunk.start_timestamp)` with `advance(chunk.end_timestamp - chunk.start_timestamp)` for success; add `AdvanceState.SKIPPED` for `chunk_result.reused` cases
  - _Requirements: 4.3, 2.3, 2.4, 2.5_

- [x] 5. Migrate `pyqenc/phases/optimization.py` to `ProgressBar`





  - Replace `duration_bar`, `update_bar` imports with `ProgressBar`, `AdvanceState`
  - Replace `duration_bar(total_seconds, title="Optimization")` with `ProgressBar(total_seconds, title="Optimization")`
  - Update `bar` parameter type annotation in `_encode_strategy_chunks_parallel` to `Callable[[int | float, AdvanceState], None] | None`
  - Replace `update_bar(bar, increment=..., failed=...)` calls: COMPLETE recovery pairs → `AdvanceState.SKIPPED`; reference-missing errors → `AdvanceState.FAILED`; success → default SUCCESS
  - _Requirements: 4.3, 2.3, 2.4, 2.5_

- [x] 6. Migrate `pyqenc/utils/visualization.py` to `ProgressBar`





  - Replace `duration_bar` import with `ProgressBar` import
  - Replace `duration_bar(...)` call with `ProgressBar(..., show_counters=False)`
  - `advance` calls remain unchanged (all SUCCESS, streaming sub-second ticks)
  - _Requirements: 4.3, 2.6_

- [x] 7. Migrate `pyqenc/phases/audio.py` `SynchronousRunner` to `ProgressBar`





  - Replace `alive_bar` import usage in `SynchronousRunner.process` with `ProgressBar` from `pyqenc.utils.alive`
  - Add per-task output-exists check: if `task.output.exists()` → skip execution, call `advance(state=AdvanceState.SKIPPED)`
  - Replace parent-failure skip path with `advance(state=AdvanceState.FAILED)`
  - Replace success path with `advance()` (SUCCESS default)
  - Replace execution-failure path with `advance(state=AdvanceState.FAILED)`
  - Remove `_summary()` helper and manual `progress.text` assignments
  - _Requirements: 4.5, 5.1, 5.2, 5.5_

- [x] 8. Migrate `pyqenc/phases/audio.py` `AsyncRunner` to `ProgressBar`





  - Replace `alive_bar` usage in `AsyncRunner.process` with `ProgressBar`
  - Store `advance` callable as `self._advance` instead of `self._progress`
  - Remove `add_done_callback` that called `p()` — replace with explicit `advance` calls in `_run_task`
  - Add per-task output-exists check at start of `_run_task`: if `task.output.exists()` → call `self._advance(state=AdvanceState.SKIPPED)`, return `True`
  - Replace parent-failure path with `self._advance(state=AdvanceState.FAILED)`
  - Replace success path with `self._advance()` (SUCCESS default)
  - Replace execution-failure path with `self._advance(state=AdvanceState.FAILED)`
  - _Requirements: 4.6, 5.3, 5.4, 5.5_
