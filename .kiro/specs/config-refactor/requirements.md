# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-23
- Completed:

## Title: Config Refactor

## Introduction

This feature replaces the existing `ConfigManager` + `PipelineConfig` pair with `AppConfig` — a layered, Pydantic-validated, domain-structured config object. Configuration is assembled once at startup by deep-merging up to three YAML files in priority order, then CLI overrides are applied as direct attribute assignments. Volatile per-run parameters (source path, work directory, force flag, etc.) are passed as plain keyword arguments to `_build_registry` and stored as typed fields on `JobPhaseResult`. All downstream phases read both config and volatile runtime values from `job_result.*`. This eliminates mixed concerns in `PipelineConfig`, enables per-key overrides without copying the full config file, and concentrates strategy resolution to a single, validated moment.

## Glossary

- **AppConfig**: The new Pydantic-validated config model that replaces both `ConfigManager` and `PipelineConfig`. Loaded once at startup; read-only by convention after CLI overrides are applied.
- **Volatile parameters**: Per-run values (`source`, `work_dir`, `force`, `cleanup`, `no_metrics`) passed as plain keyword arguments to `_build_registry` and forwarded only to `JobPhase`. Downstream phases read these as typed fields on `JobPhaseResult`.
- **Loader** (`load_app_config`): The function responsible for discovering and deep-merging the three YAML layers and returning a validated `AppConfig`.
- **Deep_Merge** (`_deep_merge`): The pure function that recursively merges two config dicts according to the defined rules (scalars: override wins; dicts: recursive merge; lists: full replacement).
- **EncodingConfig**: The `AppConfig` sub-model holding encoding settings: quality targets, strategies, optimization flag, parallelism, metrics sampling, visual hash, crop params, and strategy-selection tolerance.
- **ExtractionConfig**: The `AppConfig` sub-model holding stream filter settings: include and exclude regex patterns.
- **ChunkingConfig**: The `AppConfig` sub-model holding chunking settings: mode, scene threshold, and minimum scene length.
- **AudioConfig**: The `AppConfig` sub-model holding audio output settings: conversion filter regex, per-layout profiles, and per-run codec/bitrate overrides.
- **ProfileConfig**: Pydantic model replacing the plain `@dataclass EncodingProfile`. Holds codec reference, description, and extra FFmpeg args.
- **Strategy**: Resolved encoding strategy object carrying preset, profile, codec config, and profile args. Produced by resolving raw strategy strings.
- **QualityTarget**: Parsed quality target object carrying metric, statistic, and threshold value.
- **Bundled_Default**: The `default_config.yaml` file shipped with the package, always present.
- **User_Home_Config**: Optional config at `~/.config/pyqenc/config.yaml`.
- **CWD_Config**: Optional config at `./pyqenc.yaml` in the current working directory.
- **Phase**: A pipeline execution unit (JobPhase, ExtractionPhase, ChunkingPhase, etc.) that receives `AppConfig` in its constructor and volatile runtime values via `job_result.*`.
- **JobPhaseResult**: The result type returned by `JobPhase`, which carries a `config: AppConfig` field used by all downstream phases.

---

## Requirements

### Requirement 1: Layered YAML Config Loading

**User Story:** As a user, I want to override only specific config keys in a local file, so that I can customise encoding settings without copying the entire default config.

#### Acceptance Criteria

1. THE Loader SHALL load the Bundled_Default YAML file unconditionally as the base layer.
2. WHEN a User_Home_Config file exists, THE Loader SHALL load it as the second layer on top of the base.
3. WHEN a CWD_Config file exists, THE Loader SHALL load it as the third layer on top of the previous result.
4. THE Loader SHALL apply layers in priority order: Bundled_Default < User_Home_Config < CWD_Config, so that a higher-priority layer wins on any conflicting key.
5. WHEN neither User_Home_Config nor CWD_Config exists, THE Loader SHALL produce an `AppConfig` equivalent to loading only the Bundled_Default.
6. THE Loader SHALL return a fully validated `AppConfig` instance after merging all present layers.

---

### Requirement 2: Deep-Merge Semantics

**User Story:** As a user, I want dict-typed config sections to merge key-by-key, so that a local override file touching one codec does not erase all other codecs.

#### Acceptance Criteria

1. WHEN both the base dict and the override dict contain a scalar-valued key, THE Deep_Merge SHALL use the override value for that key in the result.
2. WHEN the base dict contains a scalar-valued key that is absent from the override dict, THE Deep_Merge SHALL preserve the base value for that key in the result.
3. WHEN both the base dict and the override dict contain a dict-valued key, THE Deep_Merge SHALL merge those sub-dicts recursively using the same rules.
4. WHEN both the base dict and the override dict contain a list-valued key, THE Deep_Merge SHALL use the override list in full and discard the base list entirely.
5. WHEN only the base dict contains a list-valued key, THE Deep_Merge SHALL preserve the base list in the result.

---

### Requirement 3: AppConfig Pydantic Validation

**User Story:** As a developer, I want all config values validated by Pydantic at load time, so that invalid config is caught early with a clear error rather than causing a runtime failure deep in the pipeline.

#### Acceptance Criteria

1. WHEN `AppConfig.model_validate()` is called with a valid merged dict, THE AppConfig SHALL produce a fully populated model with all sub-models (`ExtractionConfig`, `ChunkingConfig`, `EncodingConfig`, `AudioConfig`, `codecs`, `profiles`) correctly populated.
2. WHEN `AppConfig.model_validate()` is called with a dict that violates any field constraint, THE AppConfig SHALL raise a `ValidationError` describing the offending field.
3. THE AppConfig SHALL validate and resolve all `EncodingConfig.strategies` raw strings into `Strategy` objects exactly once, during the `model_validator(mode='after')` pass, using the `codecs` and `profiles` from the same `AppConfig`.
4. THE AppConfig SHALL validate and resolve all `EncodingConfig.quality_targets` raw strings into `QualityTarget` objects exactly once, during the `model_validator(mode='after')` pass.
5. WHEN an `EncodingConfig.strategies` raw string references a profile name or preset that does not exist in the `AppConfig.codecs` / `AppConfig.profiles` maps, THE AppConfig SHALL raise a `ValidationError` identifying the unknown reference.
6. WHEN an `EncodingConfig.quality_targets` raw string uses an unrecognised metric name or statistic, THE AppConfig SHALL raise a `ValidationError` identifying the invalid target string.

---

### Requirement 4: AppConfig Structure and Field Mapping

**User Story:** As a developer, I want config concerns separated into typed sub-models, so that each phase can read only the section relevant to it without navigating a flat bag of unrelated fields.

#### Acceptance Criteria

1. THE AppConfig SHALL expose an `extraction` field of type `ExtractionConfig` containing `include: str | None` and `exclude: str | None`.
2. THE AppConfig SHALL expose a `chunking` field of type `ChunkingConfig` containing `mode: ChunkingMode`, `scene_threshold: float`, and `min_scene_length: int`.
3. THE AppConfig SHALL expose an `encoding` field of type `EncodingConfig` containing `quality_targets`, `strategies`, `optimize`, `max_parallel`, `metrics_sampling`, `visual_hash`, `strategy_selection_tolerance`, and `crop_params`.
4. THE AppConfig SHALL expose an `audio` field of type `AudioConfig` containing `convert_filter`, `profiles`, `audio_codec`, and `audio_base_bitrate`.
5. THE AppConfig SHALL expose a `codecs` field of type `dict[str, CodecConfig]` and a `profiles` field of type `dict[str, ProfileConfig]`.
6. THE AppConfig SHALL map the existing `default_config.yaml` keys to the new namespace: `default_targets` → `encoding.quality_targets`, `default_strategies` → `encoding.strategies`, `metrics.sampling` → `encoding.metrics_sampling`, `streams.include` → `extraction.include`, `streams.exclude` → `extraction.exclude`, `audio_output.convert_filter` → `audio.convert_filter`, `audio_output.profiles` → `audio.profiles`.
7. THE Bundled_Default YAML SHALL be updated to reflect the new namespace structure while preserving all existing default values.

---

### Requirement 5: Volatile Parameter Handling

**User Story:** As a developer, I want volatile per-run parameters separated from the YAML-sourced config and not accessible as a named class, so that they land in typed fields on `JobPhaseResult` and all other phases read them from there.

#### Acceptance Criteria

1. THE `_build_registry` function SHALL accept `source: Path`, `work_dir: Path`, `force: bool`, `cleanup: CleanupLevel`, `no_metrics: bool` as plain keyword arguments and forward them only to `JobPhase.__init__`.
2. THE `JobPhase.__init__` SHALL accept `source`, `work_dir`, `force`, `cleanup`, `no_metrics` as plain keyword arguments and store them for use during `run()`.
3. THE `JobPhaseResult` SHALL carry typed fields `work_dir: Path`, `source: Path`, `cleanup: CleanupLevel`, `no_metrics: bool` populated from the stored volatile values when the phase completes.
4. NO phase other than `JobPhase` SHALL receive volatile parameters in its constructor; all such phases SHALL read volatile values exclusively from `self._job.result.*`.

---

### Requirement 6: CLI Config Assembly

**User Story:** As a developer, I want the CLI to assemble the final `AppConfig` by loading layers then applying overrides as direct attribute assignments, so that the override logic is visible and auditable in one place.

#### Acceptance Criteria

1. WHEN the CLI processes any pipeline subcommand, THE CLI SHALL call `load_app_config()` to produce the base `AppConfig` before calling `_build_registry`.
2. WHEN `--targets` is provided, THE CLI SHALL assign the parsed quality target strings to `config.encoding.quality_targets` after `load_app_config()` returns.
3. WHEN `--strategies` is provided, THE CLI SHALL assign the parsed strategy strings to `config.encoding.strategies` after `load_app_config()` returns.
4. WHEN `--sampling` is provided, THE CLI SHALL assign the integer value to `config.encoding.metrics_sampling` after `load_app_config()` returns.
5. WHEN `--max-parallel` is provided and differs from the default, THE CLI SHALL assign the value to `config.encoding.max_parallel` after `load_app_config()` returns.
6. WHEN `--include` is provided, THE CLI SHALL assign the regex string to `config.extraction.include` after `load_app_config()` returns.
7. WHEN `--exclude` is provided, THE CLI SHALL assign the regex string to `config.extraction.exclude` after `load_app_config()` returns.
8. WHEN `--audio-convert` is provided, THE CLI SHALL assign the regex string to `config.audio.convert_filter` after `load_app_config()` returns.
9. WHEN `--audio-codec` is provided, THE CLI SHALL assign the codec name to `config.audio.audio_codec` after `load_app_config()` returns.
10. WHEN `--audio-bitrate` is provided, THE CLI SHALL assign the bitrate string to `config.audio.audio_base_bitrate` after `load_app_config()` returns.
11. WHEN `--crop` is provided, THE CLI SHALL assign the parsed `CropParams` to `config.encoding.crop_params` after `load_app_config()` returns.
12. WHEN `--chunking` is provided, THE CLI SHALL assign the resolved `ChunkingMode` to `config.chunking.mode` after `load_app_config()` returns.
13. WHEN `--all-strategies` is provided, THE CLI SHALL assign `False` to `config.encoding.optimize` after `load_app_config()` returns.
14. AFTER all CLI overrides are applied, THE CLI SHALL call `_build_registry` passing the `AppConfig` and all volatile parameters (`source`, `work_dir`, `force`, `cleanup`, `no_metrics`) as plain keyword arguments.

---

### Requirement 7: Phase Registry and Constructor Signatures

**User Story:** As a developer, I want phases to receive `AppConfig` at construction and volatile runtime values via `JobPhaseResult`, so that volatile state is centralised in one place and phase constructors stay simple.

#### Acceptance Criteria

1. THE `_build_registry` function SHALL accept `(config: AppConfig, source: Path, work_dir: Path, force: bool, cleanup: CleanupLevel, no_metrics: bool, collector: MetricsCollector)` and forward volatile params only to `JobPhase`.
2. THE `JobPhase` constructor SHALL accept `(config: AppConfig, phases, *, source: Path, work_dir: Path, force: bool, cleanup: CleanupLevel, no_metrics: bool, collector: MetricsCollector)` and store all volatile values.
3. THE `JobPhaseResult` SHALL carry a `config: AppConfig` field, `work_dir: Path`, `source: Path`, `cleanup: CleanupLevel`, and `no_metrics: bool` — all populated from `JobPhase`'s stored values when the phase completes successfully.
4. ALL phases other than `JobPhase` SHALL read `AppConfig` from `job_result.config` (the value stored on `JobPhaseResult`) rather than from the constructor argument.
5. WHEN a phase needs the source video path or working directory, THE phase SHALL read it from `self._job.result.source` and `self._job.result.work_dir`.
6. WHEN a phase needs the cleanup level, THE phase SHALL read it from `self._job.result.cleanup`.
7. WHEN a phase needs the no-metrics flag, THE phase SHALL read it from `self._job.result.no_metrics`.

---

### Requirement 8: Config Access Path Mapping in Phases

**User Story:** As a developer, I want every phase to access config values through the new `AppConfig` sub-model paths, so that all references are consistent and the old flat `PipelineConfig` fields are fully removed.

#### Acceptance Criteria

1. WHEN a phase accesses stream filter settings, THE phase SHALL read `self._config.extraction.include` and `self._config.extraction.exclude`.
2. WHEN a phase accesses encoding quality settings, THE phase SHALL read `self._config.encoding.resolved_targets` and `self._config.encoding.resolved_strategies`.
3. WHEN a phase accesses encoding performance settings, THE phase SHALL read `self._config.encoding.max_parallel`, `self._config.encoding.metrics_sampling`, and `self._config.encoding.visual_hash`.
4. WHEN a phase accesses optimization settings, THE phase SHALL read `self._config.encoding.optimize` and `self._config.encoding.strategy_selection_tolerance`.
5. WHEN a phase accesses source path or working directory, THE phase SHALL read `self._job.result.source` and `self._job.result.work_dir`.
6. WHEN a phase accesses cleanup level, THE phase SHALL read `self._job.result.cleanup`.
7. WHEN a phase accesses audio conversion settings, THE phase SHALL read `self._job.result.config.audio.convert_filter`, `self._job.result.config.audio.audio_codec`, and `self._job.result.config.audio.audio_base_bitrate`.

---

### Requirement 9: Removal of Legacy Types

**User Story:** As a developer, I want `ConfigManager`, `PipelineConfig`, `find_config_source`, and the plain-dataclass `AudioConversionProfile` / `AudioOutputConfig` / `EncodingProfile` removed, so that there is no ambiguity about which config system is authoritative.

#### Acceptance Criteria

1. THE `ConfigManager` class SHALL be deleted from `config.py`.
2. THE `find_config_source()` function SHALL be deleted from `config.py`.
3. THE `PipelineConfig` model SHALL be deleted from `models.py`.
4. THE plain-dataclass `AudioConversionProfile`, `AudioOutputConfig`, and `EncodingProfile` SHALL be removed from `config.py` and replaced by their Pydantic equivalents in `app_config.py`.
5. IF any external module imported `ConfigManager`, `find_config_source`, or `PipelineConfig`, THE module SHALL be updated to use the new equivalents with no backward-compatibility shim remaining.

---

### Requirement 10: Strategy Resolution Determinism

**User Story:** As a developer, I want strategy resolution to produce the same result on every access, so that encoding phases always receive a consistent, deduplicated list of strategies regardless of how many times they query it.

#### Acceptance Criteria

1. WHEN `AppConfig` is validated, THE AppConfig SHALL resolve `encoding.strategies` raw strings to `Strategy` objects exactly once and cache the result in a private field.
2. WHEN `encoding.resolved_strategies` is accessed multiple times on the same `AppConfig` instance, THE AppConfig SHALL return the same list with the same objects in the same order on every call.
3. THE resolved strategies list SHALL be deduplicated by `(preset, profile)` pair, retaining the first occurrence when duplicates appear.

---

### Requirement 11: `AppConfig` Serialization Round-Trip

**User Story:** As a developer writing tests, I want to serialize an `AppConfig` to a dict and reload it, so that I can construct known configs in test fixtures without relying on YAML files.

#### Acceptance Criteria

1. WHEN `AppConfig.model_dump()` is called on a valid instance, THE AppConfig SHALL produce a dict that can be passed back to `AppConfig.model_validate()` and yield an equivalent `AppConfig` with the same field values throughout the model tree.
2. THE round-trip SHALL preserve `EncodingConfig.quality_targets` and `EncodingConfig.strategies` as their raw string forms so that re-validation triggers resolution again correctly.
