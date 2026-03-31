# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-10

## Introduction

This feature adds pipeline-level metrics collection and reporting to pyqenc. A `metrics.yaml` sidecar is written to the work directory root and kept incrementally up-to-date throughout the run. It summarises how wall-clock time and disk space are distributed across pipeline phases, plus convergence statistics for the CRF search. The goal is a developer-facing overview — "where did the time go, where did the space go, how hard was convergence?" — without per-event instrumentation or heavyweight profiling.

Because multiple ffmpeg processes run in parallel at reduced priority, raw CPU time is not representative; the report therefore tracks wall-clock wait time per phase category and disk space consumed per artifact category, both expressed as absolute values and as a percentage of the total.

Metrics are initialised once when the job starts and survive interruptions: the file is flushed periodically during long phases and immediately on process exit (signal or crash), so a partial run still yields a useful snapshot. A `force_wipe` resets metrics along with all other artifacts.

## Glossary

- **MetricsCollector**: The component responsible for recording phase timing intervals and disk space snapshots, persisting them incrementally to `metrics.yaml`, and producing the final summary.
- **PipelineMetrics**: The Pydantic model that holds the complete metrics state serialised to `metrics.yaml`.
- **TimeKey**: A `StrEnum` whose values are dotted strings of the form `"phase.event"` (e.g. `TimeKey.ENCODING_MAIN = "encoding.main"`). The prefix identifies the phase/subsystem; the suffix identifies the event type within it. Using `StrEnum` gives static type safety while producing readable dotted keys directly in YAML output. Internal storage is `dict[TimeKey, float]` (accumulated seconds). Grouping by prefix at report time (e.g. summing all `encoding.*` keys) is derived by splitting on `"."` — no extra schema is needed. The complete set of `TimeKey` values is:
  - `TimeKey.JOB_PROBE = "job.probe"` — VideoMetadata probing (ffprobe, stat calls in JobPhase)
  - `TimeKey.JOB_CROP_DETECT = "job.crop_detect"` — Black border detection (ffmpeg scan in `_resolve_crop`)
  - `TimeKey.EXTRACTION = "extraction.mkvextract"` — mkvextract stream extraction
  - `TimeKey.CHUNKING_SCENE_DETECT = "chunking.scene_detect"` — PySceneDetect analysis
  - `TimeKey.CHUNKING_SPLIT = "chunking.split"` — FFV1/remux chunk splitting (per-chunk, incremental)
  - `TimeKey.AUDIO = "audio.processing"` — Full AudioPhase wall-clock (no sub-type split; AudioPhase runs tasks in an async DAG and hooking individual strategy types would add complexity not worth the gain at this stage)
  - `TimeKey.ENCODING_OPTIMIZATION = "encoding.optimization"` — CRF test encodes in OptimizationPhase (per-attempt, incremental)
  - `TimeKey.ENCODING_MAIN = "encoding.main"` — CRF encodes in EncodingPhase (per-attempt, incremental)
  - `TimeKey.MERGE_CONCAT = "merge.concat"` — ffmpeg/mkvmerge concatenation (per-strategy)
  - `TimeKey.MERGE_QUALITY_MEASURE = "merge.quality_measure"` — Final VMAF/PSNR measurement (per-strategy; kept separate because VMAF measurement can be surprisingly slow and is worth distinguishing)
  - `TimeKey.RECOVERY = "recovery"` — Total wall-clock time spent in all phase `_recover()` calls (filesystem scans, sidecar YAML loading, artifact classification). Accumulates across all phases in a single run, giving a total "time spent on artifact scanning/classification" figure.
- **SpaceKey**: A `StrEnum` whose values are dotted strings identifying artifact storage categories. Same `StrEnum` pattern as `TimeKey`. The complete set of `SpaceKey` values is:
  - `SpaceKey.SOURCE = "source"` — source video file (not in work_dir, measured via path from config)
  - `SpaceKey.EXTRACTED_VIDEO = "extracted.video"` — extracted video stream MKV in `extracted/`
  - `SpaceKey.EXTRACTED_AUDIO = "extracted.audio"` — raw extracted audio MKA files in `extracted/` (original surround tracks before any processing)
  - `SpaceKey.EXTRACTED_OTHER = "extracted.other"` — subtitles, chapters, attachments in `extracted/`
  - `SpaceKey.CHUNKS = "chunks"` — all chunk files in `chunks/`
  - `SpaceKey.AUDIO_INTERMEDIATE = "audio.intermediate"` — intermediate FLAC files in `audio/` (downmix/norm processing chain outputs)
  - `SpaceKey.AUDIO_FINAL = "audio.final"` — final delivery audio files in `audio/` (AAC or other codec terminal outputs)
  - `SpaceKey.ENCODING_WORKSPACE = "encoding.workspace"` — attempt files in `encoding/` (intermediate CRF search files, per-strategy subdirs)
  - `SpaceKey.ENCODING_OUTPUTS = "encoding.outputs"` — winning encoded chunks in `encoded/` (hard-linked from encoding/, per-strategy subdirs)
  - `SpaceKey.FINAL = "final"` — final merged MKV outputs in `final/`
- **Dotted key**: A string of the form `"prefix.suffix"` used as the `category` field in YAML output. The prefix is the phase or artifact group; the suffix is the specific event or artifact type within it. Grouping by prefix is derived at report time by splitting on `"."` — no additional schema is required.
- **ConvergenceStats**: A model holding per-strategy attempt-count statistics (min, mean, stddev, max, total) across all chunks that completed CRF convergence. Stats are maintained as running accumulators (Welford's algorithm for mean/stddev) so no raw attempt list needs to be stored in memory or on disk — the model is fully resumable across restarts.
- **Phase sidecar**: An existing YAML file written alongside phase artifacts (e.g. `chunking.yaml`, `encoding.yaml`) that records phase parameters and state.
- **metrics.yaml**: The persistent metrics file written to the work directory root, created at job start and updated throughout the run.
- **Wall-clock interval**: The elapsed real time between the start and end of a phase category, measured with `time.monotonic()`. Not equivalent to CPU time when processes run in parallel or at reduced priority.
- **CRF convergence**: The iterative binary-search process that finds the lowest CRF value meeting quality targets for a single chunk/strategy pair.
- **Orchestrator**: `pyqenc/orchestrator.py` — the thin driver that runs phases in sequence and is the natural integration point for metrics collection.
- **Flush interval**: The maximum number of incremental step updates between automatic `metrics.yaml` writes during a long-running phase. An incremental step update occurs after each per-chunk or per-attempt completion within a phase loop.
- **Incremental step update**: A call to the MetricsCollector after each discrete unit of work within a phase loop (e.g. after each encoding attempt, after each chunk's winning encode is found), which accumulates elapsed time and attempt counts and may trigger a flush.
- **NoOpMetricsCollector**: A concrete implementation of the `MetricsCollector` Protocol that satisfies the interface but discards all data without performing any I/O. Used when metrics output is suppressed (e.g. `--no-metrics` flag) or in standalone/test contexts.

## Requirements

### Requirement 1: Metrics Lifecycle and Persistence

**User Story:** As a developer, I want metrics to survive interruptions and accumulate across the full run, so that a partial run still yields a useful snapshot and I never lose collected data.

#### Acceptance Criteria

1. WHEN a pipeline job starts (JobPhase initialises), THE MetricsCollector SHALL load any existing `metrics.yaml` from the work directory and resume accumulation from the persisted state, so that a resumed run adds to prior measurements rather than discarding them.
2. WHEN `force_wipe` is active, THE MetricsCollector SHALL delete any existing `metrics.yaml` and start fresh, consistent with all other artifact resets.
3. THE MetricsCollector SHALL automatically flush `metrics.yaml` atomically (`.tmp`-then-rename) after every `FLUSH_INTERVAL` recording calls (any combination of `time()` exits and `record_step()` calls), where `FLUSH_INTERVAL` is a named constant (default: 10). The collector tracks this counter internally — no external coordination is required.
4. WHEN the process receives a termination signal (SIGINT, SIGTERM, or Windows console control event) or an unhandled exception propagates to the top level, THE MetricsCollector SHALL perform an immediate final flush before the process exits.
5. IF writing `metrics.yaml` fails for any reason, THE MetricsCollector SHALL log a WARNING and allow the pipeline to continue — metrics write failure is non-fatal.
6. THE MetricsCollector SHALL be initialised once per pipeline run and shared across all phases via the Orchestrator; it SHALL NOT be re-created between phases.

### Requirement 2: Time Distribution Tracking

**User Story:** As a developer, I want to know how wall-clock time was distributed across pipeline phases, so that I can identify bottlenecks and measure the impact of algorithm changes.

#### Acceptance Criteria

1. THE MetricsCollector SHALL record a wall-clock start timestamp (via `time.monotonic()`) when each `TimeKey` activity begins and an end timestamp when it ends, accumulating elapsed seconds per key.
2. THE MetricsCollector SHALL accumulate elapsed seconds per `TimeKey` so that keys that run multiple times (e.g. encoding chunks in a loop) sum correctly across calls.
2a. FOR long-running phases that iterate over chunks or attempts (`TimeKey.ENCODING_OPTIMIZATION`, `TimeKey.ENCODING_MAIN`), THE MetricsCollector SHALL accept incremental time updates after each step (e.g. after each encoding attempt or chunk completion) so that `metrics.yaml` reflects current elapsed time mid-phase rather than only when the phase context-manager exits.
3. WHEN the pipeline completes or is interrupted, THE MetricsCollector SHALL compute `total_seconds` as the sum of all key durations and express each key's duration as both absolute seconds and as a percentage of `total_seconds`.
4. THE MetricsCollector SHALL track all `TimeKey` values defined in the Glossary as a minimum:
   - `TimeKey.JOB_PROBE` — VideoMetadata probing (ffprobe, stat calls in JobPhase)
   - `TimeKey.JOB_CROP_DETECT` — Black border detection (ffmpeg scan in `_resolve_crop`)
   - `TimeKey.EXTRACTION` — mkvextract stream extraction
   - `TimeKey.CHUNKING_SCENE_DETECT` — PySceneDetect analysis
   - `TimeKey.CHUNKING_SPLIT` — FFV1/remux chunk splitting (per-chunk, incremental)
   - `TimeKey.AUDIO` — Full AudioPhase wall-clock (no sub-type split)
   - `TimeKey.ENCODING_OPTIMIZATION` — CRF test encodes in OptimizationPhase (per-attempt, incremental)
   - `TimeKey.ENCODING_MAIN` — CRF encodes in EncodingPhase (per-attempt, incremental)
   - `TimeKey.MERGE_CONCAT` — ffmpeg/mkvmerge concatenation (per-strategy)
   - `TimeKey.MERGE_QUALITY_MEASURE` — Final VMAF/PSNR measurement (per-strategy)
   - `TimeKey.RECOVERY` — Total wall-clock time across all phase `_recover()` calls (accumulates across phases)
5. IF a `TimeKey` has zero accumulated seconds (phase was skipped or reused), THE MetricsCollector SHALL still include it in the report with `seconds: 0` and `percent: 0.0`.
6. THE MetricsCollector SHALL sort time entries in the report in descending order of `seconds` so the largest contributors appear first.
7. WHEN a phase's `_recover()` method is called, THE Orchestrator SHALL time that call and accumulate the elapsed seconds under `TimeKey.RECOVERY`, regardless of whether the phase outcome is `REUSED` or `COMPLETED`.
8. WHEN a phase outcome is `REUSED` (no actual work done beyond recovery), THE Orchestrator SHALL record time only under `TimeKey.RECOVERY` for that phase — no time is recorded under the phase's primary work key (e.g. `TimeKey.CHUNKING_SPLIT` accumulates `0` seconds if chunking was fully reused).
9. WHEN a phase outcome is `COMPLETED`, THE Orchestrator SHALL record both `TimeKey.RECOVERY` time (for the `_recover()` scan) and the primary work key time for that phase.

### Requirement 3: Disk Space Distribution Reporting

**User Story:** As a developer, I want to know how disk space was distributed across artifact categories at the end of the run, so that I can understand storage costs and verify cleanup is working.

#### Acceptance Criteria

1. WHEN the pipeline completes or is interrupted, THE MetricsCollector SHALL measure the on-disk size of each `SpaceKey` by scanning the relevant directories/files in the work directory.
2. THE MetricsCollector SHALL track all `SpaceKey` values defined in the Glossary as a minimum:
   - `SpaceKey.SOURCE` — the source video file
   - `SpaceKey.EXTRACTED_VIDEO` — extracted video stream MKV in `extracted/`
   - `SpaceKey.EXTRACTED_AUDIO` — raw extracted audio MKA files in `extracted/` (original surround tracks before any processing)
   - `SpaceKey.EXTRACTED_OTHER` — subtitles, chapters, attachments in `extracted/` (tracked separately as these can be non-trivial in size for releases with many subtitle tracks or large font attachments, and are never cleaned up by the pipeline)
   - `SpaceKey.CHUNKS` — all files in `chunks/`
   - `SpaceKey.AUDIO_INTERMEDIATE` — intermediate FLAC files in `audio/` (distinguished by `.flac` extension)
   - `SpaceKey.AUDIO_FINAL` — final delivery audio files in `audio/` (distinguished by non-`.flac` extension, e.g. `.aac`, `.m4a`)
   - `SpaceKey.ENCODING_WORKSPACE` — all files in `encoding/` (attempt files, per-strategy subdirs)
   - `SpaceKey.ENCODING_OUTPUTS` — all files in `encoded/` (winning encoded chunks, per-strategy subdirs)
   - `SpaceKey.FINAL` — all files in `final/`
3. THE MetricsCollector SHALL compute `total_bytes` as the sum of all category sizes and express each category's size as both a human-readable string (e.g. `"2.34 GB"`) and as a percentage of `total_bytes` in the YAML output.
4. IF a directory for a `SpaceKey` does not exist or is empty, THE MetricsCollector SHALL record `0` bytes for that category.
5. THE MetricsCollector SHALL sort space entries in the report in descending order of bytes so the largest contributors appear first.
6. THE MetricsCollector SHALL NOT trigger additional ffmpeg or ffprobe calls to measure space — only `Path.stat()` and directory traversal are permitted.

### Requirement 4: CRF Convergence Statistics

**User Story:** As a developer, I want summary statistics on how many encoding attempts were needed to converge per chunk, so that I can evaluate whether CRF search algorithm changes are improving or worsening convergence.

#### Acceptance Criteria

1. WHEN the pipeline completes or is interrupted, THE MetricsCollector SHALL collect the attempt count for every `(chunk_id, strategy)` pair that reached a winning encode by reading the existing `EncodingResultSidecar` YAML files and counting matching attempt files via `ENCODED_ATTEMPT_NAME_PATTERN`.
1a. WHEN a chunk's winning encode is found during the encoding or optimization phase, THE phase SHALL call an incremental update on the MetricsCollector so that convergence stats accumulate progressively rather than only being derived at pipeline end.
2. THE MetricsCollector SHALL maintain per-strategy `ConvergenceStats` using running accumulators (no raw attempt list stored). The YAML output SHALL contain:
   - `chunks` — number of chunks that completed convergence for this strategy
   - `attempts.total` — sum of all attempts across all chunks
   - `attempts.min` — minimum attempt count across all chunks
   - `attempts.mean` — arithmetic mean of attempt counts (rounded to 1 decimal place), derived from running accumulator
   - `attempts.max` — maximum attempt count across all chunks
   - `attempts.stddev` — population standard deviation of attempt counts (rounded to 1 decimal place), derived from Welford's online algorithm
3. IF only one strategy was used, THE MetricsCollector SHALL still produce per-strategy stats (with a single entry).
4. IF no encoded result sidecars are found (e.g. all chunks were reused from a prior run with no new encodes this run), THE MetricsCollector SHALL omit the `convergence` section from the report rather than writing empty or zero stats.
5. THE MetricsCollector SHALL derive attempt counts solely from existing sidecar filenames — no additional I/O or re-encoding is permitted.

### Requirement 5: metrics.yaml Output Format

**User Story:** As a developer, I want the metrics written to a concise, human-readable YAML file, so that I can inspect results at a glance and diff them across runs.

#### Acceptance Criteria

1. THE `metrics.yaml` file SHALL follow this structure:

   ```yaml
   pipeline_metrics:
     run_date: "2026-06-10 14:32:05"   # local datetime of last file write, space-separated (not T)
     partial: false                     # true if pipeline did not complete normally

     time_distribution:
       updated_at: "2026-06-10 14:32:00"   # when time/convergence data was last captured
       total_seconds: 3847
       total_duration: "01:04:07"
       breakdown:
         - category: encoding.main
           seconds: 2101
           duration: "00:35:01"
           percent: "54.6%"
         - category: encoding.optimization
           seconds: 840
           duration: "00:14:00"
           percent: "21.8%"
         - category: audio.processing
           seconds: 410
           duration: "00:06:50"
           percent: "10.7%"
         - category: recovery
           seconds: 12
           duration: "00:00:12"
           percent: "0.3%"
         # ... remaining categories sorted descending

     space_distribution:
       updated_at: "2026-06-10 14:32:05"   # when space snapshot was taken
       total_size: "30.02 GB"
       breakdown:
         - category: encoding.workspace
           size: "18.42 GB"
           percent: "61.3%"
         - category: audio.intermediate
           size: "4.10 GB"
           percent: "13.7%"
         - category: audio.final
           size: "0.82 GB"
           percent: "2.7%"
         # ... remaining categories sorted descending

     convergence:                        # omitted if no data
       updated_at: "2026-06-10 14:32:00"   # same as time_distribution.updated_at
       strategies:
         - strategy: slow+h265-aq
           chunks: 42
           attempts:
             total: 126
             min: 1
             mean: 3.0
             max: 7
             stddev: 1.2
   ```

2. THE MetricsCollector SHALL use strategy display names (with `+` separators, not filesystem-safe names) in the `convergence` section.
3. ALL datetime strings SHALL use the format `"YYYY-MM-DD HH:MM:SS"` (space separator, no `T`, no timezone suffix). `run_date` is updated on every file write. `time_distribution.updated_at` and `convergence.updated_at` are updated on every incremental flush. `space_distribution.updated_at` is updated only when a space scan is performed (on `flush()`).
4. THE MetricsCollector SHALL set `partial: true` when the pipeline did not complete normally (interrupted, failed, or still in progress at flush time); `partial: false` only when the pipeline completed all phases successfully.
5. THE MetricsCollector SHALL log an INFO message with the path to `metrics.yaml` after the final write on pipeline completion.

### Requirement 6: Integration with Phases and Orchestrator

**User Story:** As a developer, I want metrics collection to be driven by the phases themselves so that granular sub-step timing is captured, and I want the collector to always be present so metrics are never silently missing.

#### Acceptance Criteria

1. THE `MetricsCollector` SHALL be defined as a `Protocol` (or abstract base class) so that alternative backends (e.g. OpenTelemetry, Prometheus) can be substituted by injecting a different implementation without changing any phase code.
2. EVERY phase constructor SHALL accept a `collector: MetricsCollector` parameter as a required (non-optional) argument. There is no `None` fallback — a collector is always provided, whether the phase is run via the full pipeline or invoked standalone.
3. THE Orchestrator SHALL instantiate a concrete `MetricsCollector` (the YAML-backed implementation) once per `run()` call and pass it to every phase constructor.
4. WHEN a phase is invoked standalone (e.g. from `api.py` or tests), the caller SHALL construct and pass a `MetricsCollector` — a no-op implementation that satisfies the Protocol but discards all data is acceptable for tests.
5. EACH phase SHALL own its own timing calls, using the injected collector to record the `TimeKey` values relevant to its internal sub-steps:
   - `JobPhase` → `TimeKey.JOB_PROBE` (metadata probing) and `TimeKey.JOB_CROP_DETECT` (when crop detection runs)
   - `ExtractionPhase` → `TimeKey.EXTRACTION` and `TimeKey.RECOVERY` (for `_recover()`)
   - `ChunkingPhase` → `TimeKey.CHUNKING_SCENE_DETECT`, `TimeKey.CHUNKING_SPLIT` (per-chunk, incremental), and `TimeKey.RECOVERY`
   - `AudioPhase` → `TimeKey.AUDIO` (full phase wall-clock) and `TimeKey.RECOVERY`
   - `OptimizationPhase` → `TimeKey.ENCODING_OPTIMIZATION` (per-attempt, incremental) and `TimeKey.RECOVERY`
   - `EncodingPhase` → `TimeKey.ENCODING_MAIN` (per-attempt, incremental) and `TimeKey.RECOVERY`
   - `MergePhase` → `TimeKey.MERGE_CONCAT` (per-strategy), `TimeKey.MERGE_QUALITY_MEASURE` (per-strategy), and `TimeKey.RECOVERY`
6. THE `MetricsCollector` Protocol SHALL expose only recording methods visible to phases:
   - `time(key: TimeKey) -> ContextManager` — context manager that accumulates elapsed seconds for `key` on exit
   - `record_step(key: TimeKey, elapsed_seconds: float, convergence_update: ConvergenceUpdate | None = None)` — incremental update after each per-chunk or per-attempt step; phases call this to record data only, with no knowledge of flushing
7. Flushing is self-managed by the collector via `FLUSH_INTERVAL` — the Orchestrator does not flush after normal phase completion. THE Orchestrator SHALL call `collector.flush(partial=True)` only on abnormal exit: unhandled exceptions and OS signals (SIGINT, SIGTERM, Windows console control events). Phases SHALL NOT call `flush()` — it is not part of the phase-facing Protocol surface.
8. WHEN the Orchestrator registers signal handlers for graceful shutdown, it SHALL call `collector.flush(partial=True)` as part of that shutdown sequence.

### Requirement 8: --no-metrics CLI Flag

**User Story:** As a developer, I want to suppress `metrics.yaml` output with a CLI flag, so that I can run the pipeline without any metrics file I/O when I don't need the report.

#### Acceptance Criteria

1. WHEN the `--no-metrics` flag is passed on the CLI, THE CLI SHALL set a `no_metrics: bool` field on `PipelineConfig` (default: `False`) so that the flag is propagated through the existing config path without requiring a separate parameter.
2. WHEN `PipelineConfig.no_metrics` is `True`, THE Orchestrator SHALL construct a `NoOpMetricsCollector` instead of `YamlMetricsCollector` and pass it to every phase constructor — no `metrics.yaml` file is created or written at any point during the run.
3. WHEN `PipelineConfig.no_metrics` is `False` (the default), THE Orchestrator SHALL behave exactly as specified in Requirements 1 and 6 — `YamlMetricsCollector` is used and `metrics.yaml` is written normally.
4. WHEN `--no-metrics` is active, THE MetricsCollector SHALL still accept all `time()` and `record_step()` calls from phases without error — phases are unaware of whether metrics are being persisted.
5. WHEN `--no-metrics` is active, THE Orchestrator SHALL NOT register signal handlers or `atexit` hooks for metrics flushing, since there is nothing to flush.
6. THE `--no-metrics` flag SHALL be documented in the CLI help text as: `"Suppress metrics.yaml output (metrics are still collected internally but not written to disk)"`.

### Requirement 7: No New External Dependencies

**User Story:** As a developer, I want metrics collection to use only already-approved packages and Python stdlib, so that the dependency footprint does not grow.

#### Acceptance Criteria

1. THE MetricsCollector SHALL use only Python stdlib modules (`time`, `datetime`, `pathlib`, `statistics`, `os`, `signal`) and already-approved packages (`pydantic`, `pyyaml`) for all metrics logic.
2. THE MetricsCollector SHALL NOT introduce any new pip dependencies.
3. THE MetricsCollector SHALL NOT call `psutil` or any external process to measure CPU time or memory — only wall-clock time and filesystem sizes are collected.
