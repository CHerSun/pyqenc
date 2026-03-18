# TODO

- [ ] Two scene detection implementations coexist — consider consolidating:
  - `pyqenc/utils/ffmpeg.py` uses ffmpeg's built-in `scenedetect` filter
  - `pyqenc/phases/chunking.py` uses the `PySceneDetect` library (`scenedetect[opencv]`)
  - Decide which to keep and remove the other (or make it explicit why both exist).

- [ ] Evaluate whether `QualityEvaluation` (in-memory evaluation result) and `MetricsSidecar` (on-disk YAML) can be unified or if `QualityEvaluation` can be replaced by `MetricsSidecar` entirely.
  - See `.kiro/specs/phase-recovery-refactor/design.md` for context.
  - `QualityEvaluation` carries `ChunkQualityStats`, `failed_targets`, `QualityArtifacts`; `MetricsSidecar` is the lean flattened form. Determine if the rich structure is needed beyond the encoding loop.

- [ ] Normalize metric values consistently across the codebase.
  - SSIM is currently stored as a raw float (0.0–1.0) in some places and as a percentage (0–100) in others.
  - Decide on a single canonical representation for all metrics (VMAF, SSIM, PSNR) in sidecars, logs, and the CRF adjustment algorithm.
  - Update `normalize_metric` / `normalize_metric_deficit` in `pyqenc/quality.py` and all sidecar writers accordingly.

- [ ] Review whether `ChunkMetadata.chunk_id` is still needed after the phase recovery refactor.
  - `chunk_id` is the file stem and is derived from the filename at recovery time.
  - Evaluate whether storing it redundantly in `ChunkMetadata` creates multiple sources of truth.
  - If removed, update all callers that reference `chunk.chunk_id` to derive it from `chunk.path.stem` instead.

- [ ] Make a "recurring" spec to actualize docs
  - Architectural diagrams in C4 paradigm of levels 1...3.
  - Key project concepts
  - General flow

- [ ] Make a "recurring" spec to discover possible optimizations:
  - Collect a table of all classes / functions / methods.
  - Assign a public / internal / private flag to each
  - Add number of usages
  - Add a 1 sentence summary of what it is doing
  - Populate "similarity" between collected things
  - Draw a used by diagram.
  - Analyze if something looks like a duplicate or legacy

- [ ] Check for stale code
- [ ] Make all tests pass
- [ ] optimization phase tasks selection rework. Split into bins - either by duration or by mid-timestamp. Number of bins == number of chunks to select. From each bin select 1 task randomly. Limit max duration and prefilter chunks that do not match immediately. If picked still too large chunks - remove largest and re-random.
- [ ] review specs to mark what was outdated already (for future human reference) (task for agent).

- [ ] Reused artifact not reflected as success on the encoding progress bar — reused chunks don't advance the bar, making it look stalled/incomplete (e.g. bar stops at 76% despite all chunks being done). Also duplicate logging: both "Encoding complete: X newly encoded, Y reused" and "✔ encoding: Encoded X chunks, reused Y" are emitted.
-
 [ ] Apply the `✔ {success}  ✘ {failed}  ⏭ {skipped}` summary text pattern to the duration-based progress bars used in other phases (encoding, chunking), replacing per-task text updates with a running counter for consistency.

- [ ] flag `  --keep-all            Keep all intermediate files (skip cleanup prompt after completion)` looks wrong. Replace with --clean for auto only? making the pipeline to purge all intermediate results (source is untouched + keeping the final results; everything else in workdir is purged; care, source could be there too).
