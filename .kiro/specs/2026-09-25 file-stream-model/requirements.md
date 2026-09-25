# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: File → Stream Object Model & Direct-From-Source Processing
- Created: 2026-09-25
- Completed:

## Cross-Spec Notes

### What this spec supersedes

| Superseded requirement | Original spec | What changed |
|---|---|---|
| FFV1 lossless chunk files (`-c:v ffv1 -g 1 …` splits, `ChunkingMode`, `--chunking-mode` flag, remux fallback) | `2026-03-15 ffv1-lossless-chunking` | Chunk files no longer exist. Encoders consume the source file directly through `-ss/-t` windows with accurate seek; frame-perfection comes from decode-after-seek, not all-intra intermediates. The entire spec is archived. |
| Runner API taking a hand-built `cmd` list + `output_file` | `2026-03-17 ffmpeg-unified-runner` | The runner core (progress injection/parsing, `.tmp`-then-rename, stderr collection, kill registry) is retained; the call API becomes a structured `FFmpegRequest` (inputs with selectors/windows, output stage). |
| Forward reference "In-memory stream objects (no on-disk extraction)" | `2026-09-11 audio-chains` §Forward references | Realized here for chain *inputs*: chains read the source via `AudioStream.as_input()` selectors instead of extracted `.mka` files. The `passthrough`-without-file idea remains out of scope (merge still needs external audio files). |
| `VideoMetadata`/`ExtendedVideoMetadata`/`ChunkMetadata` lazy-property hierarchy | (code, no spec) | Replaced by the eager composition family below; lazy properties and in-place `video_meta=` population are removed. |

### Related, not superseded

- `2026-04-29 pts-preservation` — global PTS restoration via `timestamps.txt` at merge is unchanged; this spec adds the frame-preservation invariant on top of it.
- `2026-09-01 probe-phase-refactor` — ProbePhase keeps its sidecar and slow-facet ownership; its frame-count source changes (see Req 9).

## Introduction

The pipeline currently materializes every intermediate on disk: ExtractionPhase remux-copies the full video and every audio track into `extracted/` (≈1.1× the source), and ChunkingPhase re-encodes every scene window to FFV1 all-intra chunk files (≈0.30 bytes/pixel ≈ 4–6× the source — the dominant disk cost) purely so that the encoder and quality measurement have small local inputs. Peak intermediate footprint is roughly 6–9× the source for what is, end to end, a read of the same frames. This was fine for testing the pipeline; it is too wasteful for production.

At the same time the object layer grew identity churn: the same logical file becomes a new `VideoMetadata` at seven call sites, the source is re-probed by `MKVTrackExtractor` on every run even when everything is complete, chunk sidecars are re-read per artifact per run, and `ProbeState` is rebuilt four times per run. Metadata is lazy: properties silently trigger ffprobe, forcing dichs like the `chunk._resolution` workaround.

This spec replaces both with one design:

1. **An eager composition object model** — `File` → `VideoStream` → `ExtendedVideoStream` → `VideoStreamChunk`, plus `AudioStream` and subtitle/attachment streams on a shared generic `Stream[InfoT]` base (chapters/timestamps stay container-level artifacts). Each object is instantiated exactly once per run by its owning phase and reused by every downstream phase; each sidecar persists only the properties unique to that object ("clean dumps").
2. **Direct-from-source processing** — encoding, quality measurement, crop detection, frame counting, scene detection and audio chains all read the original source file through explicit `-map` selectors and `-ss/-t` timestamp windows. Full video/audio stream copies and FFV1 chunk files are eliminated.
3. **A structured ffmpeg runner** — `FFmpegInput`/`FFmpegRequest` replace hand-built command lists; the runner owns progress flags, `-y`, the `.tmp`-then-rename protocol and muxer injection, as today.

Frame-exactness stops being a positioning concern and becomes a verification invariant: `source.frame_count == Σ winning-attempt.frame_count == final.frame_count`, with every count obtained for free (no dedicated playback run).

## Glossary

- **File** — pydantic model: `LongPath` link to a file on disk plus basic identity metadata (`file_size_bytes`). Owned by JobPhase; persisted as `job.yaml`.
- **Stream** — pydantic model composing a `File` with type-specific stream info (video/audio/subtitle/attachment), parametrizing the generic `Stream[InfoT]` base. Owned by ExtractionPhase; the unique-info slice is persisted in `extraction.yaml`. Chapters/timestamps are not streams.
- **Fast facet / slow facet** — ffprobe-derived stream properties (fps, resolution, duration, …) vs. properties requiring a full pass over the data (frame count, crop). Extraction owns the fast facet, ProbePhase the slow one.
- **Extended video stream** — composition of `VideoStream` + slow facet (`frame_count`, `crop`). The type every downstream video phase accepts.
- **Chunk** — `VideoStreamChunk`: an extended video stream plus a `[start_timestamp, end_timestamp)` window. No file on disk.
- **Selector** — ffmpeg `-map` target (e.g. `0:2`) identifying a stream inside its container.
- **Window** — input-side `-ss <start>` + `-t <duration>` pair bounding the frames read from an input.
- **Unique-property dump** — serialization of only the fields a class adds beyond its composed references; parents are re-linked at load time.
- **Preservation invariant** — the frame-count chain `source == Σ winning attempts == final` proving no frames were lost or duplicated across encoding and merge (windows tile the source by construction).

## Requirements

### Requirement 1 — File object

**User Story:** As a developer, I want a minimal `File` model owned by JobPhase, so that file identity is established exactly once per run and downstream phases never re-derive it.

#### Acceptance Criteria

1. THE Pipeline SHALL define a pydantic `File` model with `path: LongPath` and `file_size_bytes: int | None`.
2. THE CLI SHALL parse the `source` argument as `LongPath` (today a plain `Path`) and pass it to JobPhase.
3. JobPhase SHALL construct exactly one `File` per run — populated eagerly from the filesystem — and expose it on `JobPhaseResult`.
4. `job.yaml` SHALL persist only the `File` dump: `{source: {path, file_size_bytes?}}`.
5. JobPhase source-mismatch detection SHALL compare the persisted `path` + `file_size_bytes` against live values; the current resolution-based re-probe comparison SHALL be removed.

### Requirement 2 — Stream objects (fast facet)

**User Story:** As a developer, I want typed stream objects composing the `File`, so that stream metadata is instantiated once at ExtractionPhase and carries its own selector for direct ffmpeg use.

#### Acceptance Criteria

1. THE Pipeline SHALL define a generic pydantic base `Stream[InfoT]` composing `file: File` and `info: InfoT`, with `as_input()` (Req 8.5) implemented once on the base. `VideoStream`, `AudioStream`, `SubtitleStream` and `AttachmentStream` SHALL be named subclasses binding the type parameter (`class VideoStream(Stream[VideoStreamInfo])`), such that `stream.info` is statically the concrete info type everywhere — consumers SHALL NOT cast or runtime-inspect the info type, and the composition chain (`ExtendedVideoStream.stream: VideoStream`, `VideoStreamChunk.stream: ExtendedVideoStream`) SHALL be concrete at every link.
2. All `*StreamInfo` models SHALL share, via a common `StreamInfo` base, the container-level properties every stream carries: `track_id: int`, `codec_name: str | None`, `language: str | None`, `title: str | None`, `start_timestamp: float | None`, `duration_seconds: float | None`. Placement (`start_timestamp`, `duration_seconds`) is per-stream container placement — each stream sits at an offset on the container timeline (MKV per-track block timestamps/`CodecDelay`, MP4 per-track edit lists, TS per-stream PTS) and A/V alignment derives from the streams' differing start times — so it is common, not video/audio-specific. The generic `Stream` base SHALL add no dumpable fields of its own — dumps are the info slice (inherited `StreamInfo` fields included), never the composed `File` (Req 5.1).
3. `VideoStreamInfo` SHALL add `fps: float | None`, `fps_fraction: Fraction | None` (serialized as `[num, den]`), `resolution: str | None`, `pix_fmt: str | None` to the base fields. Stream properties SHALL NOT exist on `File`.
4. `AudioStreamInfo` SHALL add `layout: ChannelLayout | None` to the base fields; `SubtitleStreamInfo` SHALL add `is_forced: bool` plus its extracted-file path; `AttachmentStreamInfo` SHALL add `filename` plus its extracted-file path.
5. Chapters and timestamps SHALL be modeled as container-level artifacts (extracted paths in `extraction.yaml`), not stream classes — they carry no `track_id` and are not `-map` selectable.
6. ExtractionPhase SHALL create each stream object exactly once per run — from the full ffprobe enumeration on a fresh run, or by loading `extraction.yaml` on a reuse run (no re-probe when the sidecar is present and its source identity matches).
7. `extraction.yaml` (new sidecar, owned by ExtractionPhase) SHALL persist the stream inventory: per-type info lists plus the source identity (`path`, `file_size_bytes`) for invalidation, plus extracted paths for timestamps/chapters/subtitles/attachments.
8. WHEN the source identity recorded in `extraction.yaml` does not match `job.yaml`, THE Pipeline SHALL re-enumerate streams and rewrite the sidecar.

### Requirement 3 — Extended video stream (slow facet)

**User Story:** As a developer, I want an `ExtendedVideoStream` carrying frame count and crop above the base stream, so that video processing phases can demand the slow probe via the type system and audio-only runs never pay for it.

#### Acceptance Criteria

1. THE Pipeline SHALL define `ExtendedVideoStream` composing `stream: VideoStream`, `frame_count: int` (0 = unknown sentinel, as today) and `crop: CropParams` — **non-optional**: an empty `CropParams` (`is_empty()`) means "no crop". `crop = None` SHALL exist only at config/CLI level (meaning "auto"), never in the stream model.
2. WHEN auto crop detection fails, ProbePhase SHALL fall back to an empty `CropParams` and log a warning.
3. ProbePhase SHALL be the sole producer/owner of the slow facet and SHALL expose `ExtendedVideoStream` on `ProbePhaseResult`.
4. Every downstream video phase (chunking, optimization, encoding, merge, measure) SHALL accept `ExtendedVideoStream` — not the base `VideoStream` — as its video input type.
5. `probe.yaml` SHALL remain the slow-facet sidecar with its current shape, owned by ProbePhase. The `crop` key MAY be omitted from the file when the crop is empty (serialization compactness only); loading SHALL always materialize a `CropParams` — an absent key loads as empty. Loading SHALL compose the base stream from the ExtractionPhase result with the persisted facet.
6. All stream fields SHALL be plain (eager) values: no property access SHALL trigger a probe. Producer-contract guards SHALL be plain asserts, e.g. `assert stream.info.fps is not None, "fps guaranteed by ExtractionPhase"` — a violated guarantee is a programming bug, not a user-facing validation error.

### Requirement 4 — Chunk objects (timestamp positioning)

**User Story:** As a developer, I want chunks defined purely as timestamp windows, so that positioning works uniformly for CFR and VFR and no frame-index arithmetic exists in the pipeline.

#### Acceptance Criteria

1. THE Pipeline SHALL define `VideoStreamChunk` composing `stream: ExtendedVideoStream` with `start_timestamp: float`, `end_timestamp: float`, `chunk_id: str` (derived from the timestamp range — current naming unchanged) and `frame_count: int` — derived from the detector-returned boundary frames (Req 9.4; `0` = unknown), not an independent count.
2. `VideoStreamChunk` SHALL NOT carry frame-position fields (`start_frame`/`end_frame`).
3. ChunkingPhase SHALL produce chunk objects derived from persisted scene boundaries plus stream duration, with no per-chunk files and no per-chunk sidecars written.
4. `chunking.yaml` SHALL persist scene boundaries as `{timestamp_seconds, frame?}`; the detector-reported `frame` value is informational only and no code path SHALL depend on it.
5. The `chunks/` directory, `ChunkingMode` (enum, `chunking.mode` config key and `--chunking-mode` CLI flag), `FFV1_VIDEO_ARGS`, and the `split_chunks`/`ChunkArtifact` machinery SHALL be deleted.

### Requirement 5 — Unique-property persistence

**User Story:** As a user, I want every YAML sidecar to contain only the properties unique to its owning object, so that the files stay human-checkable and loading re-links compositions cleanly.

#### Acceptance Criteria

1. Serializing any stream/chunk object SHALL exclude all composed references (`File`, parent streams); only the object's own info slice SHALL be written.
2. Loading SHALL re-compose: the job's `File` and ExtractionPhase's stream objects are attached from the in-run object graph, with the sidecar's source identity validated against them.
3. Round-trip `dump → load → dump` SHALL be byte-identical for every sidecar (unit-tested).
4. THE Pipeline SHALL NOT use hand-written dict serializers for the new model family: `model_dump(exclude_none=True)` / `model_validate()` are the only (de)serialization path. Key renames SHALL be resolved by renaming the field so code and YAML share one short concise name (docstrings and type constraints elaborate meaning) — since every sidecar is ours, no YAML key is externally fixed and the rename path is always available. Type conversions (`Fraction` ↔ `[num, den]`, `Decimal`, `Path`) SHALL be declared once on the annotated type, never per model. The existing hand-written `to_yaml_dict`/`from_yaml_dict` pairs SHALL be eliminated as their phases migrate.
5. Sidecar ownership SHALL remain one-phase-one-sidecar: `job.yaml` (Job), `extraction.yaml` (Extraction), `probe.yaml` (Probe), `chunking.yaml` (Chunking), plus the existing audio/optimization/encoding/merge sidecars unchanged in role.

### Requirement 6 — Single instantiation and reuse

**User Story:** As a developer, I want each logical entity constructed exactly once per run and passed by reference through phase results, so that no phase re-probes or re-reads another phase's data.

#### Acceptance Criteria

1. THE Pipeline SHALL instantiate: `File` at JobPhase; every stream at ExtractionPhase; `ExtendedVideoStream` at ProbePhase; chunks at ChunkingPhase; encoded attempts at EncodingPhase.
2. All other phases SHALL obtain these objects via the `DEPENDS_ON`/`_dep()` result mechanism and SHALL NOT construct duplicates of source/stream/chunk metadata (the `job.py`/`extraction.py`/`encoding.py` re-instantiation sites are removed).
3. The one sanctioned exception: throwaway probe instances inside validation logic (e.g. live source checks), which are never part of the shared object graph.
4. `VideoMetadata`, `ExtendedVideoMetadata`, `ChunkMetadata`, `StreamBase`/`StreamFactory`/`MKVTrackExtractor` and the `populate_from_ffmpeg_output` lazy-population plumbing SHALL be deleted once migration completes.

### Requirement 7 — Direct-from-source processing

**User Story:** As a user, I want the pipeline to read frames directly from the original source file, so that intermediate video/audio copies and lossless chunk files never consume disk.

#### Acceptance Criteria

1. Encoding (and optimization test-encodes) SHALL encode each chunk from `ffmpeg -ss <start> -i <source> -map 0:<track_id> -t <duration> <codec args> <attempt.mkv>`.
2. Quality measurement SHALL consume both sides as video stream objects (Req 14): the attempt's own stream (read whole) and the chunk window as reference (windowed with per-input `-ss`/`-t`).
3. Crop detection, frame counting and scene detection SHALL read the source with an explicit `-map 0:v:<track_id>` selector.
4. ExtractionPhase SHALL NOT extract video or audio tracks to `extracted/`; it SHALL extract only `timestamps.txt` (unconditional), chapters, subtitles and attachments (per include/exclude filters).
5. AudioPhase chains SHALL consume `AudioStream.as_input()` (source + selector); no `.mka` intermediates SHALL be produced.
6. The explicit single-stream `-map` SHALL make stray-stream leakage impossible in encode commands (resolves the `-vn/-sn/-dn` half-measure concern, TODO §8's video half).
7. **File trust:** no pipeline command SHALL write directly to a final artifact path — every file-producing operation SHALL go through the runner's `.tmp`-then-rename protocol (Req 8.2), and non-runner producers (e.g. `mkvextract` chapters, `-dump_attachment` attachment dumps) SHALL wrap their writes the same way (produce into a `.tmp` sibling, rename on verified success). THE presence of a file at its final name SHALL imply a complete, successful write; presence-based recovery (encoded winners, audio outputs, extracted artifacts) depends on this equivalence.

### Requirement 8 — FFmpeg runner request model

**User Story:** As a developer, I want the runner to accept structured inputs (source, selectors, windows, output), so that it owns command composition and every call site is declarative.

#### Acceptance Criteria

1. THE Pipeline SHALL define `FFmpegInput` (`path: LongPath`, `selector: str | None`, `start_seconds: float | None`, `duration_seconds: float | None`, `pre_input_args: tuple[str, ...]`) and `FFmpegRequest` (`inputs: list[FFmpegInput]`, `output_args`, `filter_complex: str | None`, `output: LongPath | None`, `output_format: str | None`).
2. `run_ffmpeg(request, …)` and `run_ffmpeg_async(request, …)` SHALL retain today's contracts: sync wrapper raises inside a running loop; the runner injects progress flags, owns `.tmp`-then-rename with explicit muxer, collects stderr, registers live processes for kill, and always emits `-y`.
3. Command composition SHALL be: per input `[pre_input_args] [-ss start] [-t duration] -i path`; then `-map <selector>` per input in order; then optional `-filter_complex`; then `output_args`; then output or `-f null -`.
4. WHEN `output is None`, THE runner SHALL emit a null output and skip the `.tmp` protocol (current behavior).
5. `Stream` SHALL provide the `as_input()` adapter on the generic base (Req 2.1), building `FFmpegInput` from its composition; `VideoStreamChunk` SHALL override it to add the window.
6. `FFmpegRunResult.frame_count` SHALL remain the authoritative free source of frame counts from any run; the `video_meta=` in-place population parameter SHALL be removed.
7. All existing call sites (~13) SHALL be converted; hand-built command lists SHALL no longer exist outside the runner (golden command tests pin the composed argv).
8. Single-frame screenshot outputs SHALL use the tmp protocol via `output_format: "image2"`. Multi-frame image-pattern outputs (`%04d.png` sequences) MAY bypass the substitution protocol — they write into caller-managed temp directories with their own lifecycle and are never presence-recovered artifacts.
9. THE runner SHALL inject `-map_chapters -1` into every request. ffmpeg copies input-container chapters to output regardless of `-map`, so without this guard every attempt would carry its window's slice of source chapters (and audio `.mka`/mp4 outputs the full set), which mkvmerge append then concatenates into thousands of fragmented chapter records. No ffmpeg output in this pipeline intentionally carries chapters — `chapters.xml` is the carrier, and restoring originals at merge is TODO §10's mechanism (mkvmerge-level, unaffected by the runner).

### Requirement 9 — Frame preservation invariant

**User Story:** As a user, I want proof that no frames were lost or duplicated, so that "frame-exact" becomes a verified property instead of positional machinery.

#### Acceptance Criteria

1. THE Pipeline SHALL maintain the invariant `source.frame_count == Σ chunk.frame_count == Σ winning-attempt.frame_count == final.frame_count`, where Σ chunks equals the source count **by construction** (chunk counts telescope from the same source total — Req 9.4) and the load-bearing checks are the attempt and final sums.
2. Source frame count SHALL come primarily from the total line count of `timestamps.txt` (exact per-frame PTS list; a total involves no windowing, hence no boundary-attribution error modes); the null-count ffmpeg pass SHALL remain only as fallback (timestamps file absent/unreadable) and for final-merge verification.
3. Attempt frame counts SHALL be taken from the encode run's `FFmpegRunResult.frame_count` and stored as `EncodedChunk.stream.frame_count` — THE Pipeline SHALL NOT run a second ffmpeg pass just to count attempt frames.
4. Chunk frame counts SHALL be derived from the detector-returned boundary frames: `next_boundary.frame − boundary.frame`, with the last chunk closing against the source total (`stream.frame_count − last_boundary.frame`); `0` = unknown when the source count is unknown. Per-window counting of `timestamps.txt` SHALL NOT be used (millisecond-rounded `timecodes_v2` vs microsecond seeks — attribution noise).
5. WHEN a chunk's attempt count differs from its detector-derived chunk count, THE Pipeline SHALL emit a vocal warning with diagnostics (chunk id, expected vs actual, boundary timestamps). A ±1 boundary disagreement is an expected artifact of seek-target rounding, not a failure; genuinely lost or duplicated frames still surface as hard errors in the sums.
6. WHEN two attempts of the same chunk report different frame counts, THE Pipeline SHALL treat it as an error (the same window must encode the same frames).
7. Disagreements in the invariant sums (Σ attempts vs source, final vs source) SHALL be hard errors logged with per-chunk detail.
8. Window seek targets SHALL be formatted by flooring to microseconds, so rounding can never skip the intended boundary frame.
9. Quality plots and any frame-indexed visualization SHALL use timestamp x-axes, converting via `fps_fraction` (average fps) where a log is frame-indexed.

### Requirement 10 — Codec config split

**User Story:** As a user configuring hardware codecs, I want pre-input and post-input arguments as distinct lists, so that hwaccel/vulkan setup is structurally separated from encoder tuning.

#### Acceptance Criteria

1. Each codec in `default_config.yaml` SHALL define `pre_input_args: []` (single source of truth for the default) and `encoder_args` holding only post-input tokens.
2. The `"-i", "{input}"` pair SHALL be removed from all `encoder_args` templates; `{vf}`, `{quality}`, `{preset}`, `{profile_args}` substitution is unchanged.
3. `Strategy` SHALL expose both stages, and the encoder SHALL pass codec `pre_input_args` into the request's input; nvenc/vulkan templates SHALL place `-hwaccel*`/`-init_hw_device` tokens in `pre_input_args`.

### Requirement 11 — Disk estimation and cleanup rework

**User Story:** As a user, I want space estimation based on real stream data at the point of first ownership, so that estimates reflect the new pipeline's actual footprint.

#### Acceptance Criteria

1. Disk-space estimation SHALL move from JobPhase to ExtractionPhase, using the enumerated `VideoStreamInfo` (real fps/resolution/duration) instead of job-level cached heuristics.
2. Estimation terms for extraction multipliers and FFV1/remux chunks SHALL be removed; remaining terms cover encoding attempts and finals (constants updated in `constants.py`; `default_config.yaml` remains the source of truth for configurable values).
3. Estimation SHALL remain log-only (the blocked branch stays disabled — the open question in TODO §33 is unchanged).
4. `CleanupLevel.INTERMEDIATE` SHALL cover the encoding workspace; `CleanupLevel.ALL` additionally the encoded winners tree; `extracted/` shrinks to its surviving content; the `chunks/` cleanup path is deleted.

### Requirement 12 — `setpts=PTS-STARTPTS` scoping

**User Story:** As a developer, I want the timeline normalization flag scoped to where it is load-bearing and verified, so that it is not cargo-culted.

#### Acceptance Criteria

1. `setpts=PTS-STARTPTS` SHALL be kept in the quality filter graph only, as the explicit co-basing of the two metric inputs' timelines.
2. It SHALL NOT appear in any encode path or merge command; encoded attempts keep natural 0-based PTS and merge restores global PTS via `mkvmerge --timestamps` from `timestamps.txt` (unchanged).
3. A one-time e2e task SHALL compare quality metrics with and without the flag on real media; the flag SHALL be dropped in a follow-up if measurably redundant.

### Requirement 13 — Multi-video-stream guard

**User Story:** As a user with an unusual source, I want loud validation when automatic stream selection could pick the wrong track.

#### Acceptance Criteria

1. WHEN the source contains more than one video stream, ExtractionPhase SHALL log a prominent warning that scene detection and default-selector consumers operate on the first video stream.
2. All pipeline reads of the source SHALL use explicit selectors derived from `track_id` (Req 7.3); the warning covers only external consumers (PySceneDetect/PyAV) that cannot be pointed at an arbitrary stream.

---

### Requirement 14 — Encoded attempt as a video stream

**User Story:** As a developer, I want the encoded attempt modeled with the same stream objects as everything else, so that quality measurement is symmetric and the attempt's path, size, resolution and frame count have a single typed home.

#### Acceptance Criteria

1. `EncodedChunk` SHALL compose `stream: ExtendedVideoStream` describing the attempt file's own video — `crop` empty by construction (applied during encode), `frame_count` from the encode run's `FFmpegRunResult.frame_count` — plus `chunk: VideoStreamChunk`, `strategy: Strategy` and `crf: Decimal`. It SHALL NOT duplicate `path`/`frame_count` fields: the single sources are `stream.file.path`, `stream.file.file_size_bytes` and `stream.frame_count`.
2. The attempt's `VideoStreamInfo` SHALL be populated eagerly, once, after the encode by a single fast probe (resolution drives the attempt filename, as today); recovery SHALL rebuild it from the filename without probing.
3. Quality measurement SHALL treat crop as a per-input consumption property: the filter graph applies each side's `stream.crop` (empty for the attempt, the detected crop for the source window) — no phase-level crop special-casing in the graph builder.

---

### Requirement 15 — Naming ownership

**User Story:** As a developer, I want every artifact name generated and parsed by the class that owns the identity it encodes, so that naming routines and their parsers stop being scattered across phases.

#### Acceptance Criteria

1. Each name family SHALL have exactly one owning class: generation (property or method) and parsing (classmethod returning a typed record) SHALL live on that class, and no format string, regex, or inline mapping for that family SHALL exist outside it. Separator/pattern constants stay in `constants.py`; the owning class SHALL be their sole consumer.
2. Exactly two kinds of names SHALL exist. **Display names** (logs, tables, targeting strings) carry any symbols, show identity fields verbatim, and SHALL never be used on disk. **Filesystem names** use a constrained charset. Media-sourced free text (stream titles) SHALL be consumed as-is through a single shared sanitize primitive — replacement, never rejection; the pipeline SHALL NOT fail a stream because of its title. Config-sourced names (profiles, presets) take the opposite path: validated safe at config load (Req 15.6), so they need no sanitization downstream.
3. A name SHALL be a pure function of the object's identity fields — never stored separately where it can drift.
4. `VideoStreamChunk` SHALL own the chunk id: the derived `chunk_id` property (timestamp-range naming unchanged) and a parse classmethod reconstructing the window; the owning phase supplies the stream on reconstruction.
5. `EncodedChunk` SHALL own the attempt filename (`<chunk_id>.<resolution>.q<crf>.mkv`): `file_name` generated from its composition, `parse_file_name` returning a typed name record (`chunk_id`, `resolution`, `crf`) that recovery joins against phase results; adjacent sidecar paths SHALL derive from the artifact name, not from independent format strings.
6. Strategy names SHALL be filesystem-safe by construction: profile and preset names SHALL be rejected at config load when they contain filesystem-unsafe characters (validation error naming the offender). The strategy directory and merge paths SHALL therefore embed the strategy name verbatim — no sanitization executes in any naming path; `Strategy.safe_name` and the remaining inline `:`→`_` mappings are removed with no replacement. A unit test SHALL pin every bundled `default_config.yaml` profile/preset name as safe.
7. Stream classes SHALL own their extracted-file names — `#<track_id> (type-codec) lang=…` built from info fields, titles via the shared sanitize. The legacy `#NN ID=N ` prefix collapses to `#N ` (N = track_id): the padded sort key was a directory-scan safeguard whose consumer sidecar-driven recovery removed. Recovery needs no parser there — `extraction.yaml` carries the resulting paths.
8. The merge output name (`<file stem> <strategy>.mkv`) SHALL be derived in one place from `File.path.stem` + the strategy name (safe by construction, Req 15.6); the future filename-template system (TODO §22) extends that single site.
9. Every parse-capable family SHALL have round-trip property tests: `parse(format(x)) == x` and `format(parse(s)) == s` — presence-plus-filename recovery is trustworthy only because these inverses are pinned.
