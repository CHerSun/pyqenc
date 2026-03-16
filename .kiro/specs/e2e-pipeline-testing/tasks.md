# Implementation Plan

- [x] 1. Run Scenario 1 — Full pipeline run from scratch

  - Execute: `.venv\Scripts\python.exe -m pyqenc auto "D:\_current\О чём говорят мужчины Blu-Ray (1080p) (1).mkv" --work-dir "D:\_current\pyqenc" -y --keep-all --log-level info`
  - Verify exit code 0
  - Verify crop parameters detected and logged (e.g. `Detected black borders: ...`)
  - Verify `D:\_current\pyqenc\chunks\` contains `chunk.NNNNNN-NNNNNN.mkv` files
  - Verify `D:\_current\pyqenc\encoded\` contains encoded attempt files under a strategy subfolder
  - Verify `D:\_current\pyqenc\audio\` contains `_day.aac` and `_night.aac` files
  - Verify `D:\_current\pyqenc\final\output_*.mkv` exists
  - Fix any bugs encountered before proceeding to next scenario
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [ ] 2. Fix `test_quality.py` — update tests to use `MetricType` enum and correct invariants
  - Replace all plain string metric arguments (`"ssim"`, `"vmaf"`, `"psnr"`) with `MetricType.SSIM`, `MetricType.VMAF`, `MetricType.PSNR` in `TestNormalizeMetricDeficit`
  - Update `test_normalize_unknown_metric` to expect `AssertionError` (from `assert_never`) instead of `ValueError`
  - Update `test_adjust_crf_large_deficit` and `test_adjust_crf_small_deficit` to assert invariants (CRF decreases, stays within bounds) rather than exact hardcoded values
  - _Requirements: 5.1, 6.1, 6.2_

- [ ] 3. Fix `test_config.py` — replace removed `h264-aq` profile references with `h264`
  - In `test_load_default_config`, replace `assert "h264-aq" in profiles` with `assert "h264" in profiles`
  - In `test_list_profiles_filtered`, replace `assert "h264-aq" in h264_profiles` with `assert "h264" in h264_profiles`
  - In `test_validate_strategy`, replace `"veryslow+h264-aq"` with `"veryslow+h264"`
  - _Requirements: 6.1, 6.2_

- [ ] 4. Fix `test_resumption.py` — replace `SourceVideoMetadata` with `VideoMetadata`
  - Remove `SourceVideoMetadata` from the import list
  - Add `VideoMetadata` to the import list
  - Replace all `SourceVideoMetadata(path=...)` instantiations with `VideoMetadata(path=Path(...))`
  - _Requirements: 6.1, 6.3_

- [ ] 5. Fix `_chunk_video_stateless` — handle zero detected scenes
  - In `pyqenc/phases/chunking.py`, after `detect()` returns an empty `scene_list`, log a warning and treat the entire video as one chunk (start=0, end=duration), consistent with `detect_scenes_to_state`
  - _Requirements: 6.3_

- [ ] 6. Fix `test_complete_pipeline.py` — correct `PipelineOrchestrator` constructor calls and add `ChunkingMode.REMUX`
  - Add `from pyqenc.models import ChunkingMode` import
  - Remove the `config_manager` third argument from every `PipelineOrchestrator(...)` call
  - Add `chunking_mode=ChunkingMode.REMUX` to every `PipelineConfig(...)` construction
  - _Requirements: 5.1, 5.2, 5.3_

- [ ] 7. Run full test suite and verify zero failures
  - Run `.venv\Scripts\python.exe -m pytest tests/ --tb=short -q`
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
  - Delete all files in `D:\_current\pyqenc\chunks\` (keep `progress.json` intact)
  - Also delete `D:\_current\pyqenc\encoded\` and `D:\_current\pyqenc\final\` so encoding and merge re-run
  - Re-run the same command as Scenario 1
  - Verify log contains "Scene boundaries already in state (N) -- skipping detection."
  - Verify chunks are re-split from persisted boundaries
  - Verify encoding and merge complete successfully
  - Verify exit code 0
  - Fix any scene-boundary resumption bugs before proceeding
  - _Requirements: 3.1, 3.2, 3.3_

- [ ] 10. Run Scenario 4 — Encoding partial restart (half chunks encoded)
  - Starting from a fully completed Scenario 1 state
  - Delete encoded files for approximately half the chunks from `D:\_current\pyqenc\encoded\<strategy>\`
  - Delete `D:\_current\pyqenc\final\` so merge re-runs
  - Re-run the same command as Scenario 1
  - Verify log reports count of reused chunks and count needing encoding
  - Verify only the deleted chunks are re-encoded (not the ones still present)
  - Verify merge completes and final output exists
  - Verify exit code 0
  - Fix any partial-encoding resumption bugs before proceeding
  - _Requirements: 4.1, 4.2, 4.3, 4.4_
