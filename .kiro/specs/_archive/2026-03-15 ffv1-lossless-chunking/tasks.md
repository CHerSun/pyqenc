# Implementation Plan

- [x] 1. Add `ChunkingMode` enum and update `PipelineConfig`

  - Add `ChunkingMode(Enum)` with `LOSSLESS = "lossless"` and `REMUX = "remux"` to `pyqenc/models.py`
  - Add `chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS` field to `PipelineConfig`
  - _Requirements: 1.1, 1.2_

- [x] 2. Add `pix_fmt` lazy property to `VideoMetadata`

  - Add `_pix_fmt: str | None = PrivateAttr(default=None)` backing field to `VideoMetadata` in `pyqenc/models.py`
  - Add `pix_fmt` property following the same pattern as `fps` and `resolution` — triggers `_probe_metadata()` on first access
  - In `populate_from_ffprobe`, read `stream.get("pix_fmt")` and populate `_pix_fmt` if currently `None`
  - In `populate_from_ffmpeg_output`, parse pix_fmt from the `Stream #0:0: Video:` line if currently `None`
  - Include `_pix_fmt` in `model_dump_full()` and restore it in `model_validate_full()`
  - Extend the `-show_entries` in `_probe_metadata()` to include `pix_fmt` — no extra subprocess needed
  - _Requirements: 1.5_

- [x] 3. Add `FFV1_VIDEO_ARGS` constant and update `split_chunks_from_state`

  - Define module-level `FFV1_VIDEO_ARGS: list[str]` constant with the ffv1 flags
  - Add `chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS` parameter to `split_chunks_from_state`
  - Before the split loop, access `video_meta.pix_fmt` (lazy, cached, no extra subprocess) and fall back to `"yuv420p"` with a warning if `None`
  - In the command-building block, select `[*FFV1_VIDEO_ARGS, "-pix_fmt", pix_fmt]` vs `["-c", "copy"]` based on `chunking_mode`
  - Remove `crop_params` argument and all `crop_filter_args` logic from `split_chunks_from_state` and `chunk_video` — crop is not applied at chunking time
  - Log chunking mode at info level before the loop
  - Remove the existing TODO comment about I-frame snapping being a known limitation
  - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 3.2_

- [x] 4. Thread `chunking_mode` through `chunk_video` and its internal helpers

  - Add `chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS` parameter to `chunk_video`
  - Pass it through to `split_chunks_from_state` in `_chunk_video_tracked`
  - Pass it through in `_chunk_video_stateless` (update that path's command building too)
  - _Requirements: 1.3, 1.4_

- [x] 5. Update orchestrator to pass `chunking_mode` and remove crop from chunking

  - In `_execute_chunking` in `pyqenc/orchestrator.py`, pass `chunking_mode=self.config.chunking_mode` to `chunk_video`
  - Remove the `crop_params` argument from the `chunk_video` call — crop is no longer a chunking concern
  - Ensure `crop_params` is still passed correctly to the encoding phase where it belongs
  - _Requirements: 1.2, 1.8_

- [x] 6. Update disk space estimation for chunking mode

  - Add `chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS` parameter to `estimate_required_space` in `pyqenc/utils/disk_space.py`
  - When `LOSSLESS`, use `chunks_multiplier = 5.0`; when `REMUX`, use `chunks_multiplier = 1.0`
  - Propagate the parameter through `check_disk_space` and `log_disk_space_info`
  - In the orchestrator pre-flight check, pass `chunking_mode=self.config.chunking_mode` to `log_disk_space_info`
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

- [x] 7. Add `--remux-chunking` CLI flag

  - Add `--remux-chunking` boolean flag to the `auto` subcommand in `pyqenc/cli.py`
  - Add `--remux-chunking` boolean flag to the `chunk` subcommand
  - When flag is present, set `chunking_mode=ChunkingMode.REMUX` in `PipelineConfig` / pass `ChunkingMode.REMUX` to `chunk_video`
  - Write help text explaining lossless FFV1 is the default and this flag trades frame-perfect splits for speed and smaller chunks
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [x] 8. Update existing tests

  - In all existing chunking tests that exercise the stream-copy path, pass `chunking_mode=ChunkingMode.REMUX` so they continue to pass without requiring an FFV1 encode
  - Update any `PipelineConfig` fixtures to explicitly set `chunking_mode=ChunkingMode.REMUX` to preserve existing test behavior
  - _Requirements: 4.1_

- [x] 9. Add unit test for FFV1 command construction

  - Mock `subprocess.run` and call `split_chunks_from_state` (or `chunk_video`) with `chunking_mode=ChunkingMode.LOSSLESS`
  - Assert the captured ffmpeg command contains `-c:v ffv1`, `-g 1`, `-level 3`, `-pix_fmt <expected>` and does NOT contain `-c copy`
  - Assert remux mode still produces `-c copy` and no ffv1 flags
  - _Requirements: 4.2_

- [x] 10. Add integration test for FFV1 chunking

  - Mark test with `pytest.mark.slow` and `pytest.mark.requires_ffmpeg`
  - Use the short test clip from existing integration test fixtures
  - Call `chunk_video(..., chunking_mode=ChunkingMode.LOSSLESS)` and verify: chunk files exist, are non-empty, and `ffprobe` reports the expected frame counts
  - _Requirements: 4.3_

- [x] 11. Update documentation

  - Update `README.md` (or relevant docs file) to describe the two chunking modes and the `--remux-chunking` flag
  - Note the frame-precision trade-off clearly
  - _Requirements: 4.4_
