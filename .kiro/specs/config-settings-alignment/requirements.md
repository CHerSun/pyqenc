# Requirements Document: Config Settings Alignment

<!-- markdownlint-disable MD024 -->

- Created: 2026-08-12
- Completed: 2026-08-29

## Cross-Reference Notes

This spec builds directly on top of `config-refactor` (Created: 2026-06-23, Completed: 2026-07-06), which established `AppConfig` with Pydantic sub-models and layered YAML loading. That spec is the authority on the loading mechanism; this spec is the authority on the **naming and structure** of every field within `AppConfig` and the CLI.

| Spec | Relationship |
|------|--------------|
| `config-refactor` | **Extended.** Established `AppConfig`, sub-models, and `default_config.yaml` structure. This spec renames fields, restructures `AudioConfig`, adds `MeasurementConfig`, removes `crop_params` from `AppConfig`, and revises the YAML. All field paths in `config-refactor` that reference the old names are superseded by this spec. |

---

## Title: Config Settings Alignment

## Introduction

`AppConfig` was implemented in the `config-refactor` spec. However the field names chosen at that time were inherited from the old `PipelineConfig` flat bag and do not match the YAML keys or CLI argument names consistently. There are also structural issues: audio config uses per-channel-layout profiles (an overengineered model for what is effectively one codec choice); `crop_params` is a volatile per-run value that leaked into the persistent config model; `metrics_sampling` lives directly on `EncodingConfig` rather than in a dedicated measurement sub-model; and several fields have verbose or inconsistent names.

This spec aligns the Python model field names, YAML keys, and CLI argument names into a coherent, consistent schema. The rule is: config file is descriptive and sectioned; CLI is flat and terse; both name the same concept the same way (with the CLI being the shorter form where needed).

---

## Requirements

### Requirement 1: Rename `EncodingConfig` fields

**User Story:** As a user reading `default_config.yaml`, I want field names that are concise and unambiguous within their section, so that I do not have to decode redundant prefixes or legacy names.

#### Acceptance Criteria

1. `EncodingConfig.quality_targets` SHALL be renamed to `EncodingConfig.targets` in both the Python model and the YAML key.
2. `EncodingConfig.max_parallel` SHALL be renamed to `EncodingConfig.concurrency` in both the Python model and the YAML key.
3. `EncodingConfig.metrics_sampling` SHALL be removed from `EncodingConfig` and moved to a new `MeasurementConfig` sub-model (see Requirement 3).
4. `EncodingConfig.strategy_selection_tolerance` SHALL be renamed to `EncodingConfig.optimize_tolerance` in both the Python model and the YAML key.
5. `EncodingConfig.crop_params` SHALL be removed from `EncodingConfig` entirely (see Requirement 4).
6. All existing references to old field names throughout `app_config.py`, `cli.py`, `phase.py`, all phase files, `api.py`, and `orchestrator.py` SHALL be updated to use the new names.
7. `DEFAULT_MAX_PARALLEL` constant in `constants.py` SHALL be renamed to `DEFAULT_CONCURRENCY`.

---

### Requirement 2: Restructure `AudioConfig` — flat settings, no profiles

**User Story:** As a user, I want audio conversion settings expressed as a single codec, a single per-channel bitrate, and a file extension, so that I do not have to maintain per-layout profile entries for what is effectively one global choice.

#### Acceptance Criteria

1. `AudioConfig` SHALL expose the following flat fields, replacing the existing `profiles` dict, `audio_codec` override, and `audio_base_bitrate` override:
   - `convert_pattern: str` — regex selecting processed audio files for delivery conversion (was `convert_filter`)
   - `codec: str` — the ffmpeg audio codec name (default `"aac"`)
   - `bitrate_per_channel: str` — per-channel bitrate string (default `"96k"`); scaled at runtime by channel count (×2 for 2.0, ×6 for 5.1, ×8 for 7.1)
   - `extension: str` — output file extension (default `".m4a"`)
2. `AudioConversionProfile` class SHALL be deleted from `app_config.py`.
3. `AudioConfig.profiles` dict field SHALL be deleted.
4. `AudioConfig.audio_codec` override field SHALL be deleted.
5. `AudioConfig.audio_base_bitrate` override field SHALL be deleted.
6. The audio phase code that previously read per-layout profiles SHALL be updated to compute the final bitrate by parsing `bitrate_per_channel` and multiplying by the channel count of each stream.
7. The YAML `audio:` section SHALL reflect the flat structure: `convert_pattern`, `codec`, `bitrate_per_channel`, `extension`.

---

### Requirement 3: Add top-level `MeasurementConfig` sub-model to `AppConfig`

**User Story:** As a developer, I want measurement-related settings in their own top-level config section, so that they apply uniformly to encoding quality checks, merge verification, and the standalone measure command without being scoped to encoding alone.

#### Acceptance Criteria

1. A new `MeasurementConfig(BaseModel)` SHALL be defined in `app_config.py` with at minimum the field `sampling: int` (default `DEFAULT_METRICS_SAMPLING`).
2. `AppConfig` SHALL expose a **top-level** `measurement: MeasurementConfig` field (with `default_factory=MeasurementConfig`), alongside `extraction`, `chunking`, `encoding`, and `audio` — NOT nested under `EncodingConfig`.
3. All code that previously read `config.encoding.metrics_sampling` SHALL be updated to read `config.measurement.sampling`.
4. The CLI `--sampling` argument SHALL map to `config.measurement.sampling`.
5. The YAML SHALL include a top-level `measurement:` section containing `sampling`, at the same level as `encoding:` and `audio:`.

---

### Requirement 4: Remove `crop_params` from `AppConfig`

**User Story:** As a developer, I want volatile per-run parameters absent from the persistent config model, so that there is no ambiguity between what is configurable at the file level and what is supplied per invocation.

#### Acceptance Criteria

1. `EncodingConfig.crop_params` SHALL be removed from `AppConfig` and from `EncodingConfig`.
2. `crop_params` SHALL remain available as a CLI argument (`--crop`) producing a `CropParams | None` value.
3. `crop_params` SHALL be passed as a plain volatile keyword argument through `_build_registry` to `JobPhase`, stored on `JobPhaseResult.crop` (which already exists).
4. All phase code that previously read `self._job.result.config.encoding.crop_params` SHALL be updated to read `self._job.result.crop` instead.
5. No YAML default for `crop_params` SHALL exist (it is purely a per-run CLI/API value).

---

### Requirement 5: Rename CLI arguments and their dest attributes

**User Story:** As a user of the CLI, I want argument names that are consistent with the config file keys they override, so that I can predict the config key from the CLI flag and vice versa.

#### Acceptance Criteria

1. `--chunking` (dest `chunking`) SHALL be renamed to `--chunking-mode` (dest `chunking_mode`), mapping to `config.chunking.mode`.
2. `--all-strategies` (dest `all_strategies`) SHALL be renamed to `--no-optimize` (dest `no_optimize`), setting `config.encoding.optimize = False`.
3. `--max-parallel` (dest `max_parallel`) SHALL be renamed to `--concurrency` (dest `concurrency`), mapping to `config.encoding.concurrency`.
4. `--targets` dest SHALL be renamed from `quality_target` to `targets`, mapping to `config.encoding.targets`.
5. `--sampling` dest SHALL remain `sampling`; `_build_config` SHALL assign it to `config.measurement.sampling`.
6. `--audio-convert`, `--audio-codec`, and `--audio-bitrate` CLI arguments SHALL be removed entirely. Audio settings are config-only.
7. `_add_audio_convert_arguments()` helper function SHALL be deleted from `cli.py`.
8. All subcommands that called `_add_audio_convert_arguments()` SHALL have that call removed.
9. `_build_config()` SHALL remove the audio override block (lines applying `audio_convert`, `audio_codec`, `audio_bitrate` to config).

---

### Requirement 6: Update `default_config.yaml` — reflect all `AppConfig` fields and revise comments

**User Story:** As a user customising config, I want the default config file to show every available setting with a short inline comment, so that I can copy it and know exactly what I can change without reading source code.

#### Acceptance Criteria

1. Every field in `AppConfig`, `ExtractionConfig`, `ChunkingConfig`, `EncodingConfig`, `MeasurementConfig`, and `AudioConfig` SHALL appear in `default_config.yaml` with its default value.
2. Comments in `default_config.yaml` SHALL be short (single-line inline where possible) and focused on what the value does, not on how the system works internally.
3. Long multi-paragraph block comments SHALL be replaced with brief section headers and inline `#` annotations on or next to each setting.
4. The strategy pattern syntax guide MAY be retained as a compact block comment, but SHALL be shortened to the essential examples only.
5. The `audio:` section SHALL reflect the new flat fields: `convert_pattern`, `codec`, `bitrate_per_channel`, `extension`.
6. `encoding.targets` SHALL appear (was `encoding.quality_targets`).
7. `encoding.concurrency` SHALL appear (was `encoding.max_parallel`).
8. `encoding.optimize_tolerance` SHALL appear (was `encoding.strategy_selection_tolerance`).
9. `measurement.sampling` SHALL appear as a **top-level** `measurement:` section (same level as `encoding:`, not nested inside it).
10. There SHALL be no entry for `crop_params` in the YAML (it is volatile and not config-file-settable).

---

### Requirement 7: Single source of truth for defaults — YAML only

**User Story:** As a developer, I want operational default values defined in exactly one place (the bundled YAML), so that there is no risk of the Python model silently supplying a different default than what the user sees in the config file.

#### Acceptance Criteria

1. All `AppConfig` sub-model fields that carry a value (non-nullable, non-sentinel) SHALL be declared as **required** in Pydantic — no `= value` or `= Field(default=...)` default in Python. The bundled YAML always supplies these values.
2. The only exceptions to AC1 are fields whose Python default is a structural sentinel rather than an operational value: `include: str | None = None`, `exclude: str | None = None`, and `crop_params` (already removed by Requirement 4). `None` here means "not set" — it is not a duplicate of the YAML.
3. `DEFAULT_*` constants in `constants.py` whose sole purpose is to hold a default value for a Pydantic field or CLI argument SHALL be deleted. Constants used in logic (comparisons, calculations) or used in multiple independent contexts SHALL be retained.
4. CLI arguments that map to a config field SHALL use `default=None` (meaning "not provided, do not override config"), not a hardcoded value. The one current violation — `--max-parallel` / `--concurrency` defaulting to `DEFAULT_MAX_PARALLEL` — SHALL be fixed as part of this requirement.
5. CLI arguments that do **not** map to any `AppConfig` field (e.g. `--log-level`, `--work-dir`) MAY retain their own hardcoded defaults since there is no config field to duplicate.
6. After this change, `AppConfig.model_validate()` called with an empty dict SHALL raise `ValidationError` (required fields missing), not silently produce a config with hardcoded fallbacks.
