# Requirements Document — Probe Phase Refactor

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-01
- Completed: 2026-07-11

## Cross-Spec Notes

### What this spec supersedes

| Superseded requirement | Original spec | What changed |
|---|---|---|
| `JobPhase` resolves crop parameters (manual → cached → auto-detect) | `phase-object-model` Req 2 AC 2, Glossary | Crop detection moved to **ProbePhase**; `JobPhase` no longer accepts `crop_params` or runs `_resolve_crop()` |
| Crop-params mismatch check in `OptimizationPhase` / `EncodingPhase` | `phase-object-model` Req 5 AC 6 | Mismatch now compares `ProbeState` snapshots (`persisted.probe != current_probe`); `CropParams` fields compared individually are gone |
| `VideoMetadata` holds `frame_count: int \| None` as a lazy property | `pipeline-maturity-refactor` Req 1 AC 1, Glossary | `frame_count` removed from `VideoMetadata` entirely; moved to **`ExtendedVideoMetadata`** as a required plain field; slow probe is now an explicit method call (`probe_extended()`) not a property |
| `CropParams` stored on `VideoMetadata.crop_params` and `PipelineState.source_video.crop_params` | `pipeline-correctness-refactor` Req 2 (all ACs) | Crop now lives in **`probe.yaml`** via `ProbeState`; `VideoMetadata` no longer carries crop; the orchestrator no longer reads crop from `tracker._state.source_video.crop_params` |

## Introduction

This spec covers a cluster of related changes to the job initialisation and extraction phases, motivated by the introduction of the audio-only pipeline path and a desire to eliminate hidden slow operations from the source-video probe.

The core problems:

1. **ExtractionPhase hard-requires video.** A source file with no video tracks (or a run with `--exclude` filtering out all video) fails immediately. The audio-only subcommand (`pyqenc audio`) is broken for any source where video would be filtered out.
2. **Crop detection lives in JobPhase**, which has no knowledge of whether any video will actually be extracted. This is conceptually wrong and forces the audio subcommand to accept `--crop` for no reason.
3. **Frame count probing is hidden and slow.** `VideoMetadata.frame_count` triggers `ffmpeg -c copy -f null` on demand — a silent operation that can take up to 15 minutes on a full UHD movie. This is the worst kind of hidden side-effect: a property access that blocks for minutes.
4. **Frame count and crop are only needed when video is processed.** Probing them for audio-only runs is wasteful and wrong.
5. **Crop CLI format is inconvenient.** Space-separated values require quoting or escaping in shells; comma-separated is more natural.
6. **Source metadata is missing a progress log.** When probing a new source for the first time, the user sees no indication that anything is happening.
7. **Job metadata reuse is fragile.** If `fps` or other fast-probe fields are absent or zero in an existing `job.yaml` (e.g. created by an older version), they are silently used as-is rather than re-probed.

The solution introduces a new **ProbePhase** between ExtractionPhase and ChunkingPhase. It owns frame count probing and crop detection — both slow, both video-only. It also refactors `VideoMetadata` to make the slow/fast boundary explicit in the type system.

## Glossary

- **ProbePhase**: The new pipeline phase that runs after extraction and owns: (a) source frame-count probing, (b) crop detection/resolution. Skipped entirely when no video was extracted.
- **VideoMetadata**: Existing class; after this spec holds only fast-probe fields (`path`, `file_size_bytes`, `duration_seconds`, `fps`, `fps_fraction`, `resolution`, `pix_fmt`). `frame_count` is removed as a lazy property.
- **ExtendedVideoMetadata**: New subclass of `VideoMetadata` that adds `frame_count: int` as a required plain field (not lazy). Instances are constructed only when `frame_count` is already known — either from a prior ProbePhase run or as a byproduct of an ffmpeg operation.
- **`probe_extended()`**: Method on `VideoMetadata` that runs the slow null-encode probe and returns an `ExtendedVideoMetadata`. Explicit method call (not a property) so it can never be triggered accidentally. The only path to constructing `ExtendedVideoMetadata` for a source file — clearly costly by name and call style.
- **`probe.yaml`**: New sidecar written by ProbePhase. Contains only the delta over `job.yaml`: `frame_count` and `crop`. Does not duplicate fast-probe fields.
- **Fast-probe fields**: Fields populated by `ffprobe -show_streams -show_format` (~175 ms): `duration_seconds`, `fps`, `fps_fraction`, `resolution`, `pix_fmt`, `file_size_bytes`.
- **Slow-probe field**: `frame_count`, populated by `ffmpeg -c copy -f null` (seconds to ~15 minutes on large UHD sources).

---

## Requirements

### Requirement 1 — VideoMetadata split: remove hidden slow property

**User Story:** As a developer, I want it to be impossible to accidentally trigger a 15-minute probe by accessing a property, so that slow operations are always explicit and visible in calling code.

#### Acceptance Criteria

1. THE `VideoMetadata` class SHALL remove `frame_count` as a lazy `@property`.
2. THE `VideoMetadata` class SHALL remove `_frame_count: int | None` as a `PrivateAttr`.
3. THE `VideoMetadata` class SHALL remove `_probe_frame_count()` and `_probe_frame_count_async()` methods.
4. THE `VideoMetadata.model_dump_full()` SHALL NOT serialize `frame_count`.
5. THE `VideoMetadata.model_validate_full()` SHALL NOT attempt to restore `frame_count`.
6. ALL existing code that accesses `video_meta.frame_count` or `video_meta._frame_count` SHALL be updated to use `ExtendedVideoMetadata.frame_count` instead, or removed if no longer needed.

---

### Requirement 2 — ExtendedVideoMetadata: explicit contract for slow data

**User Story:** As a developer, I want the type system to express whether frame count has been probed, so that phases that need it receive a guarantee rather than a nullable value.

#### Acceptance Criteria

1. THE system SHALL define `ExtendedVideoMetadata(VideoMetadata)` in `pyqenc/models.py` with one additional field: `frame_count: int`.
2. `frame_count` SHALL be a plain required Pydantic field — not a `PrivateAttr`, not a property, not optional.
3. THE system SHALL provide a classmethod `ExtendedVideoMetadata.from_base(base: VideoMetadata, frame_count: int) -> ExtendedVideoMetadata` that transfers all cached state from `base` via `base.model_dump_full()` + `cls.model_validate_full()`, so the method is automatically correct when `VideoMetadata` gains new fields.
4. `ExtendedVideoMetadata.model_dump_full()` SHALL include `frame_count` in its output.
5. `ExtendedVideoMetadata.model_validate_full()` SHALL restore `frame_count` from the dict.
6. WHEN `frame_count` cannot be determined, callers SHALL use `0` and log a warning; `0` is an unambiguous sentinel (no valid video has zero frames).

---

### Requirement 3 — ChunkMetadata uses ExtendedVideoMetadata

**User Story:** As a developer, I want chunk metadata to always carry a frame count so that chunking validation can sum frame counts without nullable checks.

#### Acceptance Criteria

1. `ChunkMetadata` SHALL extend `ExtendedVideoMetadata` instead of `VideoMetadata`.
2. WHEN `run_ffmpeg()` returns a `frame_count` from its progress output, ChunkingPhase SHALL pass it to `ChunkMetadata` construction via `frame_count=split_result.frame_count or 0`.
3. WHEN `run_ffmpeg()` does not return a frame count (unexpected), ChunkingPhase SHALL use `frame_count=0` and log a warning.
4. THE chunking validation that sums chunk frame counts SHALL continue to work without change — `frame_count` is now always an `int` (never `None`), so `sum(c.frame_count for c in chunks)` is valid without guards.

---

### Requirement 4 — `VideoMetadata.probe_extended()` method

**User Story:** As a developer, I want a single, clearly named method on `VideoMetadata` that performs the slow null-encode probe, so that the cost is visible at every call site and the operation is owned by the class that holds the data.

#### Acceptance Criteria

1. THE system SHALL add `probe_extended(self) -> ExtendedVideoMetadata` as a method on `VideoMetadata` in `pyqenc/models.py`.
2. THE method SHALL run `ffmpeg -i {self.path} -map 0:v:0 -c copy -f null -` via `run_ffmpeg()` (sync, not async — ProbePhase runs in a sync context). The import of `run_ffmpeg` SHALL be deferred inside the method body to avoid a circular import.
3. BEFORE running ffmpeg, THE method SHALL log at `info` level: `"Counting source frames: {self.path.name}"`.
4. WHEN ffmpeg succeeds and reports a frame count, THE method SHALL return `ExtendedVideoMetadata.from_base(self, frame_count=result.frame_count)`.
5. WHEN ffmpeg fails or reports no frame count, THE method SHALL log a warning and return `ExtendedVideoMetadata.from_base(self, frame_count=0)`.
6. THE method SHALL NOT be called from any context with a running event loop (enforced by `run_ffmpeg()`'s existing guard).
7. THE `utils/probe.py` file SHALL NOT be created — there is no free function.

---

### Requirement 5 — New ProbePhase

**User Story:** As a developer, I want a dedicated phase between Extraction and Chunking that owns frame-count probing and crop detection, so that these slow video-only operations run at the right point in the pipeline and are skipped for audio-only runs.

#### Acceptance Criteria

1. THE system SHALL implement `ProbePhase` in `pyqenc/phases/probe.py` as a `Phase` object.
2. `ProbePhase` SHALL declare dependencies: `JobPhase` and `ExtractionPhase`.
3. WHEN `ExtractionPhase.result` contains no video artifacts (empty `video` field), `ProbePhase.run()` SHALL return `FAILED` with error `"No video tracks extracted — video processing cannot continue"` and log at `error` level. `ProbePhaseResult.source` SHALL be `None` and `ProbePhaseResult.crop` SHALL be `CropParams()`.
4. WHEN `ExtractionPhase.result` contains video, `ProbePhase.run()` SHALL resolve crop and frame count as described in Requirements 6 and 7.
4a. BECAUSE `ProbePhase` returns `FAILED` when there is no video, all downstream phases that declare `ProbePhase` as a dependency (`ChunkingPhase`, `OptimizationPhase`, `EncodingPhase`, `MergePhase`) SHALL propagate `FAILED` via their existing `_ensure_dependencies()` mechanism — each SHALL log at `warning` level that it is skipping because a dependency did not complete.
4b. `AudioPhase` SHALL NOT declare `ProbePhase` as a dependency and SHALL continue to run and succeed independently of `ProbePhase`'s outcome.
4c. WHEN running in `auto` pipeline mode and one or more phases failed, the orchestrator SHALL log a final summary at `error` level indicating the run completed with errors.
5. `ProbePhase` SHALL write `probe.yaml` containing only `{ frame_count: int, crop: { top, bottom, left, right } }` after a successful probe run; it SHALL use the `.tmp`-then-rename protocol.
6. WHEN `probe.yaml` already exists and contains both `frame_count` and `crop`, `ProbePhase.run()` SHALL skip re-probing and return `REUSED` — except when `--crop` was passed explicitly (crop override always re-saves).
7. `ProbePhase.result` SHALL be typed as `ProbePhaseResult(PhaseResult)` with fields: `source: ExtendedVideoMetadata | None`, `crop: CropParams`.
8. `ProbePhase` SHALL be inserted in `_build_registry()` between `ExtractionPhase` and `ChunkingPhase` in the ordered registry dict.
9. `ProbePhase.scan()` SHALL load `probe.yaml` if present and return a `COMPLETE` result; return `ABSENT` if missing; never run ffmpeg or crop detection.

---

### Requirement 6 — Crop resolution moves to ProbePhase

**User Story:** As a developer, I want crop detection to run after extraction so it knows whether video was extracted, and to have its own storage separate from job.yaml.

#### Acceptance Criteria

1. THE `JobPhase` SHALL remove `_resolve_crop()`, the `crop_params` constructor argument, and the `crop` field from `JobPhaseResult`.
2. THE `JobState` (serialised to `job.yaml`) SHALL remove the `crop` field.
3. `ProbePhase` SHALL resolve crop using the priority order: manual `--crop` from CLI → cached from `probe.yaml` → auto-detect via `detect_crop_parameters()`.
4. WHEN auto-detecting, `detect_crop_parameters()` SHALL operate on the extracted video file (from `ExtractionPhaseResult.video`) rather than the source file.
5. `ProbePhase` SHALL store the resolved `CropParams` in `probe.yaml` after detection so subsequent runs skip detection.
6. `OptimizationPhase`, `EncodingPhase`, and `MergePhase` SHALL read crop from `probe_result.crop` (via `ProbePhase` dependency) instead of `job_result.crop`.
7. THE CLI `audio` subcommand SHALL remove `--crop` from its argument list — crop is irrelevant to audio-only runs.
8. ALL other subcommands that trigger video processing SHALL retain `--crop` and pass it through to `_build_registry()` as before, now forwarded to `ProbePhase` instead of `JobPhase`.
9. WHEN `probe.yaml` is absent (first run) and no explicit `--crop` was given, ProbePhase SHALL log `"Detecting crop: {source_video.name}"` before running detection.
10. `OptimizationParams.crop: CropParams | None` SHALL be replaced with `OptimizationParams.probe: ProbeState | None`; the mismatch check SHALL compare `persisted.probe != current_probe` rather than comparing crop fields individually.
11. `EncodingParams.crop: CropParams | None` SHALL be replaced with `EncodingParams.probe: ProbeState | None`; the mismatch check SHALL compare `persisted.probe != current_probe`.
12. `MergeParams` SHALL add `probe: ProbeState | None` (previously absent — closes the pre-existing crop invalidation gap); MergePhase SHALL detect probe mismatch and log a warning when `persisted.probe != current_probe`.

---

### Requirement 7 — Frame count moves to ProbePhase

**User Story:** As a developer, I want source frame-count probing to happen in ProbePhase so it is skipped for audio-only runs and the user sees a clear log message during the slow operation.

#### Acceptance Criteria

1. `JobPhase._create_or_update_job()` SHALL remove the eager `_ = source.frame_count` probe call.
2. `JobPhase` SHALL probe and persist only fast-probe fields: `file_size_bytes`, `duration_seconds`, `fps`, `fps_fraction`, `resolution`, `pix_fmt`.
3. `ProbePhase` SHALL call `probe_extended(job_result.job.source)` to obtain source frame count when it is not cached in `probe.yaml`.
4. `ProbePhase` SHALL store the result as `ProbePhaseResult.source: ExtendedVideoMetadata`.
5. `MergePhase` SHALL read `source_frame_count` from `probe_result.source.frame_count` instead of `job_result.job.source.frame_count`.
6. `MergePhase` SHALL declare `ProbePhase` as a dependency (in addition to its current dependencies).

---

### Requirement 8 — Optional video in ExtractionPhase

**User Story:** As a developer, I want ExtractionPhase to succeed even when no video tracks are present, so that the audio-only pipeline path works for sources with no video or when video is filtered out.

#### Acceptance Criteria

1. `ExtractionPhase._execute_extraction()` SHALL remove the hard error on empty `video_tracks`.
2. WHEN `video_tracks` is empty, ExtractionPhase SHALL skip: timestamp extraction, video stream extraction. It SHALL continue with audio, subtitle, chapter, and attachment extraction as normal.
3. WHEN `video_tracks` is empty, ExtractionPhase SHALL log at `info` level: `"No video tracks selected — skipping video extraction"`.
4. `ExtractionPhaseResult.video` SHALL be `None` when no video was extracted (existing field, behaviour unchanged for this case).
5. Downstream phases that require video (`ChunkingPhase`, `EncodingPhase`, `MergePhase`) will receive `FAILED` naturally via the `ProbePhase` dependency cascade (Requirement 5.4a) — no additional changes needed in those phases beyond declaring `ProbePhase` as a dependency.

---

### Requirement 9 — Job metadata re-probe on stale/missing fast fields

**User Story:** As a developer, I want job.yaml to self-heal if fast-probe fields are missing or invalid (e.g. from an older version that didn't serialize fps_fraction), so re-running is sufficient to fix stale state.

#### Acceptance Criteria

1. AFTER loading an existing `JobState` from `job.yaml`, `JobPhase._create_or_update_job()` SHALL check whether any fast-probe field is `None` or invalid (specifically: `fps is None or fps <= 0`).
2. WHEN any fast-probe field is invalid, `JobPhase` SHALL run `_probe_metadata()` on the source `VideoMetadata` instance, log at `info` level `"Re-probing source metadata (stale or incomplete cached fields)"`, and save the updated `job.yaml` before returning.
3. THIS re-probe SHALL run only `_probe_metadata()` (fast, ~175 ms) — never the slow null-encode.

---

### Requirement 10 — Comma-separated crop CLI format

**User Story:** As a user, I want to specify crop without needing to quote or escape the value, so that `--crop 140,140` works directly in any shell.

#### Acceptance Criteria

1. `CropParams.parse()` SHALL accept comma-separated values: `"140,140"` (2-value) and `"140,140,0,0"` (4-value).
2. THE old space-separated format SHALL be removed — no dual-format support, clean break (pre-alpha, no compatibility obligation).
3. THE `--crop` help text SHALL be updated to show the new format: `'top,bottom' or 'top,bottom,left,right'`. The no-crop sentinel example SHALL be updated to `'0,0'`.
4. THE `--crop` metavar SHALL be updated from `PARAMS` to `CROP`.
5. `CropParams.__str__()` SHALL be updated to produce comma-separated output to stay consistent with the parse format.
6. THE "Did you mean: --crop" suggestion messages in `MeasurePhase` SHALL be updated to emit comma-separated format.
7. ALL crop format references in `docs/cli-reference.md` SHALL be updated to comma-separated format.
8. `docs/architecture.md` SHALL be updated to reflect that crop detection runs in ProbePhase (not JobPhase) and is stored in `probe.yaml` (not `job.yaml`).

---

### Requirement 11 — Source probe info log

**User Story:** As a user, I want to see a log message when source metadata is being probed for the first time, so I know what is happening during any pause.

#### Acceptance Criteria

1. WHEN `JobPhase._create_or_update_job()` creates a new `JobState` (no existing `job.yaml`), it SHALL log at `info` level before probing: `"Probing source metadata: {source.path.name}"`.
2. WHEN `probe_extended()` begins the slow null-encode, it SHALL log at `info` level: `"Counting source frames: {source.path.name}"` (covered by Requirement 4.3, listed here for completeness).
3. WHEN `ProbePhase` begins crop detection, it SHALL log at `info` level: `"Detecting crop: {source.path.name}"` (covered by Requirement 6.9, listed here for completeness).

---

### Requirement 12 — MeasurePhase frame_count cleanup

**User Story:** As a developer, I want MeasurePhase to stop probing frame_count via the removed lazy property, so it uses only the remaining supported paths.

#### Acceptance Criteria

1. `MeasurePhase` SHALL remove the `_probe_frame_count_async()` call and all references to `source_meta._frame_count` via the private attr.
2. `MeasurePhase` SHALL continue to operate without frame count — the existing fallback path (interval mode when frame count is unavailable) is already correct.
3. WHEN `MeasurePhase` receives an `ExtendedVideoMetadata` instance as source (possible if called via a future pipeline integration), it MAY use `source_meta.frame_count` directly for the count-based screenshot distribution. This is a nice-to-have, not required now.
