# Implementation Plan — File → Stream Object Model & Direct-From-Source Processing

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-25
- Completed:

## Overview

Staged so every task lands green: the runner converts first (pure mechanical, command output provably identical), the model family lands second with its own tests, then phases migrate in dependency order, and dead code is swept last. Requirement ids reference `requirements.md`.

## Notes

- After each code task run `uv run ruff check .` and the relevant `uv run python -m pytest`.
- Golden command tests are the safety net for the runner conversion: pin composed argv for every converted call site **before** changing behavior anywhere.
- No legacy-compat layers: old models/classes may coexist with new ones only *between* stages (incremental migration), never inside the final state — Task 9 removes them all.
- Tests target observable behavior; each test names the bug it prevents.
- Run the full e2e smoke on real media (`D:\_encoding\source\*.mkv`, `--work-dir D:\_encoding\pyqenc_tmp`) after Tasks 5, 8 and 9.

## Tasks

- [ ] 1. FFmpeg request model + runner conversion (Req 8)
  - Add `FFmpegInput` / `FFmpegRequest` to `utils/ffmpeg_runner.py`; compose argv exactly per design (progress flags, `-y`, `-map_chapters -1`, per-input `-ss`/`-t`/`-i`, maps, `filter_complex`, output stage, null output)
  - Convert `run_ffmpeg_async`/`run_ffmpeg` to take a request; keep progress parsing, `.tmp`-then-rename + muxer, stderr handling, kill registry, sync-loop guard; remove `video_meta=`
  - `get_frame_count` builds a request internally
  - Convert all existing call sites mechanically (encode, quality, cropdetect, extraction ×4, screenshots ×2, audio measure/apply) — same inputs, same windows, no behavior change yet
  - Golden command tests: every call site's composed argv pinned against the current hand-built command

- [ ] 2. Model family (Req 1–5, 15)
  - New module (e.g. `pyqenc/stream_model.py`): `File`; generic `Stream[InfoT]` base + `StreamInfo` (container-level fields: `track_id`, `codec_name`, `language`, `title`, `start_timestamp`, `duration_seconds`); named subclasses binding the type parameter — `class VideoStream(Stream[VideoStreamInfo])` etc. for audio/subtitle/attachment — so `.info` is statically concrete with no casts at use sites; `ExtendedVideoStream`; `VideoStreamChunk`; `EncodedChunk`. Chapters/timestamps are plain sidecar fields, not classes
  - Eager fields only; producer-contract guards as plain asserts naming the field and phase
  - `as_input()` implemented once on the `Stream` base; chunk override adds the window; `chunk_id` derived property + parse classmethod on the chunk class (absorbing `_chunk_name_duration`) — the class is the sole consumer of the chunk-name constants
  - Single shared filesystem-sanitize helper (Req 15.2): Windows-forbidden set + control chars, replacement never rejection — for media-sourced free text (stream titles) only
  - Safe-name check + config-load rejection for profile/preset names; unit test pinning every bundled `default_config.yaml` name as safe (Req 15.6 — strategy names safe by construction, no runtime sanitize)
  - Sidecar (de)serialization for the new slices: `job.yaml` shrunk schema, `extraction.yaml` inventory, `chunking.yaml` scenes without `chunking_mode` — `model_dump(exclude_none=True)`/`model_validate` only, no hand-written pairs; type conversions (Fraction/Decimal/Path) declared once on annotated types
  - Unit tests: unique-slice dumps (no parent fields leak), `dump → load → dump` byte-identity, source-identity validation, naming round-trips (`parse(format(x)) == x`)

- [ ] 3. JobPhase + CLI (Req 1)
  - CLI `source` → `type=LongPath`; `api._drive` passes it through
  - JobPhase: eager `File`, shrunk `job.yaml`, path+size mismatch check, drop the estimation call and the resolution re-probe
  - `JobPhaseResult` carries `File`
  - Update `test_job_phase.py` for the new mismatch semantics

- [ ] 4. ExtractionPhase (Req 2, 7.4, 7.7, 11.1, 13, 15.7)
  - Full ffprobe enumeration → typed stream objects, persisted to `extraction.yaml`; reuse-run loads the sidecar (no re-probe) and validates source identity
  - Stream classes own `extracted_file_name()` generation — `#N (type-codec) lang=…` with N = track_id, `#NN ID=N ` prefix collapsed (absorbing `StreamBase` naming; titles via the shared sanitize) — no parser needed, the sidecar carries paths
  - Extract only timestamps/chapters/subtitles/attachments; delete video/audio track copy paths
  - Attachment dumps + chapters writes wrapped tmp-then-rename at phase level (file-trust rule, Req 7.7 — today both bypass the protocol via `output_file=None`)
  - Move disk-space estimation here on real `VideoStreamInfo`; rework constants (drop FFV1/remux/extraction terms)
  - Multi-video-stream warning
  - `ExtractionPhaseResult` carries the stream objects
  - Update `test_extraction_pts.py` + `test_extraction_streams_filter.py`

- [ ] 5. ProbePhase + ChunkingPhase (Req 3, 4, 9.2–9.3)
  - Probe: frame count from `timestamps.txt` (new `utils/timestamps.py` parser; null-count fallback), cropdetect via `stream.as_input()`, emits `ExtendedVideoStream` with non-optional crop (empty on auto-detect failure + warning); `probe.yaml` shape unchanged (crop key omitted when empty, materialized empty on load)
  - Chunking: scenedetect on source; boundaries → `VideoStreamChunk` list (timestamp windows + detector-derived `frame_count` from boundary-frame differences, last closing against the source total); delete `split_chunks`, `ChunkingMode`, `FFV1_VIDEO_ARGS`, chunk sidecars, `ChunkArtifact`, `chunks/` handling, `--chunking-mode` flag and `chunking.mode` config
  - E2E smoke on real media (first full direct-from-source chunking)
  - Update `test_chunking.py`, `test_probe_phase.py`

- [ ] 6. EncodingPhase + OptimizationPhase + quality (Req 7.1–7.3, 9.1, 9.4–9.6, 10, 14, 15)
  - Encode from `chunk.as_input()`; quality consumes `attempt.stream.as_input()` + `chunk.as_input()` — per-side crop from `stream.crop`, reference windowed with `-ss`/`-t`
  - `EncodedChunk.file_name` / `parse_file_name` (typed `chunk_id`/`resolution`/`crf` record) own the attempt filename; sidecar paths derive from it; strategy dir embeds the strategy name verbatim — safe by construction (Req 15.6); drop `_enc_encoded_strategy_dir`, `Strategy.safe_name`, and merge's inline mappings
  - Codec config split (`pre_input_args` / `encoder_args` without `"-i", "{input}"`); migrate `default_config.yaml` templates incl. nvenc
  - `EncodedChunk` composes `stream: ExtendedVideoStream` (the attempt's own video: crop empty by construction, `frame_count` from `result.frame_count`, info from one fast post-encode probe driving the filename) + `chunk` + `Strategy` + `crf`; no duplicated path/frame_count fields; `AttemptMetadata`/`EncodedArtifact` duality collapses
  - Invariant checks wired: per-chunk attempt-count agreement (hard — same window, same frames), Σ attempts vs source (hard), attempt vs detector chunk count → vocal warning with diagnostics (±1 boundary expected from seek rounding); seek targets floored to µs so rounding never drops a boundary frame
  - Visualization x-axes → timestamps via `fps_fraction`
  - Update `test_encoding_phase.py`, `test_optimization_phase.py`, `tests/integration/test_encoding_quality.py`

- [ ] 7. AudioPhase + MergePhase (Req 7.5, 15.6, 15.8)
  - Audio chains consume `AudioStream.as_input()`; delete `.mka` extraction consumption; measurement passes parse `result.stderr_lines`; `-vn/-sn/-dn` dropped (explicit single-stream `-map` subsumes them); injectable runner signature becomes `FFmpegRequest → FFmpegRunResult`; audio.yaml signatures/sidecar unchanged
  - Merge consumes `EncodedChunk`/`Strategy` objects; output name derived in one place from `File.path.stem` + the strategy name (safe by construction); mechanics (mkvmerge append + `--timestamps`) untouched; final frame-count verify stays
  - Update `test_audio_phase.py`, `test_merge_mkvmerge.py`

- [ ] 8. measure + cleanup levels (Req 11.4, 8.8)
  - `measure.py` adopts `File`/stream loaders; drop ad-hoc `VideoMetadata` usage
  - Single-frame screenshots routed through the tmp protocol via `output_format: "image2"`; pattern sequences stay in caller-managed temp dirs
  - Cleanup: INTERMEDIATE = encoding workspace; ALL = + `encoded/`; remove `chunks/` paths
  - E2E smoke on real media

- [ ] 9. Dead-code sweep + full verification (Req 6.4)
  - Delete `VideoMetadata`/`ExtendedVideoMetadata`/`ChunkMetadata`, `StreamBase`/`StreamFactory`/`MKVTrackExtractor`, `populate_from_ffmpeg_output`, `ChunkingParams.chunking_mode` remnant, `probe_extended`, FFV1/remux constants, and all remaining `to_yaml_dict`/`from_yaml_dict` pairs (Req 5.4)
  - Project-wide search for leftovers; `uv run ruff check .`; full `uv run python -m pytest`
  - Full e2e on real media; verify the invariant chain end-to-end (source == Σ attempts == final)
  - setpts comparison experiment on real media (Req 12.3) — record results in this spec; drop the flag in a follow-up only if measurably redundant

- [ ] 10. Cross-spec review + TODO reconciliation
  - Review this spec against `2026-03-17 ffmpeg-unified-runner`, `2026-04-29 pts-preservation`, `2026-09-01 probe-phase-refactor`, `2026-09-11 audio-chains`; add difference summaries to the tops of both specs
  - `2026-03-15 ffv1-lossless-chunking` archived with successor note (done at spec creation)
  - Prune TODO.md entries covered here (§8, §25, §26, §28, §29, §31); annotate §33
  - Docs: replace `docs/Pipeline flow overview.mmd` with `docs/Pipeline flow target.mmd` content (delete the target file once merged)

- [ ] 11. Finalize
  - Update `- Completed:` date in all three files
