# TODO

- [ ] Two scene detection implementations coexist — consider consolidating:
  - `pyqenc/utils/ffmpeg.py` uses ffmpeg's built-in `scenedetect` filter
  - `pyqenc/phases/chunking.py` uses the `PySceneDetect` library (`scenedetect[opencv]`)
  - Decide which to keep and remove the other (or make it explicit why both exist).

- [ ] Evaluate whether `QualityEvaluation` (in-memory evaluation result) and `MetricsSidecar` (on-disk YAML) can be unified or if `QualityEvaluation` can be replaced by `MetricsSidecar` entirely.
  - See `.kiro/specs/phase-recovery-refactor/design.md` for context.
  - `QualityEvaluation` carries `ChunkQualityStats`, `failed_targets`, `QualityArtifacts`; `MetricsSidecar` is the lean flattened form. Determine if the rich structure is needed beyond the encoding loop.

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

- [ ] flag `  --keep-all            Keep all intermediate files (skip cleanup prompt after completion)` looks wrong. Replace with --clean for auto only? making the pipeline to purge all intermediate results (source is untouched + keeping the final results; everything else in workdir is purged; care, source could be there too).
