# Implementation Plan: PTS Preservation

<!-- markdownlint-disable MD024 -->

- Created: 2026-04-29

## Overview

Implement PTS preservation across the extraction and merge phases. The work
falls into seven groups: constants/models, timestamp extraction, video/audio
command fixes, replacing mkvextract with ffmpeg for other streams, wiring
mkvmerge into the merge phase, tests, and a cross-spec review pass.

No new external tools or Python packages are introduced — only `ffmpeg`,
`ffprobe`, and `mkvmerge` (all already approved dependencies) are used.

## Tasks

- [x] 1. Add constants and data models
  - Add `TIMESTAMPS_FILENAME = "timestamps.txt"` constant to `pyqenc/constants.py`
  - Add `TimestampArtifact` dataclass to `pyqenc/phases/extraction.py` (subclass of `Artifact`; states `COMPLETE`/`ABSENT` only)
  - Add `timestamps_path: Path | None = None` field to `ExtractionPhaseResult` dataclass
  - _Requirements: 3.1, 3.5, 5.2_

- [x] 2. Implement timestamp extraction
  - [x] 2.1 Implement `_extract_timestamps(source, video_track_id, output)` in `extraction.py`
    - Run `ffprobe -v error -select_streams <video_track_id> -show_packets -show_entries packet=pts_time -of csv=print_section=0`
    - Convert each output line: `int(float(line.strip()) * 1000)` → integer milliseconds
    - Write `# timestamp format v2` header followed by one value per line
    - Use `.tmp`-then-rename protocol for atomicity
    - On ffprobe failure, empty output, or any unparseable line → log `critical`, raise
    - _Requirements: 3.1, 3.2, 3.3_

  - [x] 2.2 Integrate `_extract_timestamps()` into `_execute_extraction()`
    - Call `_extract_timestamps()` unconditionally (not gated by include/exclude filters)
    - Use `video_tracks[0].track_id` as `video_track_id`
    - Output path: `extracted_dir / TIMESTAMPS_FILENAME`
    - _Requirements: 3.4_

  - [x] 2.3 Integrate `TimestampArtifact` into `_recover()`
    - Always check for `extracted/timestamps.txt` regardless of include/exclude filters
    - Classify as `COMPLETE` when file exists, `ABSENT` otherwise
    - Include in `force_wipe` deletion
    - Include in the returned artifacts list
    - _Requirements: 3.5, 3.6, 3.7, 3.8_

  - [x] 2.4 Set `timestamps_path` on `ExtractionPhaseResult`
    - Set to `TimestampArtifact.path` when state is `COMPLETE`, `None` when `ABSENT`
    - Apply in both `_execute_extraction()` and `scan()` / `run()` return paths
    - _Requirements: 5.2_
 
  - [x] 2.5 Write unit tests for timestamp extraction
    - `test_extract_timestamps_format`: mock ffprobe output with known PTS values; verify header and `int(pts * 1000)` per line
    - `test_timestamp_artifact_recovery_complete`: timestamps.txt present → `COMPLETE`
    - `test_timestamp_artifact_recovery_absent`: timestamps.txt absent → `ABSENT`
    - `test_timestamp_artifact_force_wipe`: `force_wipe=True` → file deleted
    - `test_merge_fails_without_timestamps`: `timestamps_path=None` → `FAILED` with clear message
    - _Requirements: 3.1–3.8, 5.3_

  - [x] 2.6 Write property test: PTS conversion correctness (Property 1)
    - **Property 1: PTS conversion correctness**
    - Generate random float PTS values in seconds; call conversion logic directly; verify each output line equals `int(pts_seconds * 1000)` and file starts with `# timestamp format v2`
    - Tag: `# Feature: pts-preservation, Property 1: PTS conversion correctness`
    - **Validates: Requirements 3.1, 3.2**

  - [x] 2.7 Write property test: timestamp filter independence (Property 2)
    - **Property 2: Timestamp filter independence**
    - Generate random include/exclude filter strings; run `_recover()` with a pre-existing `timestamps.txt`; verify `TimestampArtifact` is always in the artifact list and its state is only `COMPLETE` or `ABSENT`
    - Tag: `# Feature: pts-preservation, Property 2: Timestamp filter independence`
    - **Validates: Requirements 3.4, 3.5**

  - [x] 2.8 Write property test: timestamp artifact classification (Property 3)
    - **Property 3: Timestamp artifact classification**
    - Generate random file states (present, absent); run `_recover()`; verify `COMPLETE` iff file exists, `ABSENT` otherwise
    - Tag: `# Feature: pts-preservation, Property 3: Timestamp artifact classification`
    - **Validates: Requirements 3.6, 3.7**

- [x] 3. Checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Fix video and audio extraction commands
  - [x] 4.1 Fix video extraction command in `_execute_extraction()`
    - Remove `-fflags`, `+genpts`, `-avoid_negative_ts`, `make_zero` from video ffmpeg command
    - Add `-f`, `matroska` to video ffmpeg command (needed for `.tmp` protocol)
    - Remove `-y` (unified runner handles overwrite)
    - _Requirements: 2.1, 2.3, 2.4_

  - [x] 4.2 Fix audio extraction command in `_execute_extraction()`
    - Remove `-fflags`, `+genpts`, `-avoid_negative_ts`, `make_zero` from audio ffmpeg command
    - Remove `-y` (unified runner handles overwrite)
    - _Requirements: 2.2_

  - [x] 4.3 Write unit tests for extraction command correctness
    - `test_video_extraction_no_avoid_negative_ts`: verify `-avoid_negative_ts` absent from video cmd
    - `test_video_extraction_has_matroska_format`: verify `-f matroska` present in video cmd
    - `test_audio_extraction_no_avoid_negative_ts`: verify `-avoid_negative_ts` absent from audio cmd
    - _Requirements: 2.1, 2.2, 2.3_

- [x] 5. Replace mkvextract with ffmpeg for subtitle, chapter, and attachment streams
  - [x] 5.1 Add `_SUBTITLE_FFMPEG_FORMAT` mapping constant in `extraction.py`
    - `{"srt": "srt", "ssa": "ass", "ass": "ass"}` — text subtitle codecs needing explicit `-f`
    - _Requirements: 1.1, 1.5_

  - [x] 5.2 Replace subtitle extraction with `run_ffmpeg()` calls
    - For each subtitle track: `ffmpeg -i source -map 0:<track_id> -c copy [-f <fmt>] output_file`
    - Text subtitles (srt, ssa, ass): add `-f <fmt>` from `_SUBTITLE_FFMPEG_FORMAT`
    - Bitmap subtitles (pgs, sub): no `-f` flag needed
    - _Requirements: 1.1, 1.4, 1.5_

  - [x] 5.3 Replace chapter extraction with `run_ffmpeg()` call
    - `ffmpeg -i source -f ffmetadata output_file`
    - Update `ChaptersStream.file_extension` from `"xml"` to `"txt"`
    - _Requirements: 1.1, 1.4, 1.6_

  - [x] 5.4 Replace attachment extraction with `run_ffmpeg()` call
    - `ffmpeg -i source -dump_attachment:<track_id> <output_file> -t 0 -f null -`
    - Pass `output_file=None` to runner (dump_attachment writes directly, not through muxer)
    - _Requirements: 1.1, 1.4, 1.7_

  - [x] 5.5 Remove `MKVTrackExtractor.extract_tracks()` call from `_execute_extraction()`
    - Delete the `other_absent` / `extractor.extract_tracks()` block
    - Verify no remaining `mkvextract` calls exist in `extraction.py`
    - _Requirements: 1.1, 1.4_

  - [x] 5.6 Write unit tests for ffmpeg-based stream extraction
    - `test_subtitle_text_extraction_uses_ffmpeg`: verify `run_ffmpeg` called with `-f srt` / `-f ass`
    - `test_subtitle_bitmap_extraction_no_format_flag`: verify no `-f` for pgs/sub
    - `test_chapter_extraction_uses_ffmetadata`: verify `-f ffmetadata` in chapter cmd
    - `test_attachment_extraction_uses_dump_attachment`: verify `-dump_attachment:<id>` in cmd
    - `test_no_mkvextract_calls`: verify `mkvextract` is never invoked
    - _Requirements: 1.1, 1.4–1.7_

- [x] 6. Checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Merge phase — mkvmerge integration
  - [x] 7.1 Add `ExtractionPhase` as a direct dependency of `MergePhase`
    - Add `self._extraction: ExtractionPhase | None` to `MergePhase.__init__`
    - Wire from registry: `cast("_ExtractionPhase", phases[_ExtractionPhase]) if phases else None`
    - Add to `self.dependencies` list
    - Update `_ensure_dependencies()` to check extraction result is complete
    - _Requirements: 5.2_

  - [x] 7.2 Implement `_build_mkvmerge_options(chunks, output, timestamps_path)` in `merge.py`
    - Returns `list[str]`: `["-o", str(output), "--timestamps", f"0:{timestamps_path}", str(chunks[0])]` + `[f"+{c}" for c in chunks[1:]]`
    - First chunk has no prefix; each subsequent chunk is preceded by `"+"` as a separate element
    - `--timestamps` applied to track 0 of first chunk only
    - _Requirements: 4.3, 4.4, 5.1, 5.4_

  - [x] 7.3 Implement `_write_mkvmerge_options_file(path, args)` in `merge.py`
    - Write JSON array of strings using `.tmp`-then-rename protocol
    - Use `json.dumps(args, ensure_ascii=False, indent=2)`
    - _Requirements: 4.2, 4.3_

  - [x] 7.4 Replace ffmpeg concat block in `_execute_merge()` with mkvmerge invocation
    - Check `timestamps_path` from `self._extraction.result.timestamps_path`; if `None` or missing → log `critical`, return `_failed()`
    - Write options file to `final_dir / f"concat_{safe_name}.json"`
    - Run `subprocess.run(["mkvmerge", f"@{options_file}"], capture_output=True, text=True)`
    - On non-zero exit: log `error` with stderr tail (last 20 lines), leave options file on disk, append to `failed_strategies`
    - On success: delete options file with `options_file.unlink(missing_ok=True)`
    - _Requirements: 4.1, 4.2, 4.5, 4.6, 4.7, 4.8, 5.1, 5.3_

  - [x] 7.5 Fix Python string concatenation bug in the old ffmpeg concat command
    - Add missing comma between `"+genpts"` and `"-y"` in `concat_cmd` list
    - _Requirements: 7.1, 7.2, 7.3_

  - [x] 7.6 Write unit tests for mkvmerge integration
    - `test_mkvmerge_options_single_chunk`: one chunk → no `+` prefix
    - `test_mkvmerge_options_multiple_chunks`: N chunks → first no prefix, rest have `+` prefix
    - `test_mkvmerge_options_timestamps_placement`: `--timestamps 0:<path>` appears before first chunk
    - `test_mkvmerge_options_file_deleted_on_success`: options file deleted after successful merge
    - `test_mkvmerge_options_file_retained_on_failure`: options file retained after failed merge
    - `test_concat_bug_fix`: `"+genpts"` and `"-y"` are separate elements in concat_cmd list
    - `test_merge_fails_without_timestamps`: `timestamps_path=None` → `FAILED` with clear message
    - _Requirements: 4.1–4.8, 5.1, 5.3, 7.1–7.3_

  - [x] 7.7 Write property test: frame count preservation (Property 4)
    - **Property 4: Frame count preservation**
    - For any source video (using test fixture with mocked mkvmerge), verify `get_frame_count(merged_output) == get_frame_count(source)`
    - Tag: `# Feature: pts-preservation, Property 4: Frame count preservation`
    - **Validates: Requirement 6.1**

  - [x] 7.8 Write property test: PTS monotonicity (Property 5)
    - **Property 5: PTS monotonicity**
    - For any merged output (using test fixture), extract PTS values via ffprobe and verify they are strictly increasing
    - Tag: `# Feature: pts-preservation, Property 5: PTS monotonicity`
    - **Validates: Requirement 6.2**

  - [x] 7.9 Write property test: PTS accuracy (Property 6)
    - **Property 6: PTS accuracy**
    - For any source video (using test fixture), verify `abs(merged_pts[i] - source_pts[i]) <= 1` for all frames
    - Tag: `# Feature: pts-preservation, Property 6: PTS accuracy`
    - **Validates: Requirement 6.3**

- [x] 8. Final checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [-] 9. Cross-spec review and spec housekeeping
  - Review this spec against `merge-phase-revamp`, `ffmpeg-unified-runner`, `phase-recovery-refactor`, and `metrics-two-tier`
  - Add or update cross-spec summary at the top of this spec and at the top of each related spec where relevant changes or supersessions exist
  - Update `Completed` date in this spec (`- Completed: <ISO date>`)
  - _Requirements: 9.1, 9.2_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Unit tests go in `tests/unit/test_extraction_pts.py` and `tests/unit/test_merge_mkvmerge.py`
- Property-based tests go in `tests/test_pts_preservation_properties.py` (Hypothesis, min 100 iterations each)
- `ffprobe` calls use `subprocess.run` directly — consistent with `MKVTrackExtractor._run_ffprobe()`; do NOT route through `run_ffmpeg()`
- `mkvmerge` calls use `subprocess.run` directly — consistent with the existing `mkvextract` pattern; do NOT route through `run_ffmpeg()`
- All `ffmpeg` calls MUST go through `run_ffmpeg()` / `run_ffmpeg_async()` per coding standards
- The `.tmp`-then-rename protocol applies to `timestamps.txt` and the mkvmerge options file; it does NOT apply to attachment extraction (ffmpeg writes directly via `-dump_attachment`)
- `_build_registry` in `phase.py` must be updated to wire `ExtractionPhase` into `MergePhase`
