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
