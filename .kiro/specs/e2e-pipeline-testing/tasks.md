# Implementation Plan

- [x] 1. Run Scenario 1 — Full pipeline run from scratch



















  - Execute: `uv run pyqenc auto "D:\_current\О чём говорят мужчины Blu-Ray (1080p) (1).mkv" --work-dir "D:\_current\pyqenc1" -y --keep-all --log-level info`
  - Verify exit code 0
  - Verify crop parameters detected and logged (e.g. `Detected black borders: ...`)
  - Verify `D:\_current\pyqenc1\chunks\` contains chunk `.mkv` files
  - Verify `D:\_current\pyqenc1\encoded\` contains encoded attempt files under a strategy subfolder
  - Verify `D:\_current\pyqenc1\audio\` contains audio files
  - Verify `D:\_current\pyqenc1\final\output_*.mkv` exists
  - Fix any bugs encountered before proceeding to next scenario
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [ ] 2. Fix `test_quality.py` — update tests to use `MetricType` enum and correct invariants
  - Update `test_normalize_unknown_metric` to expect `AssertionError` (from `assert_never`) instead of `ValueError`
  - Update `test_adjust_crf_large_deficit` and `test_adjust_crf_small_deficit` to assert invariants (CRF decreases, stays within bounds) rather than exact hardcoded values (`17.0`, `19.5`)
  - _Requirements: 6.1, 6.2_

- [ ] 3. Fix `test_config.py` — replace removed `h264-aq` profile references with `h264`
  - In `test_load_default_config`, remove `assert "h264-aq" in profiles` (profile no longer exists)
  - In `test_list_profiles_filtered`, replace `assert "h264-aq" in h264_profiles` with `assert "h264" in h264_profiles`
  - In `test_validate_strategy`, replace `config.validate_strategy("veryslow+h264-aq")` with `config.validate_strategy("veryslow+h264")`
  - _Requirements: 6.1, 6.2_

- [x] 4. Fix `test_resumption.py` — replace `SourceVideoMetadata` with `VideoMetadata`
  - Already done: file correctly imports and uses `VideoMetadata`
  - _Requirements: 6.1, 6.3_

- [ ] 5. Fix `test_extraction_chunking.py` — update `chunk_video` calls to pass a `tracker`
  - `tracker` is now a required parameter in `chunk_video` (stateless path was removed)
  - Create a `ProgressTracker` with a `tmp_path`-based work dir and initialize state before each `chunk_video` call
  - Update all calls in `TestExtractionChunkingIntegration` and `TestFFV1ChunkingIntegration`
  - _Requirements: 6.1, 6.3_

- [ ] 6. Fix `test_complete_pipeline.py` — correct `PipelineOrchestrator` constructor calls and add `ChunkingMode.REMUX`
  - Add `from pyqenc.models import ChunkingMode` import
  - Remove the `config_manager` third argument from every `PipelineOrchestrator(...)` call
  - Add `chunking_mode=ChunkingMode.REMUX` to every `PipelineConfig(...)` construction
  - _Requirements: 5.1, 5.2, 5.3_

- [ ] 7. Run full test suite and verify zero failures
  - Run `uv run pytest tests/ --tb=short -q`
  - All tests must pass (excluding any explicitly marked `@pytest.mark.slow` that require the real source video)
  - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [ ] 8. Run Scenario 2 — Full restart (all phases reused)
  - Without clearing the work directory, re-run the same command as Scenario 1
  - Verify each phase logs a reuse/already-exists message
  - Verify no new encoding or splitting is performed (log output shows no ffmpeg encode calls)
  - Verify exit code 0
  - Fix any resumption bugs before proceeding
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

- [ ] 9. Run Scenario 3 — Chunking partial restart (scenes in state, no chunk files)
  - Delete all files in `D:\_current\pyqenc1\chunks\` (keep `progress.json` intact)
  - Also delete `D:\_current\pyqenc1\encoded\` and `D:\_current\pyqenc1\final\` so encoding and merge re-run
  - Re-run the same command as Scenario 1
  - Verify log contains "Scene boundaries already in state (N) -- skipping detection."
  - Verify chunks are re-split from persisted boundaries
  - Verify encoding and merge complete successfully
  - Verify exit code 0
  - Fix any scene-boundary resumption bugs before proceeding
  - _Requirements: 3.1, 3.2, 3.3_

- [ ] 10. Run Scenario 4 — Encoding partial restart (half chunks encoded)
  - Starting from a fully completed Scenario 1 state
  - Delete encoded files for approximately half the chunks from `D:\_current\pyqenc1\encoded\<strategy>\`
  - Delete `D:\_current\pyqenc1\final\` so merge re-runs
  - Re-run the same command as Scenario 1
  - Verify log reports count of reused chunks and count needing encoding
  - Verify only the deleted chunks are re-encoded (not the ones still present)
  - Verify merge completes and final output exists
  - Verify exit code 0
  - Fix any partial-encoding resumption bugs before proceeding
  - _Requirements: 4.1, 4.2, 4.3, 4.4_
