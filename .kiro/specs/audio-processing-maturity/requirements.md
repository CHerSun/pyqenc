# Requirements Document

<!-- markdownlint-disable MD024 -->

Created: 2026-03-18
Completed: 2026-03-18

## Introduction

This spec covers the maturation of the audio processing phase in pyqenc. The current implementation has several correctness, naming, and usability gaps that need to be addressed: the ffmpeg runner is not using the `.tmp` flow for output files; audio file naming is opaque and not human-readable; downmix strategies lack source channel layout enforcement; the strategy taxonomy needs clear short identifiers; audio clipping during custom downmix is unresolved; the static normalization library dependency should be eliminated in favour of a 2-pass ffmpeg approach; the final AAC format and the keep-filter are partially hard-coded; and the progress display needs verification.

### Target processing graph

```
ch=7.1 source  →  5.1 ← stem.flac         (single-pass downmix, no normalisation)
                        ↓ (treated as ch=5.1)
ch=5.1 source  →  2.0 std ← stem.flac     (downmix + EBU R128, 2-pass)
               →  2.0 night ← stem.flac   (downmix + EBU R128, 2-pass, LFE mild)
               →  2.0 nboost ← stem.flac  (downmix + EBU R128, 2-pass, LFE strong)
any source     →  norm ← stem.flac        (EBU R128 only, 2-pass, if not yet normalised)

any of the above normalised outputs:
               →  dynaudnorm ← 2.0 std ← stem.flac
               →  dynaudnorm ← 2.0 night ← stem.flac
               →  dynaudnorm ← 2.0 nboost ← stem.flac
               →  dynaudnorm ← norm ← stem.flac

keep-filter    →  aac ← ... .aac          (final delivery, profile-selected bitrate; only converted files go into the final MKV)
```

## Glossary

- **AudioEngine**: The orchestrator class in `pyqenc/phases/audio.py` that builds and executes the audio processing plan.
- **BaseStrategy**: Abstract base class for all audio processing strategies.
- **DownmixStrategy**: A `BaseStrategy` subclass that applies ffmpeg audio filters to reduce channel count.
- **TrueNormalizeStrategy**: A `BaseStrategy` subclass that applies EBU R128 static loudness normalisation.
- **ConversionStrategy**: A terminal `BaseStrategy` subclass that converts audio to a final delivery codec (e.g. AAC).
- **strategy_short**: A concise, unique, human- and machine-readable identifier for a strategy instance (e.g. `std`, `night`, `nboost`). Used as the prefix in output filenames: `{strategy_short} ← {stem}`.
- **audio filename separator**: The `←` character used between `strategy_short` and `stem` in audio output filenames. Defined as a named constant `AUDIO_STEM_SEPARATOR` in `pyqenc/constants.py`.
- **source stem**: The `Path.stem` of the extracted source audio file as produced by the extraction phase (e.g. `#2 ID=2 (audio-ac3) lang=rus ch=5.1(side) start=0.028`). The extraction phase naming convention MUST be preserved; the audio processing phase relies on the channel layout tag embedded in this stem.
- **channel layout tag**: A substring embedded in a filename that identifies the channel configuration (e.g. `ch=7.1`, `ch=5.1`, `ch=2.0`, `ch=stereo`). The exact tag strings MUST be defined as named constants shared between the extraction phase and the audio processing phase — never duplicated as magic strings.
- **static normalization**: A 2-pass EBU R128 loudness normalisation that computes a single gain value for the whole file and applies it uniformly.
- **dynaudnorm**: The ffmpeg `dynaudnorm` filter, a 3rd-party filter included in ffmpeg by default, which applies dynamic normalisation while retaining more perceived clarity than `loudnorm`. Applied on top of static `norm` as a final leaf strategy (`strategy_short`: `dynaudnorm`).
- **audio clipping**: Distortion that occurs when audio sample values exceed the representable range of the output format during ffmpeg processing.
- **2-pass clipping prevention**: A technique where pass 1 applies the downmix strategy and analyses the resulting loudness/peak, and pass 2 applies the same strategy together with a compensating gain — both within a single ffmpeg invocation per pass.
- **terminal task**: A task in the `AudioEngine` plan that is a leaf node — no further strategy in the registered list returns `True` from `check` for its output file. Termination is achieved naturally because each strategy's `check` method is scoped to its specific input conditions; no explicit `is_terminal` flag is required.
- **keep filter**: A regex pattern supplied by the user that selects which audio files (by filename) are passed to the `ConversionStrategy` finalizer for conversion to the final delivery format. The finalizer is applied to any file whose name matches the keep filter, regardless of whether that file is a terminal task or an intermediate one.
- **stream include filter**: A regex pattern applied during extraction to ALL stream types (video, audio, subtitles, chapters). Only streams whose would-be output filename matches this pattern are extracted. `None` means include all.
- **stream exclude filter**: A regex pattern applied during extraction to ALL stream types. Streams whose would-be output filename matches this pattern are skipped, even if they also match the include filter. `None` means exclude none. Exclusion takes precedence over inclusion.
- **audio-convert filter**: A regex pattern that selects which processed audio files (by filename) are converted to the final delivery format (AAC) at the end of the audio processing phase. Only files matching this pattern are passed to the `ConversionStrategy` finalizer.- **dry-run**: Execution mode where the plan is built and displayed but no files are written.
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

1. WHEN the Audio Processing System produces an intermediate or final audio file, THE Audio Processing System SHALL name the file using the pattern `{strategy_short} ← {stem}.flac` (or `.aac` for the final delivery file), where `stem` is the stem of the immediate source file and `strategy_short` is the unique short identifier of the strategy that produced the file.
2. WHEN the Audio Processing System applies a chain of strategies to a source file, THE Audio Processing System SHALL use the immediate parent file's stem as the `stem` component in the output filename.
3. THE Audio Processing System SHALL ensure that `strategy_short` values are unique across all registered strategies so that filenames are unambiguous.
4. THE Audio Processing System SHALL define the `←` separator character used in audio filenames as a named constant in `pyqenc/constants.py`, and all audio filename construction SHALL reference that constant exclusively.

---

### Requirement 3 — Strategy short identifiers

**User Story:** As a developer, I want each strategy to carry a concise, unique `strategy_short` identifier, so that filenames and log messages are self-explanatory and parseable by code.

#### Acceptance Criteria

1. THE Audio Processing System SHALL assign the following `strategy_short` values to the built-in strategies:
   - 7.1→5.1 downmix: `5.1`
   - 5.1→2.0 standard with EBU R128 normalisation (ffmpeg default downmix, ignores LFE): `2.0 std`
   - 5.1→2.0 night mode with EBU R128 normalisation (incorporates LFE mildly): `2.0 night`
   - 5.1→2.0 night mode boosted with EBU R128 normalisation (stronger LFE boost): `2.0 nboost`
   - Static gain normalisation (2-pass EBU R128, applied to any source that has not yet been normalised by `norm`, `2.0 std`, `2.0 night`, or `2.0 nboost`): `norm`
   - dynaudnorm normalisation (applied on top of any normalised output — `norm ←`, `2.0 std ←`, `2.0 night ←`, or `2.0 nboost ←`): `dynaudnorm`
   - AAC conversion finalizer: `aac`
2. THE Audio Processing System SHALL expose `strategy_short` as a required attribute on `BaseStrategy`.
3. WHEN two strategies share the same `strategy_short`, THE Audio Processing System SHALL raise a `ValueError` at `AudioEngine` construction time.

---

### Requirement 4 — Source channel layout enforcement

**User Story:** As a user, I want the pipeline to refuse to apply a downmix strategy to an incompatible source file, so that I never get silently wrong output from a mismatched strategy.

#### Acceptance Criteria

1. WHEN the 7.1→5.1 strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename contains the channel layout tag `ch=7.1`.
2. WHEN a 5.1→2.0 strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename contains the channel layout tag `ch=5.1`.
3. WHEN the `norm` strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename does not already carry any normalisation indicator — i.e. the filename does not start with `norm ←`, `2.0 std ←`, `2.0 night ←`, or `2.0 nboost ←`.
4. WHEN the `dynaudnorm` strategy's `check` method is called with a source file, THE Audio Processing System SHALL return `True` only if the source filename indicates that a normalised output has already been produced — i.e. the filename starts with `norm ←`, `2.0 std ←`, `2.0 night ←`, or `2.0 nboost ←`.
5. THE Audio Processing System SHALL reference channel layout tag strings exclusively via named constants defined in `pyqenc/constants.py`, and the extraction phase SHALL use the same constants when constructing output filenames.
6. THE Audio Processing System SHALL NOT rename or modify the source audio files produced by the extraction phase; the source stem is used as-is in the `{strategy_short} ← {stem}` naming pattern.

---

### Requirement 5 — Audio clipping prevention via 2-pass static normalisation

**User Story:** As a user, I want downmixed audio files to never clip, so that the output audio has correct loudness without distortion.

#### Acceptance Criteria

1. WHEN the Audio Processing System applies a custom downmix strategy (`2.0 night` or `2.0 nboost`), THE Audio Processing System SHALL perform a 2-pass EBU R128 process using a shared internal helper: pass 1 applies the downmix filter combined with the `loudnorm` analysis filter in a single ffmpeg call to measure integrated loudness and true peak; pass 2 applies the downmix filter combined with the `loudnorm` linear normalisation filter (using the measured values from pass 1) in a single ffmpeg call to produce the final FLAC output.
2. WHEN pass 1 of the 2-pass process completes, THE Audio Processing System SHALL parse the `loudnorm` JSON measurement block from the ffmpeg stderr output.
3. WHEN pass 2 of the 2-pass process completes, THE Audio Processing System SHALL produce a FLAC output file that does not exceed −0.5 dBFS true peak level.
4. WHEN the `2.0 std` strategy is applied, THE Audio Processing System SHALL apply the downmix and EBU R128 normalisation using the same shared 2-pass helper with the standard ffmpeg downmix filter arguments, producing a normalised output in a single pipeline.
5. WHEN the 7.1→5.1 strategy is applied, THE Audio Processing System SHALL apply it in a single ffmpeg pass without normalisation (no clipping risk because channel levels are not boosted).

---

### Requirement 6 — Static normalisation without ffmpeg-normalize library

**User Story:** As a developer, I want static normalisation to be implemented directly via ffmpeg without the `ffmpeg-normalize` Python package, so that we reduce external dependencies and gain full control over the normalisation process.

#### Acceptance Criteria

1. THE Audio Processing System SHALL implement a shared 2-pass EBU R128 `loudnorm` helper function that: (a) accepts an input `Path`, an optional list of additional ffmpeg audio filter arguments to prepend, and an output `Path`; (b) runs pass 1 to measure integrated loudness and true peak; (c) runs pass 2 to apply linear normalisation (with the prepended filters if any) and write the output FLAC file.
2. THE Audio Processing System SHALL implement the standalone `norm` strategy by invoking the shared 2-pass helper with no additional filter arguments, producing a `norm ← {stem}.flac` output file.
3. THE Audio Processing System SHALL implement the clipping-prevention step in night and nboost strategies by invoking the shared 2-pass helper with the respective downmix filter arguments prepended, so that the downmix and normalisation are applied together in each pass.
4. WHEN the shared 2-pass helper executes, THE Audio Processing System SHALL invoke `run_ffmpeg` or `run_ffmpeg_async` for both passes, passing the output `Path` as `output_file` to enforce the `.tmp` flow.
5. THE Audio Processing System SHALL NOT import or use the `ffmpeg_normalize` Python package anywhere in the audio phase.
6. WHEN pass 1 of the shared helper fails to produce a parseable loudnorm JSON measurement, THE Audio Processing System SHALL log an error and mark the task as failed without writing any output file.

---

### Requirement 7 — Configurable final format and convert filter

**User Story:** As a user, I want to control which processed audio streams are converted to the final delivery format and what that format is, so that I can tailor the output to my needs without modifying source code.

#### Acceptance Criteria

1. THE Audio Processing System SHALL read the default `audio_convert` pattern from `default_config.yaml` under the `audio_output` section; the built-in default SHALL match all normalised outputs — files whose name starts with `norm ←`, `dynaudnorm ←`, `2.0 std ←`, `2.0 night ←`, or `2.0 nboost ←`.
2. THE Audio Processing System SHALL apply the `ConversionStrategy` finalizer to every file whose name matches the `audio_convert` pattern, regardless of whether that file is a leaf node in the processing graph.
3. THE Audio Processing System SHALL NOT apply the `ConversionStrategy` finalizer based on any `is_terminal` flag; the finalizer is applied solely based on the `audio_convert` regex match against the output filename.
4. THE Audio Processing System SHALL allow the user to save a custom `audio_convert` pattern in a user-supplied `pyqenc.yaml` configuration file under the same `audio_output` section.
5. WHEN the `auto` CLI subcommand is invoked, THE CLI SHALL expose an `--audio-convert` option that overrides the config-derived pattern for that run.
6. WHEN the `audio` CLI subcommand is invoked, THE CLI SHALL expose the same `--audio-convert` option.
7. WHEN a user does not supply `--audio-convert`, THE CLI SHALL use the `audio_convert` value from the loaded configuration.

---

### Requirement 9 — Per-channel-layout final conversion profiles

**User Story:** As a user, I want the final delivery codec and bitrate to be automatically chosen based on the channel layout of the audio being converted, so that stereo and surround streams each get an appropriate bitrate without requiring multiple CLI flags.

#### Acceptance Criteria

1. THE Audio Processing System SHALL define an `audio_output` section in `default_config.yaml` containing: a `keep_filter` regex string (default per Requirement 7.1) and a `profiles` map keyed by channel layout (e.g. `2.0`, `5.1`, `7.1`), each specifying `codec`, `bitrate`, and `extension`; the built-in profile defaults SHALL be: `2.0` → AAC CBR 192k `.aac`; `5.1` → AAC CBR 512k `.aac`; `7.1` → AAC CBR 768k `.aac`.
2. WHEN the `ConversionStrategy` finalizer is applied to a file, THE Audio Processing System SHALL encode the output in CBR (constant bitrate) mode to ensure compatibility with video remuxing tools that do not correctly handle VBR audio.
3. WHEN the `ConversionStrategy` finalizer is applied to a file, THE Audio Processing System SHALL select the conversion profile by matching the channel layout tag present in the source filename against the configured profiles.
4. WHEN no profile matches the channel layout of a file, THE Audio Processing System SHALL fall back to the `2.0` profile and log a warning.
5. THE Audio Processing System SHALL allow the user to override the `audio_output` section in a user-supplied `pyqenc.yaml` configuration file using the same key structure as `default_config.yaml`.
6. WHEN the `--audio-codec` and `--audio-bitrate` CLI options are supplied, THE CLI SHALL treat `--audio-bitrate` as the base bitrate for 2.0 stereo and scale it proportionally by channel count for other layouts; `--audio-codec` SHALL override the codec for all profiles in that run.

---

### Requirement 8 — Items-based progress display

**User Story:** As a user, I want to see a live progress bar that counts completed audio tasks, so that I can monitor how far along the audio phase is.

#### Acceptance Criteria

1. WHEN the Audio Processing System executes the plan in non-dry-run mode, THE Audio Processing System SHALL display a single `alive_bar` progress bar that advances by one unit for each completed task (success or failure).
2. WHEN the Audio Processing System builds the plan in dry-run mode, THE Audio Processing System SHALL display the full list of planned tasks without executing them and without advancing a progress bar.
3. WHEN a task is skipped due to a parent failure, THE Audio Processing System SHALL still advance the progress bar by one unit and log a warning.
4. WHEN the Audio Processing System displays progress, THE progress bar text SHALL include the `strategy_short` identifier and the output filename of the current task.

---

### Requirement 10 — Unified stream include/exclude filters for extraction

**User Story:** As a user, I want a single pair of include/exclude regex patterns that apply to all stream types during extraction, so that I can precisely select or reject any stream without needing separate flags per stream type.

#### Acceptance Criteria

1. THE Extraction System SHALL accept an `include` parameter (regex string or `None`) that selects streams for extraction by matching against the would-be output filename of each stream; when `None`, all streams are included.
2. THE Extraction System SHALL accept an `exclude` parameter (regex string or `None`) that rejects streams from extraction by matching against the would-be output filename; when `None`, no streams are excluded.
3. WHEN both `include` and `exclude` are supplied, THE Extraction System SHALL apply inclusion first and then exclusion, so that exclusion takes precedence over inclusion.
4. THE Extraction System SHALL apply the include/exclude filters to ALL stream types — video, audio, subtitles, and chapters — using the same would-be output filename that the extraction phase would produce for each stream.
5. WHEN the Extraction System runs in dry-run mode, THE Extraction System SHALL display each stream in a compact tabular format with a `✔` symbol for included streams and a `✗` symbol for excluded streams, vertically aligned by column.
6. WHEN the Extraction System runs in execute mode, THE Extraction System SHALL log each stream with a `✔` or `✗` symbol indicating whether it was included or excluded.
7. THE Extraction System SHALL allow the user to save default `include` and `exclude` patterns in a user-supplied `pyqenc.yaml` configuration file under a `streams` section.
8. WHEN the `auto` CLI subcommand is invoked, THE CLI SHALL expose `-i` / `--include` and `-x` / `--exclude` options that override the config-derived patterns for that run.
9. WHEN the `extract` CLI subcommand is invoked, THE CLI SHALL expose the same `-i` / `--include` and `-x` / `--exclude` options.
10. WHEN a user does not supply `-i` or `-x`, THE CLI SHALL use the values from the loaded configuration (which default to `None` — all streams extracted, none excluded).
11. WHEN the Extraction System completes a run, THE Extraction System SHALL persist the `include` and `exclude` patterns used (including `None`) in the extraction phase sidecar file alongside other phase state.
12. WHEN the Extraction System starts a subsequent run and a sidecar exists, THE Extraction System SHALL compare the current `include` and `exclude` patterns against the persisted values; IF they differ, THE Extraction System SHALL treat the phase as requiring re-execution and log a warning indicating the filter change.
