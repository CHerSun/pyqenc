# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-03-15
- Completed: 2026-03-15

## Introduction

This feature addresses a set of user-experience and correctness improvements to the pyqenc encoding pipeline. The improvements span four areas:

1. **Crop detection correctness** — crop detection is currently run per-chunk attempt, but should be detected once on the extracted video stream and applied uniformly to all chunk attempts in both optimization and encoding phases.
2. **Chunk attempt log formatting** — log messages during chunk encoding and optimization are inconsistent; they should follow a uniform `{strategy}/{chunk} attempt {attempt}: <message>` pattern, with a distinct final chunk result message prefixed with ✔.
3. **Optimization phase log summaries** — each strategy result and the final optimization summary should be visually distinctive and easy to scan.
4. **alive_progress progress bars** — end-user progress tracking is missing for the chunking, optimization, and encoding phases; progress bars using `alive_progress` should be added.

## Glossary

- **Chunk**: A short video segment produced by splitting the source video at scene boundaries.
- **Strategy**: A named combination of encoder preset and quality profile (e.g. `slow+h265-aq`).
- **CRF**: Constant Rate Factor — the quality parameter passed to the video encoder; lower = higher quality.
- **Optimization phase**: The pipeline phase that tests a representative subset of chunks with all strategies to select the one producing the smallest file at target quality.
- **Encoding phase**: The pipeline phase that encodes all chunks using the selected (or configured) strategy.
- **Crop detection**: The process of detecting black borders in the video stream so they can be cropped during encoding.
- **alive_progress**: The approved Python progress-bar library used in this project.
- **ProgressTracker**: The `pyqenc.progress.ProgressTracker` class that persists pipeline state to disk.
- **ChunkEncoder**: The `pyqenc.phases.encoding.ChunkEncoder` class responsible for iterative CRF-based chunk encoding.
- **Extraction phase**: The pipeline phase that extracts raw video and audio streams from the source MKV.

---

## Requirements

### Requirement 1 — Crop Detection Runs Once Per Stream

**User Story:** As a pipeline operator, I want crop detection to run once on the extracted video stream and be reused for all chunk attempts, so that encoding time is not wasted re-detecting crops and all chunks are cropped consistently.

#### Acceptance Criteria

1. WHEN the extraction phase completes, THE Pipeline SHALL detect crop parameters from the extracted video stream and persist them in `PhaseMetadata.crop_params`.
2. WHEN the optimization phase encodes a chunk attempt, THE ChunkEncoder SHALL apply the crop parameters persisted from the extraction phase without re-running crop detection.
3. WHEN the encoding phase encodes a chunk attempt, THE ChunkEncoder SHALL apply the crop parameters persisted from the extraction phase without re-running crop detection.
4. IF crop parameters are not present in the extraction phase metadata, THEN THE Pipeline SHALL proceed without cropping and log a warning at INFO level.
5. WHILE the optimization phase is running, THE Pipeline SHALL apply the same crop parameters to every chunk attempt uniformly.

---

### Requirement 2 — Uniform Chunk Attempt Log Formatting

**User Story:** As a developer debugging the pipeline, I want all chunk-level log messages to follow a consistent format, so that I can quickly identify which strategy, chunk, and attempt each message belongs to.

#### Acceptance Criteria

1. WHEN a chunk encoding attempt begins, THE ChunkEncoder SHALL emit an INFO log in the format `{strategy}/{chunk} attempt {attempt}: starting with CRF {crf}`.
2. WHEN quality metrics are evaluated for a chunk attempt, THE ChunkEncoder SHALL emit an INFO log in the format `{strategy}/{chunk} attempt {attempt}: Metrics: {metric_summary}`.
3. WHEN a chunk attempt produces a new best CRF, THE ChunkEncoder SHALL emit an INFO log in the format `{strategy}/{chunk} attempt {attempt}: new best CRF {crf}`.
4. WHEN a chunk encoding reaches its final result (optimal CRF found or search exhausted), THE ChunkEncoder SHALL emit an INFO log prefixed with `✔` in the format `✔ {strategy}/{chunk}: final CRF {crf} after {n} attempts`.
5. THE ChunkEncoder SHALL apply the same log format for chunk attempts in both the optimization phase and the encoding phase.

---

### Requirement 3 — Distinctive Optimization Phase Log Summaries

**User Story:** As a pipeline operator, I want each strategy result and the final optimization summary to stand out visually in the log output, so that I can quickly locate and interpret optimization outcomes.

#### Acceptance Criteria

1. WHEN the optimization phase completes testing a strategy, THE Pipeline SHALL emit a visually distinctive INFO log block that includes the strategy name, average CRF, and total size of optimal chunks.
2. WHEN the optimization phase selects the optimal strategy, THE Pipeline SHALL emit a visually distinctive final summary INFO log that includes the winning strategy name, its average CRF, and its total size.
3. THE Pipeline SHALL separate each strategy result block from surrounding log lines using a consistent visual delimiter (e.g. a line of `─` characters or `=` characters).
4. WHEN multiple strategies are tested, THE Pipeline SHALL include a comparison table or list in the final summary showing all strategies with their average CRF and total size.

---

### Requirement 5 — Parallel Workers in Optimization Phase

**User Story:** As a pipeline operator, I want the optimization phase to encode test chunks in parallel using the same worker pool as the encoding phase, so that optimization time scales with available CPU resources.

#### Acceptance Criteria

1. WHEN the optimization phase encodes test chunks, THE Pipeline SHALL use a configurable number of parallel workers with a default of 2.
2. THE Pipeline SHALL reuse the `max_parallel` configuration value (already used by the encoding phase) to control the number of optimization workers.
3. WHEN `max_parallel` is set via CLI argument, THE Pipeline SHALL apply the same value to both the optimization and encoding phases.
4. WHILE the optimization phase is running in parallel, THE Pipeline SHALL ensure that all test chunks for a given strategy complete before that strategy's result block is logged.

---

### Requirement 4 — alive_progress Progress Bars for Chunking, Optimization, and Encoding

**User Story:** As an end user running the pipeline, I want to see live progress bars for the chunking, optimization, and encoding phases, so that I can monitor pipeline progress without reading raw log output.

#### Acceptance Criteria

1. WHEN the chunking phase splits chunks, THE Pipeline SHALL display an `alive_progress` bar that increments by one for each successfully split chunk, with a total equal to the number of detected scene boundaries.
2. WHEN scene detection is running and a finite chunk count is not yet known, THE Pipeline SHALL display an `alive_progress` spinner (infinite bar) for the scene detection sub-phase.
3. IF scene detection produces a boundary count before splitting begins, THEN THE Pipeline SHALL convert the progress display to a finite bar with total equal to the number of boundaries.
4. WHEN the optimization phase encodes chunk attempts, THE Pipeline SHALL display an `alive_progress` bar with total equal to `M × N` where `M` is the number of strategies under test and `N` is the number of test chunks, incrementing by one on each successfully completed chunk attempt.
5. WHEN the encoding phase encodes chunks, THE Pipeline SHALL display an `alive_progress` bar with total equal to `M × N` where `M` is the number of strategies and `N` is the number of chunks, incrementing by one on each successfully completed chunk.
6. WHERE an `alive_progress` bar is active, THE Pipeline SHALL display the current strategy name and chunk identifier as the bar title or suffix so the user knows what is being processed.
7. IF a chunk attempt fails, THE Pipeline SHALL increment the progress bar and display a warning indicator rather than stalling the bar.
