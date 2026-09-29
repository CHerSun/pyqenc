# Spec: File → Stream → Chunk object model + direct-from-source ffmpeg (rev 2)

Session deliverable: **spec docs only** (`.kiro/specs/2026-09-24 file-stream-model/` + TODO.md prune). Implementation is a staged follow-up after spec review.

## 1. Direct input replaces extracted streams and chunk files — YES

- **Encoding/optimization**: `-ss <start> -i <source> -map 0:v:<track_id> -t <dur> <codec args> <out>` — the exact seek pattern `split_chunks` uses today (chunking.py:256-263). Frame-perfection comes from accurate-seek decode-discard, not from FFV1 all-intra intermediates.
- **Quality**: both inputs windowed with per-input `-ss`/`-t` (per-input `-t` pattern already exists, quality.py:440-447). Reference = source window (same frames as the FFV1 chunk, lossless either way).
- **Cropdetect / frame-count / scene-detect**: source + explicit `-map 0:v:<track_id>`.
- **Still on disk**: `timestamps.txt`, chapters/subtitles/attachments, encoding attempts, audio chain outputs, finals.
- **Disk**: extracted video+audio copies (≈1.1×) and FFV1 `chunks/` tree (≈4–6× source) eliminated. Peak ≈6–9× → ≈1.5–2.5× source.

## 2. Class structure — composition, unique-property dumps, eager, single instantiation

```
File                       owner: JobPhase            dump → job.yaml
  path: LongPath
  file_size_bytes: int | None      (path+size = invalidation identity)

VideoStream                owner: ExtractionPhase (fast facet)
  file: File               [composed — never dumped]
  info: VideoStreamInfo    dump → extraction.yaml streams.video
    track_id, codec_name,
    start_timestamp, duration_seconds,          (stream placement in container)
    fps, fps_fraction, resolution, pix_fmt      (video-stream properties)

ExtendedVideoStream        owner: ProbePhase (slow facet)
  stream: VideoStream      [composed — never dumped]
  frame_count: int         dump → probe.yaml (ProbeState, schema unchanged)
  crop: CropParams | None
  → every downstream video phase (chunking/optimization/encoding/merge/measure)
    accepts ExtendedVideoStream; audio-only mode never builds it

VideoStreamChunk           owner: ChunkingPhase (derived; no per-chunk files)
  stream: ExtendedVideoStream  [composed — never dumped]
  start_timestamp, end_timestamp
  chunk_id: str (derived from ts range — naming unchanged, recovery keys survive)
  frame_count: int | None  (from timestamps.txt window count; invariant only)
  [NO start_frame/end_frame — positioning is timestamp-based]
                           dump → chunking.yaml scenes [{timestamp_seconds, frame?}]
                             (detector's frame number informational only)

AudioStream                owner: ExtractionPhase
  file: File + info: AudioStreamInfo
    (track_id, codec, layout, language, title, duration_seconds, start_timestamp)
                           dump → extraction.yaml streams.audio[]

SubtitleStream / AttachmentStream / ChaptersStream
  file + info              dump → extraction.yaml streams.* (+ extracted paths)

EncodedChunk (attempt)     owner: EncodingPhase
  chunk: VideoStreamChunk  [composed]
  strategy: Strategy       [object — no strategy strings in-memory]
  crf: Decimal, path: Path, frame_count: int | None
                           dump → encoding result sidecars (unchanged, filename-keyed)
```

Rules:
- **File slim-down consequences**: the only consumers of file-level fast-video fields were JobPhase disk estimation and `_find_source_mismatches` (job.py:339). Estimation moves to ExtractionPhase (first owner of stream data — real fps/res/duration instead of heuristics; partially resolves TODO §33, entry kept open for the log-only/blocking question). Mismatch check becomes path + file_size_bytes. job.yaml = `{source: {path, file_size_bytes}}`; tolerant load of old files.
- **Frame-exactness → preservation invariant** (not positional math):
  `source.frame_count == Σ chunk.frame_count == attempt.frame_count == final.frame_count`, with counts sourced free:
  - source count: `timestamps.txt` line count (exact per-frame PTS list; fallback: null-count pass)
  - chunk counts: `timestamps.txt` window counts `[start, end)`
  - attempt counts: `FFmpegRunResult.frame_count` read at the call site after each encode run (`progress=end` is already parsed) — **no second ffmpeg run**; the `video_meta=` in-place parameter dies with the lazy models, its frame-count purpose survives via the result object
  - final count: merge's existing null-count verification
  Plots/graphs switch to timestamp x-axes (fps_fraction conversion when a log is frame-indexed).
- **Eager, no lazy props** (TODO §26): fields are plain Optionals filled by the owning phase; None where the contract requires a value = explicit error. `VideoMetadata`/`ExtendedVideoMetadata`/`ChunkMetadata`, `StreamBase`/`StreamFactory`/`MKVTrackExtractor`, `populate_from_ffmpeg_output` plumbing die.
- **Single instantiation per run**: File @ Job, streams @ Extraction, extended @ Probe, chunks @ Chunking, attempts @ Encoding; downstream reuses via `_dep()` results. One sanctioned second instance: throwaway probes inside validation logic.
- **Sidecar ownership preserved** (one phase = one sidecar): job.yaml (File), extraction.yaml (NEW: full stream inventory + source path/size identity; reuse-runs load it — no re-probe), probe.yaml (slow facet), chunking.yaml (scenes; `chunking_mode` field deleted), audio/optimization/encoding/merge sidecars unchanged in role.

## 3. FFmpeg runner rework (TODO §28) — sync + async kept

```python
@dataclass(frozen=True)
class FFmpegInput:
    path:             LongPath
    selector:         str | None        # "0:<track_id>" → -map, in input order
    start_seconds:    float | None      # input -ss (before -i)
    duration_seconds: float | None      # input -t (before -i)
    pre_input_args:   tuple[str, ...]   # hwaccel / -init_hw_device / vulkan

@dataclass(frozen=True)
class FFmpegRequest:
    inputs:         list[FFmpegInput]   # 1 for encode/extract/probe, 2 for quality
    output_args:    tuple[str, ...]     # codec / -vf output stage
    filter_complex: str | None
    output:         LongPath | None     # None → "-f null -"
    output_format:  str | None          # .tmp muxer override (flac/ipod/...)

run_ffmpeg(request, progress_callback=None, cwd=None) -> FFmpegRunResult
run_ffmpeg_async(...)                   # async core; sync wrapper contract unchanged
```

Runner keeps: progress-flag injection + stdout parsing, `.tmp`-then-rename + explicit muxer, stderr collection, kill registry. Adapters: `VideoStream.as_input()`, `VideoStreamChunk.as_input()` (+window), `AudioStream.as_input()`.

**Codec config split** (default_config.yaml): each codec gains `pre_input_args: []` (empty default defined there — single source of truth) and `encoder_args` keeps post-input tokens with `"-i", "{input}"` removed. `Strategy` exposes both stages; `{vf}`/`{quality}`/`{preset}`/`{profile_args}` substitution unchanged. Golden command tests pin composed argv for every converted call site.

## 4. `setpts=PTS-STARTPTS` decision

Kept, **quality filter graph only**. psnr/ssim/libvmaf pair frames by timestamp; input-side `-ss` resets output ts to ~0 only approximately (sub-frame rounding; container start-time quirks), so the flag is the explicit co-basing of both metric inputs — a no-op when both start at 0. It never touches encode outputs (natural 0-based PTS) or merge (global PTS restored via `mkvmerge --timestamps` from timestamps.txt, as today). Spec includes a one-time e2e task: compare metrics with/without on real media; drop the flag later if provably redundant.

## 5. Phase-by-phase

| Phase | Change |
|---|---|
| CLI/api | `source` parsed as LongPath (today plain Path) |
| Job | eager File (path+size); mismatch = path+size; estimation moves out |
| Extraction | ffprobe enumeration → typed streams persisted to extraction.yaml; extracts only timestamps/chapters/subs/attachments (no video/audio copies); runs disk-space estimation with real stream data |
| Probe | frame_count primary from timestamps.txt (fallback null-count); cropdetect on source window; emits ExtendedVideoStream |
| Audio | chains consume `AudioStream.as_input()` — no `.mka` intermediates; audio.yaml signatures unchanged |
| Chunking | scene-detect on source (warn if >1 video stream); boundaries → timestamp-positioned chunks only. DELETE: FFV1/REMUX machinery, `ChunkingMode`, `FFV1_VIDEO_ARGS`, per-chunk sidecars, `ChunkArtifact`, `chunks/` |
| Optimization | logic unchanged; test-encodes on chunk windows |
| Encoding | attempts = source-window encodes; quality reference = windowed source; EncodedChunk composition (chunk + Strategy + crf + free frame_count); pipeline-revision marker in encoding.yaml invalidates old-pipeline attempts |
| Merge | mechanics unchanged (mkvmerge append + timestamps.txt); consumes EncodedChunk/Strategy objects |
| measure | adopts File/VideoStream loaders |
| disk_space/cleanup | estimators reworked for new footprint; INTERMEDIATE = encoding workspace, ALL = + encoded/ |

## 6. Risks

- Per-attempt re-seek: cue-point seek + decode-discard bounded by GOP; encode time dominates.
- Multi-video-stream sources: validate + warn at extraction (PyAV picks v:0).
- VFR: timestamp windows identical to today's split semantics; PTS restoration unchanged.
- timestamps.txt-based counts depend on complete per-frame extraction (mkvextract primary, ffprobe packets fallback) — fallback null-count pass if the file is absent.
- Old work dirs: revision marker forces one-time re-encode.

## 7. Session steps (docs only, no code)

1. Write `.kiro/specs/2026-09-24 file-stream-model/` per agent-specs.md: `requirements.md`, `design.md` (Mermaid class diagram + pipeline flow, sidecar map, runner request spec, phase contracts, invariant chain, config/CLI changes, migration/invalidation, risks, the setpts e2e verification task), `tasks.md` (staged implementation: runner-first mechanical conversion → model family → job/extraction → probe/chunking → encoding/optimization/quality → audio/merge/measure → dead-code sweep + ruff + full pytest + e2e smoke on real media; each stage green).
2. Prune TODO.md outright (covered by the spec, per its preamble): §25 (strategy objects), §26 (File→Stream→fragment, eager), §28 (ffmpeg command class), §29 (frame-count dedup — `get_frame_count` becomes the single null-count path, used only for final-merge verify and fallback).
3. Annotate §33 (estimation moves to ExtractionPhase with real data; log-only/blocking question stays open); all other entries untouched.