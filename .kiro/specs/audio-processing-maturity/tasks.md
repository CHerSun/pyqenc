# Implementation Plan

<!-- markdownlint-disable MD024 -->

Created: 2026-03-18
Completed: 2026-03-18

- [x] 1. Add shared audio constants to `pyqenc/constants.py`





  - Add `AUDIO_STEM_SEPARATOR = "←"` constant
  - Add `AUDIO_CH_71`, `AUDIO_CH_51`, `AUDIO_CH_20`, `AUDIO_CH_STEREO` channel layout tag constants
  - Add `_NORMALISED_PREFIXES` tuple (used by `norm` and `dynaudnorm` check logic) — or export the individual prefix strings so strategies can build them from constants
  - _Requirements: 2.4, 3.1, 4.5_

- [x] 2. Refactor `BaseStrategy` and add `strategy_short` to all strategies




  - Add `strategy_short: str` as a required parameter to `BaseStrategy.__init__`
  - Add `output_path(source, extension)` helper method to `BaseStrategy` that constructs `{strategy_short} {AUDIO_STEM_SEPARATOR} {source.stem}.{extension}`
  - Update `AudioEngine.__init__` to validate uniqueness of `strategy_short` across all registered strategies, raising `ValueError` on collision
  - Remove `is_terminal` field from `Task` dataclass
  - _Requirements: 2.1, 2.4, 3.2, 3.3_

- [x] 3. Implement the shared 2-pass EBU R128 helper





  - Implement `_two_pass_loudnorm(source, output, extra_filters)` async function in `phases/audio.py`
  - Pass 1: build ffmpeg cmd with `loudnorm=print_format=json` analysis filter (plus any `extra_filters` prepended), call `run_ffmpeg_async` with `output_file=None` (analysis only, no file output)
  - Parse the `loudnorm` JSON block from `FFmpegRunResult.stderr_lines` using a regex scan for the JSON between `[Parsed_loudnorm` markers
  - Pass 2: build ffmpeg cmd with `loudnorm=linear=true:...` using measured values (plus `extra_filters`), call `run_ffmpeg_async` with `output_file=output` to enforce `.tmp` flow
  - Raise `RuntimeError` if pass 1 JSON is not parseable
  - _Requirements: 5.1, 5.2, 5.3, 6.1, 6.4, 6.6_

- [x] 4. Rewrite all strategy classes with correct `check()`, naming, and `.tmp` flow





- [x] 4.1 Implement `DownmixStrategy71to51` (`strategy_short="5.1"`)


  - `check()`: returns `True` only if `AUDIO_CH_71` is in source filename
  - `execute_async()`: single-pass ffmpeg downmix, `output_file=output` passed to `run_ffmpeg_async`
  - `plan()`: uses `output_path(source)` helper
  - _Requirements: 1.1, 3.1, 4.1, 5.5_

- [x] 4.2 Implement `DownmixStrategy51to20Std` (`strategy_short="2.0 std"`)

  - `check()`: returns `True` only if `AUDIO_CH_51` is in source filename
  - `execute_async()`: delegates to `_two_pass_loudnorm` with standard ffmpeg downmix filter (`-ac 2`) prepended
  - `plan()`: uses `output_path(source)` helper
  - _Requirements: 1.1, 3.1, 4.2, 5.4_

- [x] 4.3 Implement `DownmixStrategy51to20Night` (`strategy_short="2.0 night"`)

  - `check()`: returns `True` only if `AUDIO_CH_51` is in source filename
  - `execute_async()`: delegates to `_two_pass_loudnorm` with night-mode LFE downmix filter prepended
  - `plan()`: uses `output_path(source)` helper
  - _Requirements: 1.1, 3.1, 4.2, 5.1_

- [x] 4.4 Implement `DownmixStrategy51to20NBoost` (`strategy_short="2.0 nboost"`)

  - `check()`: returns `True` only if `AUDIO_CH_51` is in source filename
  - `execute_async()`: delegates to `_two_pass_loudnorm` with boosted LFE downmix filter prepended
  - `plan()`: uses `output_path(source)` helper
  - _Requirements: 1.1, 3.1, 4.2, 5.1_

- [x] 4.5 Implement `NormStrategy` (`strategy_short="norm"`)

  - `check()`: returns `True` only if source filename does NOT start with any normalised prefix (`norm ←`, `2.0 std ←`, `2.0 night ←`, `2.0 nboost ←`)
  - `execute_async()`: delegates to `_two_pass_loudnorm` with no extra filters
  - `plan()`: uses `output_path(source)` helper
  - Remove all usage of `ffmpeg_normalize` library
  - _Requirements: 1.1, 3.1, 4.3, 6.2, 6.5_

- [x] 4.6 Implement `DynaudnormStrategy` (`strategy_short="dynaudnorm"`)

  - `check()`: returns `True` only if source filename starts with a normalised prefix
  - `execute_async()`: single-pass ffmpeg with `dynaudnorm` filter, `output_file=output` passed to `run_ffmpeg_async`
  - `plan()`: uses `output_path(source)` helper
  - _Requirements: 1.1, 3.1, 4.4_

- [x] 4.7 Rewrite `ConversionStrategy` to be profile-aware with CBR enforcement

  - Accept `profiles: dict[str, AudioConversionProfile]` and optional `base_bitrate_override: str | None`
  - `plan()`: uses `output_path(source, extension)` helper with profile-derived extension
  - `execute_async()`: select profile by scanning source filename for channel layout tag; scale bitrate by channel count if `base_bitrate_override` is set; enforce CBR via `-b:a <bitrate>` (no `-vbr`); pass `output_file=output` to `run_ffmpeg_async`
  - Fall back to `2.0` profile with `logger.warning` when no layout tag matches
  - `check()`: always returns `False` (applied via keep filter only)
  - _Requirements: 1.1, 7.2, 7.3, 9.2, 9.3, 9.4_

- [x] 5. Rework `AudioEngine.build_plan()` to use keep-filter for finalizer dispatch





  - Remove `max_depth`, `keep`, `include`, `exclude` parameters from `build_plan()`; replace with `convert_filter: str` (compiled regex)
  - After each new output path is added to the queue, check if its filename matches `convert_filter`; if so, append a `ConversionStrategy` task for it (with the output as source)
  - Remove all `is_terminal` / `force_keep` / `stop_reached` logic
  - Graph terminates naturally: `dynaudnorm` outputs never match any strategy's `check()`
  - _Requirements: 7.2, 7.3_

- [x] 6. Update `SynchronousRunner` progress display





  - Replace per-task `progress.text` updates with a running summary counter: `✔ {success}  ✘ {failed}  ⏭ {skipped}`, updated after each task
  - In dry-run mode, print the full task list (one line per task showing `strategy_short` and output filename) without executing and without a progress bar
  - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 7. Add `audio_output` and `streams` sections to config





- [x] 7.1 Add `AudioConversionProfile` and `AudioOutputConfig` dataclasses to `config.py`


  - `AudioConversionProfile(codec, bitrate, extension)`
  - `AudioOutputConfig(convert_filter, profiles)`
  - `StreamFilterConfig(include, exclude)`
  - _Requirements: 7.1, 9.1_


- [x] 7.2 Add `audio_output` and `streams` sections to `default_config.yaml`

  - `audio_output.convert_filter` default regex matching all normalised outputs
  - `audio_output.profiles` with `2.0` (AAC 192k), `5.1` (AAC 512k), `7.1` (AAC 768k)
  - `streams.include: null`, `streams.exclude: null`
  - _Requirements: 7.1, 9.1, 10.7_


- [x] 7.3 Add `get_audio_output_config()` and `get_stream_filter()` methods to `ConfigManager`

  - Parse `audio_output` section into `AudioOutputConfig`
  - Parse `streams` section into `StreamFilterConfig`
  - _Requirements: 7.4, 9.5, 10.7_

- [x] 8. Refactor extraction phase to use unified include/exclude filters






  - Replace `video_filter` / `audio_filter` parameters in `extract_streams()` with `include: str | None` and `exclude: str | None`
  - Apply `streams_filter_plain_regex(tracks, include, exclude)` to ALL stream types in one call (not per-type)
  - Persist `include` and `exclude` values in the extraction phase sidecar
  - On subsequent runs, compare persisted vs current values; log a warning and mark phase as needing re-execution if they differ
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.11, 10.12_

- [x] 9. Add compact stream display to extraction phase





  - After filtering, print each stream with `✔` (included) or `✗` (excluded) symbol followed by the would-be output filename, columns vertically aligned
  - Show this display in both dry-run and execute modes (dry-run: before any extraction; execute: as part of the extraction log)
  - _Requirements: 10.5, 10.6_

- [x] 10. Update CLI arguments





  - Remove `--video-filter` and `--audio-filter` from `auto` and `extract` subcommands
  - Add `-i` / `--include` and `-x` / `--exclude` to `auto` and `extract` subcommands; wire to `extract_streams(include=..., exclude=...)`
  - Remove `--audio-keep` from `auto` and `audio` subcommands
  - Add `--audio-convert`, `--audio-codec`, `--audio-bitrate` to `auto` and `audio` subcommands
  - Update `PipelineConfig` model to replace `video_filter`/`audio_filter` with `include`/`exclude`, and add `audio_convert`, `audio_codec`, `audio_base_bitrate` fields
  - _Requirements: 7.5, 7.6, 9.6, 10.8, 10.9, 10.10_

- [x] 11. Wire updated audio phase into `process_audio_streams()` and orchestrator





  - Instantiate all new strategy classes with correct `strategy_short` values
  - Pass `convert_filter` from config/CLI to `AudioEngine.build_plan()`
  - Pass `AudioOutputConfig.profiles` (with optional CLI overrides applied) to `ConversionStrategy`
  - Remove legacy day/night mode split and post-processing rename logic
  - Update `AudioResult` if needed to reflect the new output structure
  - _Requirements: 7.1, 7.2, 9.2, 9.3_

- [x] 12. Unit tests for new audio components



  - Test each strategy's `check()` with matching and non-matching filenames
  - Test `AudioEngine.build_plan()` graph shape against the target processing graph
  - Test `ConversionStrategy` profile selection and CBR bitrate scaling
  - Test `_two_pass_loudnorm` loudnorm JSON parsing with realistic stderr samples (mock `run_ffmpeg_async`)
  - Test `streams_filter_plain_regex()` with include-only, exclude-only, and combined patterns
  - _Requirements: 1.1, 4.1–4.4, 6.6, 7.2, 9.3_

- [x] 13. Mark spec completed
  - Set `Completed` date in `requirements.md`, `design.md`, and `tasks.md`
  - _Requirements: all_
