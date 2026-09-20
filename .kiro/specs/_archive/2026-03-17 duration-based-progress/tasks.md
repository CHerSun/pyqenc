# Implementation Plan

- [x] 1. Add `PROGRESS_DURATION_UNIT` constant and `duration_bar` helper





  - Add `PROGRESS_DURATION_UNIT = " s"` to `pyqenc/constants.py`
  - Update `update_bar` in `pyqenc/utils/alive.py` to accept `increment: float` instead of `int`
  - Add `duration_bar(total_seconds, title, unit)` context manager to `pyqenc/utils/alive.py` that opens `alive_bar` in `manual=True` mode and yields an `advance(seconds: float) -> None` callable
  - _Requirements: 4.1, 4.2, 4.4, 5.1, 5.2, 5.3_

- [x] 2. Switch chunking phase to duration-based progress





  - In `split_chunks_from_state` (`phases/chunking.py`): replace `alive_bar(len(boundaries), unit=PROGRESS_CHUNK_UNIT)` with `duration_bar(video_meta.duration_seconds or 0.0, title="Chunking")`
  - Replace `bar()` calls with `advance(end_ts - start_ts)`
  - Remove `PROGRESS_CHUNK_UNIT` import from `chunking.py`
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 3. Switch optimization phase to duration-based progress





  - In `find_optimal_strategy` (`phases/optimization.py`): compute `total_seconds = sum(c.end_timestamp - c.start_timestamp for c in test_chunks) * len(strategies)` and replace `alive_bar(total_chunks, unit=PROGRESS_CHUNK_UNIT)` with `duration_bar(total_seconds, title="Optimization")`
  - Update `_encode_strategy_chunks_parallel` signature: change `bar` parameter type annotation from raw `alive_bar` handle to `Callable[[float], None] | None`
  - Replace `bar()` calls inside `_encode_strategy_chunks_parallel` with `update_bar(bar, increment=chunk.end_timestamp - chunk.start_timestamp)`
  - Remove `PROGRESS_CHUNK_UNIT` import from `optimization.py`
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 4. Switch encoding phase to duration-based progress





  - In `encode_all_chunks` (`phases/encoding.py`): compute `total_seconds = sum(c.end_timestamp - c.start_timestamp for c in chunks) * len(strategies)` and replace `alive_bar(total_items, unit=PROGRESS_CHUNK_UNIT)` with `duration_bar(total_seconds, title="Encoding")`
  - Update `_encode_chunks_parallel` signature: change `bar` parameter type annotation to `Callable[[float], None] | None`
  - Replace `update_bar(bar, increment=int(chunk_result.success))` with `update_bar(bar, increment=chunk.end_timestamp - chunk.start_timestamp)`
  - Remove `PROGRESS_CHUNK_UNIT` import from `encoding.py`
  - _Requirements: 3.1, 3.2, 3.3_

- [x] 5. Refactor `visualization.py` metrics bar to use `duration_bar`





  - In `evaluate_chunk` (`utils/visualization.py`): replace the inline `manual=True` block and `_advance` closure with `duration_bar(_NUM_METRIC_PASSES * (duration_seconds or 0.0), title=f"Metrics: {bar_title}")`
  - Pass the yielded `advance` callable directly as `bar_advance` to `_generate_metrics`
  - _Requirements: 5.4_

- [x] 6. Mark spec completed





  - Update `Completed` date in `requirements.md` and `design.md`
  - _Requirements: all_
