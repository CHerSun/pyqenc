# Tasks — Probe Phase Refactor

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-01

## Tasks

### Task 1 — Split VideoMetadata: remove frame_count lazy property

**Req:** 1

- [x] Remove `_frame_count: int | None` `PrivateAttr` from `VideoMetadata`
- [x] Remove `frame_count` `@property` from `VideoMetadata`
- [x] Remove `_probe_frame_count()` method from `VideoMetadata`
- [x] Remove `_probe_frame_count_async()` method from `VideoMetadata`
- [x] Remove `frame_count` serialization from `VideoMetadata.model_dump_full()`
- [x] Remove `frame_count` deserialization from `VideoMetadata.model_validate_full()`
- [x] Verify no remaining references to `video_meta.frame_count` or `video_meta._frame_count` on plain `VideoMetadata` instances (grep check)

---

### Task 2 — Add ExtendedVideoMetadata

**Req:** 2

- [x] Add `ExtendedVideoMetadata(VideoMetadata)` to `pyqenc/models.py` with `frame_count: int` field
- [x] Implement `ExtendedVideoMetadata.from_base(base, frame_count)` classmethod using `base.model_dump_full()` + `cls.model_validate_full()` — refactor-proof, no manual attr enumeration
- [x] Override `model_dump_full()` to include `frame_count`
- [x] Override `model_validate_full()` to restore `frame_count`
- [x] Add to public exports in `models.py` as needed

---

### Task 3 — Rebase ChunkMetadata on ExtendedVideoMetadata

**Req:** 3

- [x] Change `ChunkMetadata` to extend `ExtendedVideoMetadata` instead of `VideoMetadata`
- [x] Update `ChunkMetadata.model_validate_full()` to handle `frame_count`
- [x] Update ChunkingPhase `ChunkMetadata` construction: `frame_count=split_result.frame_count or 0`
- [x] Replace warning at chunking line 290 with check on `frame_count == 0`
- [x] Update chunking validation sum: `sum(c.frame_count for c in chunks)` (no `or 0` guard needed now)

---

### Task 4 — Add VideoMetadata.probe_extended() method

**Req:** 4

- [x] Add `probe_extended(self) -> ExtendedVideoMetadata` method to `VideoMetadata` in `pyqenc/models.py`
- [x] Defer `run_ffmpeg` import inside the method body (avoids circular import)
- [x] Log `info` before ffmpeg call: `"Counting source frames: {self.path.name}"`
- [x] Use `run_ffmpeg()` (sync) for the null-encode
- [x] Return `ExtendedVideoMetadata.from_base(self, frame_count=result.frame_count)` on success
- [x] Return `ExtendedVideoMetadata.from_base(self, frame_count=0)` with warning on failure
- [x] Confirm `utils/probe.py` is NOT created

---

### Task 5 — Add ProbeState sidecar model

**Req:** 5, 6

- [x] Add `ProbeState(BaseModel)` to `pyqenc/state.py` with `frame_count: int` and `crop: CropParams | None`
- [x] Implement `ProbeState.to_yaml_dict()`
- [x] Implement `ProbeState.from_yaml_dict()`
- [x] Implement `ProbeState.load(path) -> ProbeState | None`
- [x] Implement `ProbeState.save(path)` with `.tmp`-then-rename protocol

---

### Task 6 — Implement ProbePhase

**Req:** 5, 6, 7

- [x] Create `pyqenc/phases/probe.py`
- [x] Define `ProbePhaseResult(PhaseResult)` with `source: ExtendedVideoMetadata | None` and `crop: CropParams`
- [x] Implement `ProbePhase` class with `JobPhase` and `ExtractionPhase` dependencies
- [x] Implement `ProbePhase.scan()`: load `probe.yaml` → `COMPLETE`; absent → `ABSENT`
- [x] Implement `ProbePhase.run()`:
  - [x] Return `FAILED` when no video extracted; log at `error` level; set `source=None`, `crop=CropParams()`
  - [x] Skip if `probe.yaml` has both values and no explicit `--crop` (return `REUSED`)
  - [x] Resolve crop: manual → cached → `detect_crop_parameters()` on extracted video
  - [x] Resolve frame count: cached → `job_result.job.source.probe_extended()`
  - [x] Write `probe.yaml` via `.tmp`-then-rename
  - [x] Return `COMPLETED` with `ProbePhaseResult`
- [x] Add `ProbePhase` to public imports if needed

---

### Task 7 — Insert ProbePhase into _build_registry()

**Req:** 5, 8

- [x] Add `ProbePhase` import (deferred) in `phase.py`
- [x] Add `video_required: bool = True` parameter to `_build_registry()`
- [x] Insert `registry[ProbePhase]` between `ExtractionPhase` and `AudioPhase` in `_build_registry()` (only when registry is for a video-capable subcommand; `audio` subcommand does not include `ProbePhase`)
- [x] Forward `video_required` to `ExtractionPhase` constructor
- [x] Move `crop_params` kwarg from `JobPhase` constructor call to `ProbePhase` constructor call
- [x] Pass `video_required=False` from `process_audio()` / `audio` subcommand path in `api.py`
- [x] Update `_build_registry()` docstring to reflect new phase order and parameters

---

### Task 8 — Update JobPhase: remove crop and frame_count probe

**Req:** 7, 9, 11

- [x] Remove `crop_params` constructor parameter from `JobPhase.__init__()`
- [x] Remove `_resolve_crop()` method from `JobPhase`
- [x] Remove `crop` field from `JobPhaseResult`
- [x] Remove `_ = source.frame_count` line from `_create_or_update_job()`
- [x] Add `logger.info("Probing source metadata: %s", source.path.name)` before the probe block (first-run path only)
- [x] Add fast-field self-heal: after loading existing `JobState`, if `fps is None or fps <= 0`, re-run `_probe_metadata()` and save
- [x] Remove `crop` field from `JobState` in `state.py`
- [x] Update `JobState.to_yaml_dict()` to not emit `crop`
- [x] Update `JobState.from_yaml_dict()` to not read `crop` (silently ignore if present in old files)

---

### Task 9 — Update ExtractionPhase: video_required flag and optional video

**Req:** 8

- [x] Add `video_required: bool = True` constructor parameter to `ExtractionPhase`
- [x] Store as `self._video_required`
- [x] When `video_required=False`: skip video and timestamp extraction unconditionally; log at `debug` that video is skipped by pipeline mode
- [x] When `video_required=True` and `video_tracks` is empty: remove hard-error block; add `info` log `"No video tracks selected — skipping video extraction"`; continue with audio/subtitle/chapter/attachment extraction
- [x] Gate timestamp extraction on `video_tracks` being non-empty in both cases
- [x] Gate video stream extraction loop on `video_tracks` being non-empty in both cases
- [x] Verify audio/subtitle/chapter/attachment extraction continues normally in both no-video cases

---

### Task 10 — Update downstream phases: ProbeState-based invalidation

**Req:** 6

- [x] `state.py` — `OptimizationParams`: replace `crop: CropParams | None` with `probe: ProbeState | None`; update `to_yaml_dict()`, `from_yaml_dict()`, serialization
- [x] `state.py` — `EncodingParams`: replace `crop: CropParams | None` with `probe: ProbeState | None`; update serialization
- [x] `state.py` — `MergeParams`: add `probe: ProbeState | None`; update serialization
- [x] `OptimizationPhase`: add `ProbePhase` dependency; replace `getattr(job_result, "crop", None)` with `probe_result.crop`; replace crop mismatch check with `persisted.probe != current_probe`; update `_ensure_dependencies()` log for ProbePhase failure to use `warning` level
- [x] `EncodingPhase`: add `ProbePhase` dependency; replace crop source and mismatch check the same way; `warning`-level log for dependency cascade
- [x] `MergePhase`: add `ProbePhase` dependency; replace crop source; replace `source_video.frame_count` with `probe_result.source.frame_count if probe_result.source else 0`; add probe mismatch detection with warning log; `warning`-level log for dependency cascade
- [x] `PipelineOrchestrator`: add final summary log at `error` level when any phase returned `FAILED`

---

### Task 11 — Update CropParams: comma-separated format and all references

**Req:** 10

- [x] `models.py` — `CropParams.parse()`: split on `","` instead of whitespace
- [x] `models.py` — `CropParams.parse()` docstring: update format description, examples (`"140,140"`, `"140,140,0,0"`), and error message
- [x] `models.py` — `CropParams.__str__()`: use `f"{self.top},{self.bottom},{self.left},{self.right}"`
- [x] `cli.py` — `_add_crop_arguments()` help text: `'top bottom'` → `'top,bottom'`; `'0 0'` → `'0,0'`
- [x] `cli.py` — `_add_crop_arguments()` metavar: `PARAMS` → `CROP`
- [x] `cli.py` — epilog/example at line 945: `--crop "0 0"` → `--crop "0,0"`
- [x] `measure.py` lines 511, 522 — "Did you mean" suggestions: `{top} {bottom}` → `{top},{bottom}`
- [x] `docs/cli-reference.md` — all crop format references: `"top bottom"` → `"top,bottom"`, `"0 0"` → `"0,0"`
- [x] `docs/architecture.md` — update crop section: crop detection moves to ProbePhase (not Job phase); stored in `probe.yaml` (not `job.yaml`); update the architecture table and the "Automatic crop detection" section accordingly

---

### Task 12 — Update CLI: remove --crop from audio subcommand

**Req:** 6

- [x] Remove `_add_crop_arguments(p)` call from `_create_audio_subcommand()`
- [x] Remove `crop_params = _resolve_crop_params(args)` from `_cmd_audio()`
- [x] Remove `crop_params=crop_params` kwarg from `process_audio()` call in `_cmd_audio()`
- [x] Update `process_audio()` in `api.py` to remove `crop_params` parameter

---

### Task 13 — Clean up MeasurePhase frame_count references

**Req:** 12

- [x] Remove `await source_meta._probe_frame_count_async()` call from MeasurePhase
- [x] Remove `source_meta._frame_count` private attr access from MeasurePhase
- [x] Ensure the interval-mode fallback path (when frame_count is unavailable) is reached naturally
- [x] Verify MeasurePhase tests still pass with the fallback-only path

---

### Task 14 — Update tests

- [x] Update `test_job_phase.py`: remove crop-related assertions; update metadata probe expectations
- [x] Update `test_metrics_integration.py`: remove `detect_crop_parameters` patch; update to use `ProbePhase`
- [x] Update `test_pts_preservation_properties.py`: remove `job_result.crop` mock field
- [x] Update `test_merge_mkvmerge.py`: remove `job_result.crop` mock field; add `probe_result` mock
- [x] Add unit tests for `ExtendedVideoMetadata.from_base()`
- [x] Add unit tests for `ProbeState` load/save round-trip
- [x] Add unit tests for `CropParams.parse()` with comma format; verify old space format raises `ValueError`
- [x] Add unit tests for `ProbePhase.run()`: skip path (no video), cache hit path, full detection path

---

### Task 15 — Review spec against other specs and update cross-spec summaries

- [-] Check `phase-object-model` spec — JobPhase crop detection was a requirement there; note supersession
- [~] Check `pipeline-maturity-refactor` and `pipeline-correctness-refactor` specs for any crop/frame_count references
- [~] Add cross-spec summary table to top of this spec's `requirements.md` and `design.md`
- [~] Add a note to `phase-object-model` requirements about crop detection being superseded
- [~] Update `Completed:` date in both spec files

