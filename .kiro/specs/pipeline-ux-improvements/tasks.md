# Implementation Plan

- [x] 1. Add `log_format.py` utility module





  - Create `pyqenc/utils/log_format.py` with `fmt_attempt`, `fmt_chunk_final`, `fmt_strategy_result_block`, and `fmt_optimization_summary` helpers
  - `fmt_attempt(strategy, chunk_id, attempt, msg)` → `"{strategy}/{chunk_id} attempt {attempt}: {msg}"`
  - `fmt_chunk_final(strategy, chunk_id, crf, attempts)` → `"✔ {strategy}/{chunk_id}: final CRF {crf:.2f} after {attempts} attempts"`
  - `fmt_strategy_result_block(strategy, avg_crf, total_size_mb, num_chunks, passed, error)` → list of lines with `─` delimiters
  - `fmt_optimization_summary(optimal, results)` → list of lines with `═` delimiters and comparison table
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4_

- [x] 2. Thread crop params through `ChunkEncoder` and encoding phase





- [x] 2.1 Add `crop_params: CropParams | None = None` parameter to `ChunkEncoder.__init__`; store as `self._crop_params`


  - Inject `-vf {crop_params.to_ffmpeg_filter()}` into the ffmpeg command inside `_encode_with_ffmpeg` when crop is set and non-empty
  - _Requirements: 1.2, 1.3_
- [x] 2.2 Add `crop_params: CropParams | None = None` parameter to `encode_all_chunks`; pass it to `ChunkEncoder` constructor



  - _Requirements: 1.3_
- [x] 2.3 Write unit tests for crop injection in `ChunkEncoder`


  - Mock `_encode_with_ffmpeg`; assert `-vf` arg present when crop set, absent when `None`
  - Verify crop is applied consistently for both encoding and optimization phase calls
  - _Requirements: 1.2, 1.3, 1.5_

- [x] 3. Thread crop params through optimization phase and add parallel workers





- [x] 3.1 Add `crop_params: CropParams | None = None` and `max_parallel: int = 2` parameters to `find_optimal_strategy`; pass `crop_params` to `ChunkEncoder`


  - _Requirements: 1.2, 5.1, 5.2_
- [x] 3.2 Implement `_encode_strategy_chunks_parallel` async helper inside `optimization.py`

  - Mirrors `_encode_chunks_parallel` from `encoding.py`; encodes all test chunks for one strategy in parallel using `asyncio` + thread executor
  - Accepts `max_parallel` semaphore; increments `alive_bar` handle on each chunk completion
  - _Requirements: 5.1, 5.3, 5.4_
- [x] 3.3 Replace the sequential per-chunk loop in `find_optimal_strategy` with calls to `_encode_strategy_chunks_parallel`

  - Strategy loop remains sequential; parallelism is within each strategy's test chunks
  - _Requirements: 5.4_

- [x] 4. Add `_resolve_crop_params` to orchestrator and pass crop + `max_parallel` downstream





  - Add `_resolve_crop_params(self) -> CropParams | None` to `PipelineOrchestrator`; reads `PhaseMetadata.crop_params` from extraction phase state
  - Call it in `_execute_optimization` and `_execute_encoding`; pass result to `find_optimal_strategy` and `encode_all_chunks`
  - Pass `self.config.max_parallel` to `find_optimal_strategy`
  - Log INFO "Crop params loaded: {crop}" or INFO "No crop params; encoding without crop"
  - _Requirements: 1.1, 1.4, 5.2, 5.3_

- [x] 5. Replace chunk attempt logging in `encoding.py` with uniform format





  - Import and use `fmt_attempt` and `fmt_chunk_final` from `log_format.py` for all per-attempt log lines inside `encode_chunk`
  - Add metrics summary line using `fmt_attempt` after quality evaluation
  - Replace final-result log lines with `fmt_chunk_final` at INFO level
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 6. Add distinctive optimization summaries in `optimization.py`





  - Import `fmt_strategy_result_block` and `fmt_optimization_summary` from `log_format.py`
  - Emit `fmt_strategy_result_block` lines after each strategy's test chunks complete
  - Emit `fmt_optimization_summary` lines after the optimal strategy is selected
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 7. Add `alive_progress` bars to chunking phase





  - In `detect_scenes_to_state`: wrap `detect(…)` call with an `alive_bar(unknown="waves", title="Scene detection")` spinner
  - In `split_chunks_from_state`: add `alive_bar(len(boundaries), title="Chunking", unit="chunk")` finite bar; increment on each successfully split chunk with `text=chunk_stem`
  - Apply same pattern to `_chunk_video_stateless`
  - _Requirements: 4.1, 4.2, 4.3_

- [x] 8. Add `alive_progress` bars to optimization and encoding phases





  - In `find_optimal_strategy`: add `alive_bar(len(strategies) * len(test_chunks), title="Optimization", unit="chunk")` bar; pass handle to `_encode_strategy_chunks_parallel`; increment on each chunk completion (success or failure)
  - In `encode_all_chunks` / `_encode_chunks_parallel`: add `alive_bar(len(chunks) * len(strategies), title="Encoding", unit="chunk")` bar; increment on each chunk completion; show current strategy/chunk as bar text
  - _Requirements: 4.4, 4.5, 4.6, 4.7_
