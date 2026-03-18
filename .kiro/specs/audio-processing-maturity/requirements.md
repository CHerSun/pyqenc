# Requirements Document

<!-- markdownlint-disable MD024 -->

Created: 2026-03-18
Completed: -

## Introduction

This spec covers the maturation of the audio processing phase in pyqenc. The current implementation has several correctness, naming, and usability gaps that need to be addressed: the ffmpeg runner is not using the `.tmp` flow for output files; audio file naming is opaque and not human-readable; downmix strategies lack source channel layout enforcement; the strategy taxonomy needs clear short identifiers; audio clipping during custom downmix is unresolved; the static normalization library dependency should be eliminated in favour of a 2-pass ffmpeg approach; the final AAC format and the keep-filter are partially hard-coded; and the progress display needs verification.

## Glossary

- **AudioEngine**: The orchestrator class in `pyqenc/phases/audio.py` that builds and executes the audio processing plan.
- **BaseStrategy**: Abstract base class for all audio processing strategies.
- **DownmixStrategy**: A `BaseStrategy` subclass that applies ffmpeg audio filters to reduce channel count.
- **TrueNormalizeStrategy**: A `BaseStrategy` subclass that applies EBU R128 static loudness normalisation.
- **ConversionStrategy**: A terminal `BaseStrategy` subclass that converts audio to a final delivery codec (e.g. AAC).
- **strategy_short**: A concise, unique, human- and machine-readable identifier for a strategy instance (e.g. `std`, `night`, `night-boost`).
- **source stem**: The `Path.stem` of the extracted source audio file (e.g. `track_01_eng_ch=7.1`).
- **channel layout tag**: A substring embedded in a filename that identifies the channel configuration (e.g. `ch=7.1`, `ch=5.1`, `ch=2.0`, `ch=stereo`).
- **static normalization**: A 2-pass EBU R128 loudness normalisation that computes a single gain value for the whole file and applies it uniformly.
- **dynamic normalization**: A 1-pass ffmpeg `loudnorm` filter that applies a time-varying gain curve over the file duration.
- **dynaudnorm**: The ffmpeg `dynaudnorm` filter, a 3rd-party filter included in ffmpeg by default, which applies dynamic normalisation while retaining more perceived clarity than `loudnorm`.
- **audio clipping**: Distortion that occurs when audio sample values exceed the representable range of the output format during ffmpeg processing.
- **2-pass clipping prevention**: A technique where pass 1 applies the downmix strategy and analyses the resulting loudness/peak, and pass 2 applies the same strategy together with a compensating gain — both within a single ffmpeg invocation per pass.
- **terminal task**: A task in the `AudioEngine` plan that is a leaf node; the `ConversionStrategy` finalizer is applied to it.
- **keep filter**: A regex pattern that selects which terminal audio results are converted to the final delivery format (AAC).
- **dry-run**: Execution mode where the plan is built and displayed but no files are written.
- **`.tmp` flow**: The protocol enforced by `run_ffmpeg` / `run_ffmpeg_async` where output files are written to a `<stem>.tmp` sibling and atomically renamed to the final name on success.
- **ffmpeg-normalize**: A Python package currently used for static normalisation; targeted for removal in this spec.
- **PipelineConfig**: The Pydantic model in `pyqenc/models.py` that holds all pipeline configuration.

---

## Requirements

### Requirement 1 — `.tmp` flow for all ffmpeg audio calls

**User Story:** As a developer, I want all ffmpeg calls in the audio phase to use the `.tmp`-then-rename protocol, so that partial output files are never left on disk after a failure.

#### Acceptance Criteria

1. WHEN the Audio Processing System executes any ffmpeg command that produces an output file, THE Audio Processing System SHALL pass the intended output `Path` as the `output_file` argument to `run_ffmpeg` or `run_ffmpeg_async`.
2. WHEN an ffmpeg audio command fails, THE Audio Processing System SHALL leave no partial output file at the final output path.
3. WHEN an ffmpeg audio command succeeds, THE Audio Processing System SHALL rename the `.tmp` sibling to the final output path before returning.

---

### Requirement 2 — Human-readable audio file naming

**User Story:** As a user, I want each intermediate and final audio file to have a name that clearly identifies its origin and the transformation applied, so that I can inspect the work directory and understand what each file represents.

#### Acceptance Criteria

1. WHEN the Audio Processing System produces an intermediate or final audio file, THE Audio Processing System SHALL name the file using the pattern `{strategy_short} from {stem}.flac` (or `.aac` for the final delivery file), where `stem` is the source stem of the original extracted audio file and `strategy_short` is the unique short identifier of the strategy that produced the file.
2. WHEN the Audio Processing System applies a chain of strategies to a source file, THE Audio Processing System SHALL use the immediate parent file's stem as the `stem` component in the output filename.
3. THE Audio Processing System SHALL ensure that `strategy_short` values are unique across all registered strategies so that filenames are unambiguous.

---

### Requirement 3 — Strategy short identifiers

**User Story:** As a developer, I want each strategy to carry a concise, unique `strategy_short` identifier, so that filenames and log messages are self-explanatory and parseable by code.

#### Acceptance Criteria

1. THE Audio Processing System SHALL assign the following `strategy_short` values to the built-in strategies:
   - 7.1→5.1 downmix: `71to51`
   - 5.1→2.0 standard (ffmpeg default, ignores LFE): `std`
   - 5.1→2.0 night mode (incorporates LFE mildly): `night`
   - 5.1→2.0 night mode boosted dialogs (stronger LFE boost): `night-boost`
   - Static gain normalisation (2-pass): `norm`
   - Dynamic loudnorm normalisation (1-pass, applied on top of `norm`): `dyna`
   - dynaudnorm normalisation (applied on top of `norm`): `dynaudnorm`
   - AAC conversion finalizer: `aac`
2. THE Audio Processing System SHALL expose `strategy_short` as a required attribute on `BaseStrategy`.
3. WHEN two strategies share the same `strategy_short`, THE Audio Processing System SHALL raise a `ValueError` at `AudioEngine` construction time.

---

### Requirement 4 — Source channel layout enforcement

**User Story:** As a user, I want the pipeline to refuse to apply a downmix strategy to an incompatible source file, so that I never get silently wrong output from a mismatched strategy.

#### Acceptance Criteria

1. WHEN the 7.1→5.1 strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename contains the channel layout tag `ch=7.1`.
2. WHEN a 5.1→2.0 strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename contains the channel layout tag `ch=5.1`.
3. WHEN a normalisation strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename contains the channel layout tag `ch=2.0` or `ch=stereo`, and the file does not already carry the `norm` short identifier in its name.
4. WHEN a dynamic normalisation strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename indicates that static normalisation (`norm`) has already been applied.

---

### Requirement 5 — Audio clipping prevention via 2-pass static normalisation

**User Story:** As a user, I want downmixed audio files to never clip, so that the output audio has correct loudness without distortion.

#### Acceptance Criteria

1. WHEN the Audio Processing System applies a custom downmix strategy (night mode or night-boost), THE Audio Processing System SHALL perform a 2-pass process: pass 1 applies the downmix filter and the `loudnorm` analysis filter in a single ffmpeg call, and pass 2 applies the downmix filter together with the measured gain correction in a single ffmpeg call.
2. WHEN pass 1 of the 2-pass process completes, THE Audio Processing System SHALL parse the `loudnorm` JSON measurement from the ffmpeg output.
3. WHEN pass 2 of the 2-pass process completes, THE Audio Processing System SHALL produce a FLAC output file that does not exceed −0.5 dBFS peak level.
4. WHEN the standard 5.1→2.0 strategy is applied, THE Audio Processing System SHALL apply it in a single ffmpeg pass (no clipping risk because LFE is discarded).
5. WHEN the 7.1→5.1 strategy is applied, THE Audio Processing System SHALL apply it in a single ffmpeg pass (no clipping risk because channel levels are not boosted).

---

### Requirement 6 — Static normalisation without ffmpeg-normalize library

**User Story:** As a developer, I want static normalisation to be implemented directly via ffmpeg without the `ffmpeg-normalize` Python package, so that we reduce external dependencies and gain full control over the normalisation process.

#### Acceptance Criteria

1. THE Audio Processing System SHALL implement static gain normalisation using a 2-pass ffmpeg `loudnorm` approach: pass 1 measures integrated loudness and true peak; pass 2 applies the measured values as linear normalisation.
2. WHEN the static normalisation strategy executes, THE Audio Processing System SHALL invoke `run_ffmpeg` or `run_ffmpeg_async` for both passes, passing the output `Path` as `output_file` to enforce the `.tmp` flow.
3. THE Audio Processing System SHALL NOT import or use the `ffmpeg_normalize` Python package anywhere in the audio phase.
4. WHEN static normalisation pass 1 fails to produce a parseable loudnorm JSON measurement, THE Audio Processing System SHALL log an error and mark the task as failed without writing any output file.

---

### Requirement 7 — Configurable final format and keep filter

**User Story:** As a user, I want to control which processed audio streams are converted to the final delivery format and what that format is, so that I can tailor the output to my needs without modifying source code.

#### Acceptance Criteria

1. THE Audio Processing System SHALL accept a `keep_filter` parameter (regex string) that selects which terminal audio results are passed to the `ConversionStrategy` finalizer; the default value SHALL be `^(norm|dyna|dynaudnorm) from `.
2. THE Audio Processing System SHALL accept a `final_codec` parameter (string, e.g. `aac`) with a default value of `aac`.
3. THE Audio Processing System SHALL accept a `final_bitrate` parameter (string, e.g. `192k`) with a default value of `192k`.
4. THE Audio Processing System SHALL accept a `final_extension` parameter (string, e.g. `.aac`) with a default value of `.aac`.
5. WHEN the `auto` CLI subcommand is invoked, THE CLI SHALL expose `--audio-keep`, `--audio-codec`, and `--audio-bitrate` options that map to `keep_filter`, `final_codec`, and `final_bitrate` respectively.
6. WHEN the `audio` CLI subcommand is invoked, THE CLI SHALL expose the same `--audio-keep`, `--audio-codec`, and `--audio-bitrate` options.
7. WHEN a user does not supply `--audio-keep`, THE CLI SHALL use the default keep filter value defined in Requirement 7.1.

---

### Requirement 8 — Items-based progress display

**User Story:** As a user, I want to see a live progress bar that counts completed audio tasks, so that I can monitor how far along the audio phase is.

#### Acceptance Criteria

1. WHEN the Audio Processing System executes the plan in non-dry-run mode, THE Audio Processing System SHALL display a single `alive_bar` progress bar that advances by one unit for each completed task (success or failure).
2. WHEN the Audio Processing System builds the plan in dry-run mode, THE Audio Processing System SHALL display the full list of planned tasks without executing them and without advancing a progress bar.
3. WHEN a task is skipped due to a parent failure, THE Audio Processing System SHALL still advance the progress bar by one unit and log a warning.
4. WHEN the Audio Processing System displays progress, THE progress bar text SHALL include the `strategy_short` identifier and the output filename of the current task.
