# Bugfix Requirements Document — Project Cleanup

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-11

## Introduction

The pyqenc project has accumulated code smells, structural inconsistencies, and bloat over a series of rapid feature specs. The project is pre-alpha with no public API commitment, so this is the right moment for a thorough cleanup before the first release. The goal is a codebase that is clean, internally consistent, and free of patterns that will cause pain during future development.

This document captures the defective patterns currently present and the correct state each must reach. It also requires that all existing tests pass after cleanup, and that any tests covering removed or legacy code are removed alongside that code.

---

## Bug Analysis

### Current Behavior (Defect)

**Section 1 — `getattr` on typed objects**

1.1 WHEN phase code accesses `force_wipe`, `crop`, `job`, `audio`, `chunks`, `encoded`, `selected_strategies`, `strategy_results` on typed `PhaseResult` subclasses, THEN the system uses `getattr(result, "field", default)` instead of direct attribute access, making refactoring unsafe and hiding type errors at dev-time.

1.2 WHEN `cli.py` accesses `args.include`, `args.exclude`, `args.audio_convert`, `args.audio_codec`, `args.audio_bitrate` on an `argparse.Namespace`, THEN the system uses `getattr(args, "field", None)` even though these arguments are always registered on the relevant subparser and are always present on the namespace.

**Section 2 — `runtime_checkable` Protocol instead of inheritance**

2.1 WHEN `Phase` is defined in `phase.py`, THEN the system decorates it with `@runtime_checkable` and uses structural duck-typing, instead of having every phase class inherit from `Phase` directly so the type checker verifies conformance at definition time.

**Section 3 — Imports not at the top of the file**

3.1 WHEN phase `__init__` methods need their dependency phase types (e.g. `AudioPhase`, `EncodingPhase`, `JobPhase`), THEN the system defers those imports inside `__init__` bodies citing circular imports, instead of restructuring module boundaries to eliminate the cycle.

3.2 WHEN `api.py` standalone functions need their phase types (e.g. `ExtractionPhase`, `ChunkingPhase`), THEN the system defers those imports inside each function body, instead of placing them at the top of the file.

3.3 WHEN `audio.py` `_build_audio_engine` and `AudioPhase._get_convert_filter` need `ConfigManager`, THEN the system defers that import inside the function body, instead of placing it at the top of the file.

3.4 WHEN `merge.py` `_execute_merge` and `MergePhase.run` need `TimeKey`, THEN the system defers that import inside the method body, instead of placing it at the top of the file.

3.5 WHEN `visualization.py` `QualityEvaluator` needs `VideoMetadata`, THEN the system defers that import inside a method body, instead of placing it at the top of the file.

3.6 WHEN `encoding.py` `_probe_resolution` needs `json`, THEN the system defers that import inside the function body, instead of placing it at the top of the file.

**Section 4 — `__all__` in internal files**

4.1 WHEN `phase.py` defines `__all__`, THEN the system exports an explicit list from an internal mechanics file that is never imported as a public API surface.

4.2 WHEN `metrics.py` defines `__all__` with task-comment annotations (`# Added in task 7`, `# noqa: F401` with task references), THEN the system maintains a stale export list with leftover task scaffolding comments that no longer reflect the module's actual state.

4.3 WHEN `__init__.py` defines `__all__`, THEN the system re-exports symbols from an internal package init that is not a public distribution API.

**Section 5 — Unused imports suppressed with `# noqa: F401`**

5.1 WHEN `metrics.py` imports `math`, `time as _time`, `dataclass`, `field`, `datetime`, `Path`, `Protocol`, `yaml` with `# noqa: F401` comments, THEN the system suppresses linter warnings for imports that are actually used in the same file but were scaffolded incrementally with task-tracking comments, leaving stale noise in the import block.

5.2 WHEN `models.py` imports `ConfigDict` with `# noqa: F401 (ConfigDict used in PipelineConfig)`, THEN the system suppresses a linter warning with an inline comment instead of verifying whether the import is genuinely needed.

**Section 5b — Other `# noqa` suppressions**

5b.1 WHEN `alive.py` has two `advance` function definitions with `# noqa: E501` (line too long), THEN the system suppresses a formatting rule instead of reformatting the line.

5b.2 WHEN `disk_space.py` accesses `video._frame_count` and `video._file_size_bytes` with `# noqa: SLF001` (private member access), THEN the system suppresses a rule that is flagging a genuine design smell: `disk_space.py` reaches into `VideoMetadata`'s private cache to avoid triggering lazy-load probes, instead of `VideoMetadata` exposing a dedicated method for cache-only reads.

5b.3 WHEN `ffmpeg_runner.py` and `metrics.py` use `except Exception as exc:  # noqa: BLE001` (blind exception catch), THEN the system suppresses the rule rather than either narrowing the exception type or configuring a `per-file-ignores` entry in `ruff.toml` with an explicit justification comment in the config.

**Section 6 — Excessive `| None` arguments**

6.1 WHEN `_minimal_config` in `api.py` accepts `quality_targets: list[QualityTarget] | None`, `strategies: list[Strategy] | None`, `include: str | None`, `exclude: str | None`, `audio_convert: str | None`, `audio_codec: str | None`, `audio_base_bitrate: str | None`, THEN the system allows callers to pass `None` for fields where the flow always provides a value or where an empty list / empty string is the correct sentinel, spreading `None`-checks through the call chain.

6.2 WHEN `PipelineConfig` fields carry `| None` for values that are always resolved before the config is used (e.g. `include`, `exclude` regex strings where `None` and `""` have distinct meaning but the distinction is not enforced), THEN the system allows ambiguous `None` to propagate into phase logic.

#### `| None` Audit Table

Every `| None`-typed parameter and field in production code (tests excluded), with a verdict on whether `None` is justified.

Legend — **Keep?**: ✅ keep `| None` (semantically meaningful) · ❌ remove (flow always provides a value or a non-None sentinel is correct) · ⚠️ needs clarification before deciding.

| Location | Parameter / Field | Type | Always initialised by flow? | Clear reason for `None`? | Keep? | Notes |
|---|---|---|---|---|---|---|
| `api.py` · `_minimal_config` | `quality_targets` | `list[QualityTarget] \| None` | No — callers may omit | `None` → use `[]`; empty list is the correct sentinel | ❌ | Replace default with `[]`; callers that want "no targets" already pass nothing |
| `api.py` · `_minimal_config` | `strategies` | `list[Strategy] \| None` | No — callers may omit | `None` means "resolve defaults from config"; distinct from `[]` (all combos) | ✅ | Semantically distinct: `None` = config default, `[]` = all combos |
| `api.py` · `_minimal_config` | `include` | `str \| None` | No — callers may omit | `None` means "no include filter"; `""` would match nothing (wrong sentinel) | ✅ | `None` is the correct "no filter" sentinel here |
| `api.py` · `_minimal_config` | `exclude` | `str \| None` | No — callers may omit | Same as `include` | ✅ | `None` is the correct "no filter" sentinel here |
| `api.py` · `_minimal_config` | `audio_convert` | `str \| None` | No — callers may omit | `None` = use config default; `""` = convert nothing (distinct meanings) | ✅ | Semantically distinct; must be documented in docstring |
| `api.py` · `_minimal_config` | `audio_codec` | `str \| None` | No — callers may omit | `None` = use profile default; non-None = override | ✅ | Override pattern; `None` is the correct "no override" sentinel |
| `api.py` · `_minimal_config` | `audio_base_bitrate` | `str \| None` | No — callers may omit | `None` = use profile default; non-None = override | ✅ | Same as `audio_codec` |
| `models.py` · `PipelineConfig` | `crop_params` | `CropParams \| None` | No — resolved by `JobPhase` at runtime | `None` = auto-detect; explicit `CropParams` = manual | ✅ | Intentional: `None` triggers auto-detection in `JobPhase` |
| `models.py` · `PipelineConfig` | `include` | `str \| None` | No — user may not supply | `None` = no filter (same as `_minimal_config`) | ✅ | Correct sentinel |
| `models.py` · `PipelineConfig` | `exclude` | `str \| None` | No — user may not supply | `None` = no filter | ✅ | Correct sentinel |
| `models.py` · `PipelineConfig` | `audio_convert` | `str \| None` | No — user may not supply | `None` = use config default; `""` = convert nothing | ✅ | Must be documented; regression prevention item 3.6 |
| `models.py` · `PipelineConfig` | `audio_codec` | `str \| None` | No — user may not supply | `None` = no override | ✅ | Correct sentinel |
| `models.py` · `PipelineConfig` | `audio_base_bitrate` | `str \| None` | No — user may not supply | `None` = no override | ✅ | Correct sentinel |
| `models.py` · `AudioMetadata` | `codec` | `str \| None` | No — parsed from ffprobe; may be absent | Track may genuinely lack codec info | ✅ | Reflects real-world data absence |
| `models.py` · `AudioMetadata` | `channels` | `int \| None` | No — parsed from ffprobe; may be absent | Same | ✅ | Reflects real-world data absence |
| `models.py` · `AudioMetadata` | `language` | `str \| None` | No — parsed from ffprobe; may be absent | Same | ✅ | Reflects real-world data absence |
| `models.py` · `AudioMetadata` | `title` | `str \| None` | No — parsed from ffprobe; may be absent | Same | ✅ | Reflects real-world data absence |
| `models.py` · `AudioMetadata` | `duration_seconds` | `float \| None` | No — parsed from ffprobe; may be absent | Same | ✅ | Reflects real-world data absence |
| `models.py` · `AudioMetadata` | `start_timestamp` | `float \| None` | No — parsed from ffprobe; may be absent | Same | ✅ | Reflects real-world data absence |
| `models.py` · `VideoMetadata` (private attrs) | `_duration_seconds`, `_frame_count`, `_fps`, `_resolution`, `_pix_fmt`, `_file_size_bytes` | `T \| None` | No — lazy-loaded on first access | `None` = not yet probed; sentinel for lazy-load pattern | ✅ | Core of the lazy-loading design; cannot be removed |
| `state.py` · `JobState` | `crop` | `CropParams \| None` | No — set after crop detection runs | `None` = crop detection not yet run | ✅ | Intentional; detection happens in `JobPhase._resolve_crop` |
| `state.py` · `ExtractionParams` | `include` | `str \| None` | No — user may not supply | Same as `PipelineConfig.include` | ✅ | Persisted filter state; `None` = no filter |
| `state.py` · `ExtractionParams` | `exclude` | `str \| None` | No — user may not supply | Same | ✅ | Persisted filter state |
| `state.py` · `OptimizationParams` | `crop` | `CropParams \| None` | No — may not have run yet | `None` = optimization not yet run with crop | ✅ | Persisted state; absence is meaningful |
| `state.py` · `OptimizationParams` | `metrics_sampling` | `int \| None` | Yes — always written by the code | No — `None` only exists for old YAML files; no backward compat needed | ❌ | Make `int` with a required value; treat missing field in YAML as a mismatch (re-run) |
| `state.py` · `EncodingParams` | `crop` | `CropParams \| None` | No — may not have run yet | `None` = encoding not yet run with crop | ✅ | Persisted state; absence is meaningful |
| `state.py` · `ChunkingParams` | `chunking_mode` | `str \| None` | Yes — always written by the code | No — `None` only exists for old YAML files; no backward compat needed | ❌ | Make `str` (required); treat missing field in YAML as a mismatch (re-run) |
| `state.py` · `MergeParams` | `metrics_sampling` | `int \| None` | Yes — always written by the code | No — `None` only exists for old YAML files; no backward compat needed | ❌ | Make `int` (required); treat missing field in YAML as a mismatch (re-run) |
| `state.py` · `AudioParams` | `audio_codec` | `str \| None` | No — user may not supply | `None` = no override persisted | ✅ | Persisted state; `None` = "was not set" |
| `state.py` · `AudioParams` | `audio_base_bitrate` | `str \| None` | No — user may not supply | Same | ✅ | Persisted state |
| `phase.py` · `PhaseResult` | `error` | `str \| None` | No — only set on failure | `None` = no error (success path) | ✅ | Standard error-or-None pattern |
| `phase.py` · `Phase` (Protocol) | `result` | `PhaseResult \| None` | No — set after first `scan()`/`run()` | `None` = phase not yet executed | ✅ | Lifecycle sentinel; checked by orchestrator |
| `orchestrator.py` · `PipelineResult` | `error` | `str \| None` | No — only set on failure | Same as `PhaseResult.error` | ✅ | Standard error-or-None pattern |
| `phases/job.py` · `JobPhaseResult` | `job` | `JobState \| None` | No — `None` on failure paths | `None` = job initialisation failed | ✅ | Failure path; downstream phases check `is_complete` |
| `phases/job.py` · `JobPhaseResult` | `crop` | `CropParams \| None` | No — `None` on failure or scan-without-yaml | `None` = not yet resolved | ✅ | Lifecycle; downstream phases read from `job.crop` |
| `phases/extraction.py` · `ExtractionPhaseResult` | `video` | `VideoMetadata \| None` | No — `None` on failure | `None` = extraction failed | ✅ | Failure path |
| `phases/merge.py` · `MergeArtifact` | `frame_count` | `int \| None` | No — populated after measurement | `None` = not yet measured | ✅ | Lifecycle; set during `_execute_merge` |
| `phases/merge.py` · `MergeArtifact` | `plot_path` | `Path \| None` | No — only set when plot is produced | `None` = no plot generated | ✅ | Optional output |
| `phases/encoding.py` · `ChunkEncodingResult` | `final_crf` | `float \| None` | No — `None` on failure | `None` = encoding did not converge | ✅ | Failure path |
| `phases/encoding.py` · `ChunkEncodingResult` | `encoded_file` | `AttemptMetadata \| None` | No — `None` on failure | `None` = no file produced | ✅ | Failure path |
| `phases/encoding.py` · `ChunkEncodingResult` | `error` | `str \| None` | No — only set on failure | Same as `PhaseResult.error` | ✅ | Standard error-or-None pattern |
| `phases/encoding.py` · `EncodingResult` | `error` | `str \| None` | No — only set on failure | Same | ✅ | Standard error-or-None pattern |
| `phases/encoding.py` · `ChunkEncoder.__init__` | `crop_params` | `CropParams \| None` | No — callers may omit | `None` = no cropping | ✅ | Correct sentinel; `is_empty()` check handles zero-crop |
| `phases/audio.py` · `ConversionStrategy.__init__` | `base_bitrate_override` | `str \| None` | No — callers may omit | `None` = use profile bitrate; non-None = override | ✅ | Override pattern |
| `utils/ffmpeg_runner.py` · `run_ffmpeg_async` / `run_ffmpeg` | `output_file` | `Path \| list[Path] \| None` | No — probe/null-encode calls pass `None` | `None` = no output file (probe mode); documented in docstring | ✅ | Intentional; documented with examples |
| `utils/ffmpeg_runner.py` · `run_ffmpeg_async` / `run_ffmpeg` | `progress_callback` | `ProgressCallback \| None` | No — callers may omit | `None` = no progress reporting | ✅ | Standard optional-callback pattern |
| `utils/ffmpeg_runner.py` · `run_ffmpeg_async` / `run_ffmpeg` | `video_meta` | `VideoMetadata \| None` | No — callers may omit | `None` = don't populate metadata | ✅ | Standard optional-output pattern |
| `utils/ffmpeg_runner.py` · `run_ffmpeg_async` / `run_ffmpeg` | `cwd` | `Path \| None` | No — callers may omit | `None` = inherit process cwd | ✅ | Standard subprocess pattern |
| `utils/ffmpeg_runner.py` · `FFmpegRunResult` | `frame_count` | `int \| None` | No — only set when `progress=end` seen | `None` = ffmpeg did not emit a final progress block | ✅ | Reflects real-world absence |
| `utils/visualization.py` · `compute_statistics` | `std_cutoff_max` | `float \| None` | No — callers may omit | `None` = no upper cutoff for std calculation | ✅ | Optional filter |
| `utils/visualization.py` · `compute_statistics` | `std_cutoff_min` | `float \| None` | No — callers may omit | `None` = no lower cutoff | ✅ | Optional filter |
| `utils/visualization.py` · `MetricVisualStyle` | `lossless_threshold` | `float \| None` | No — PSNR has no finite lossless threshold | `None` = metric has no lossless threshold (PSNR) | ✅ | Intentional; PSNR lossless = ∞ |
| `utils/visualization.py` · `create_unified_plot` | `styles` | `dict[...] \| None` | No — callers may omit | `None` = use defaults | ✅ | Standard optional-override pattern |
| `utils/visualization.py` · `create_unified_plot` | `fps` | `float \| None` | No — callers may omit | `None` = show raw frame numbers (no timestamp axis) | ✅ | Intentional mode switch |
| `quality.py` · `run_metric` | `cwd` | `Path \| None` | No — callers may omit | `None` = use distorted file's parent dir | ✅ | Standard optional-cwd pattern |
| `quality.py` · `run_metric` | `progress_callback` | `ProgressCallback \| None` | No — callers may omit | `None` = no progress reporting | ✅ | Standard optional-callback pattern |
| `quality.py` · `run_metric` | `output_extension` | `str \| None` | No — callers may omit | `None` = use default extension per metric type | ✅ | Optional override |
| `quality.py` · `adjust_crf` | `fail_crf` / `pass_crf` (local vars) | `float \| None` | No — computed from history | `None` = no bound known yet (first attempt) | ✅ | Algorithm state; not a parameter |
| `quality.py` · `CRFHistory.get_bounds` return | `fail_crf`, `pass_crf` | `float \| None` | No — may not exist yet | `None` = no bound established | ✅ | Algorithm state |
| `config.py` · `ConfigManager.__init__` | `config_path` | `Path \| None` | No — callers may omit | `None` = search default locations | ✅ | Standard optional-path pattern |
| `config.py` · `ConfigManager.list_profiles` | `codec` | `str \| None` | No — callers may omit | `None` = list all profiles regardless of codec | ✅ | Standard optional-filter pattern |
| `config.py` · `ConfigManager._expand_profile_pattern` | `preset` | `str \| None` | No — internal; `None` = all presets | `None` = expand all presets for the profile | ✅ | Internal algorithm parameter |
| `utils/validation.py` · `Validator.__init__` | `config_manager` | `Any \| None` | No — callers may omit | `None` = skip profile validation | ✅ | Optional dependency injection |

**Summary:** Four parameters are flagged for removal/change:
- `quality_targets` in `_minimal_config` → default to `[]` (empty list is the correct sentinel)
- `ChunkingParams.chunking_mode` → make `str` (required); missing field in YAML = mismatch, trigger re-run
- `OptimizationParams.metrics_sampling` → make `int` (required); same treatment
- `MergeParams.metrics_sampling` → make `int` (required); same treatment

`audio_convert` in `_minimal_config` and `PipelineConfig` needs a docstring clarifying `None` vs `""` semantics. All other `| None` usages are semantically justified.

**Section 7 — Duplicated module-level helper functions**

7.1 WHEN `_targets_as_strings` is needed, THEN the system defines it independently in both `merge.py` and `optimization.py` with identical semantics, instead of defining it once in a shared location.

7.2 WHEN `_cleanup_tmp_files` is needed, THEN the system defines it independently in both `recovery.py` and `merge.py` with identical semantics, instead of using the single definition from `recovery.py`.

7.3 WHEN `_failed(error)` factory helpers are needed, THEN the system defines a separate `_failed` function in each of `chunking.py`, `audio.py`, `merge.py`, `optimization.py`, and `encoding.py`, each returning a different `PhaseResult` subtype — this is acceptable per-phase specialisation, but the pattern of `_outcome_from_artifacts` / `_recovery_message` is also duplicated across `audio.py` and `merge.py` with identical logic, and should be consolidated into `PhaseResult` or `phase.py`.

**Section 8 — Dangling standalone functions without clear ownership**

8.1 WHEN `_measure_quality` and `_log_metrics_summary` and `_collect_crf_data` exist as module-level functions in `merge.py`, THEN the system places logic that is exclusively used by `MergePhase` outside the class, making the class boundary unclear and the functions harder to find.

8.2 WHEN `_build_audio_engine` and `_build_and_display_dry_run_plan` exist as module-level functions in `audio.py`, THEN the system places logic that is exclusively used by `AudioPhase` outside the class.

8.3 WHEN `_make_encoder`, `_select_test_chunks`, `_delete_encoded_result_sidecars` exist as module-level functions in `optimization.py`, THEN the system places logic that is exclusively used by `OptimizationPhase` outside the class.

8.4 WHEN `_probe_resolution`, `_read_metrics_sidecar`, `_write_metrics_sidecar`, `_hardlink_or_copy`, `_write_encoding_result_sidecar`, `_enc_encoded_strategy_dir`, `_recover_encoding_attempts` exist as module-level functions in `encoding.py`, THEN the system places logic that is exclusively used by `EncodingPhase` outside the class.

**Section 9 — Legacy tests and test files referencing removed APIs**

9.1 WHEN `tests/unit/test_refactor_core.py` contains `TestMergeFinalVideo` tests that call `merge_final_video(...)` as a standalone function, THEN the system runs tests against an API that no longer exists in the current phase-object model, causing test failures or requiring the old function to be kept alive.

9.2 WHEN `tests/` root contains `test_metrics.py`, `test_metrics_integration.py`, `test_metrics_orchestrator.py`, `test_metrics_properties.py` as flat files outside the `tests/unit/` or `tests/integration/` subdirectory structure, THEN the system has an inconsistent test layout that makes test discovery and organisation harder.

**Section 10 — Specs outdated**

10.1 WHEN any spec in `.kiro/specs/` was completed before the current codebase state, THEN the spec lacks a summary section describing what has changed or been superseded since it was written, making it hard to understand the current state of the system from the spec history.

**Section 11 — Audio bitrate config inconsistency**

11.1 WHEN `AudioConversionProfile` in `config.py` and `audio.py` stores a per-channel-layout `bitrate` field, AND the CLI / API expose a single `--audio-bitrate` / `audio_base_bitrate` parameter that is a stereo-equivalent base value scaled proportionally, THEN the system has two parallel representations of the same concept: per-layout absolute bitrates in config and a single base bitrate at the call site, with the scaling logic duplicated in both `_build_audio_engine` and `_build_and_display_dry_run_plan`.

11.2 WHEN `ConversionStrategy` accepts both a `profiles` dict (with per-layout absolute bitrates) AND a `base_bitrate_override` (stereo-equivalent base), THEN the system applies the scaling logic a second time inside `_resolve_bitrate`, meaning the override path and the config path use different representations of the same value with no single source of truth.

**Section 12 — `lossless_threshold` in `MetricVisualStyle`**

12.1 WHEN `MetricVisualStyle` carries a `lossless_threshold: float | None` field, THEN the system stores a per-style threshold that is never actually used in plot rendering — the lossless count in summary boxes is computed by `vals >= 100.0` unconditionally, making the field dead code.

12.2 WHEN `lossless_label` in `MetricVisualStyle` is set to `"∞ dB"` for PSNR and `"100.0"` for SSIM/VMAF, THEN the system encodes metric-specific display logic inside a generic style struct rather than deriving it from `MetricType`, coupling the style to knowledge it should not own.

**Section 13 — `# type: ignore` comments**

13.1 WHEN phase `__init__` methods store dependency references as `"_JobPhase | None"` typed attributes (set via `cast(...) if phases else None`), THEN every subsequent access to `self._job.result`, `self._job.scan()`, etc. requires a `# type: ignore[union-attr]` comment because the type checker cannot prove the attribute is non-None at the call site, even though the `_ensure_dependencies()` guard always verifies it before use.

13.2 WHEN `PhaseResult` subclasses initialise list fields as `None` with `# type: ignore[assignment]` (e.g. `chunks: list[ChunkMetadata] = None`), THEN the type annotation is a lie — the field is `None` at construction time but typed as a non-optional list.

13.3 WHEN `config_handler.set_global(enrich_print=False)` is called with `# type: ignore` in multiple phase files, THEN the suppression is a workaround for a missing type stub in `alive_progress`, not a genuine type error in the project code.

13.4 WHEN `ffmpeg_runner.py` accesses `proc.stdout`, `proc.stderr`, and `proc.returncode` with `# type: ignore[arg-type]` / `# type: ignore[arg-type]`, THEN the suppression is caused by `asyncio.subprocess.Process` having `Optional` typed pipes even when `PIPE` was passed — a known stdlib typing limitation.

**Section 15b — Misplaced constants**

15b.1 WHEN each phase file defines its own YAML filename as a module-level string constant (e.g. `_JOB_YAML_FILENAME = "job.yaml"` in `job.py`, `_CHUNKING_YAML = "chunking.yaml"` in `chunking.py`, etc.), THEN the system scatters phase-state-file names across seven separate files instead of grouping them in one place. These constants are owned by their respective phase classes and should either be class-level constants or consolidated in `constants.py` under a "Phase state files" group. The same applies to `METRICS_YAML_FILENAME` in `metrics.py`.

15b.2 WHEN `audio.py` defines `_INTERMEDIATE_CODEC`, `_INTERMEDIATE_EXTENSION`, `_LOUDNORM_TARGET_I`, `_LOUDNORM_TARGET_TP`, `_LOUDNORM_TARGET_LRA` as module-level constants, THEN the EBU R128 standard values (`_LOUDNORM_TARGET_*`) belong in `constants.py` (they are domain constants, not implementation details), while `_INTERMEDIATE_CODEC`/`_INTERMEDIATE_EXTENSION` belong as class-level constants on the strategies that use them.

15b.3 WHEN `_CHANNEL_COUNTS: dict[str, int]` is defined at module level in `audio.py` but is only used by `ConversionStrategy._resolve_bitrate`, THEN the system places a class-specific lookup table outside the class.

15b.4 WHEN `metrics.py` defines `_TEMP_SUFFIX = ".tmp"` as a module-level constant, THEN the system duplicates `TEMP_SUFFIX` from `constants.py`.

15b.5 WHEN `quality.py` defines `_MAX_METRIC = 100.0` as a module-level constant, THEN the system defines a domain constant (the normalized metric scale upper bound) outside `constants.py` where it belongs.

15b.6 WHEN `visualization.py` `create_crf_plot` defines `_N_STAT_COLS: int = 3` inside the function body, THEN the system defines a constant inside a function instead of at module level.

15b.7 WHEN `visualization.py` `compute_statistics` defines `levels` and `keys` lists inside the function body on every call, THEN the system allocates these fixed lists on every invocation instead of defining them as module-level constants.

---

### Expected Behavior (Correct)

**Section 2 — `getattr` on typed objects**

2.1 WHEN phase code accesses fields on typed `PhaseResult` subclasses, THEN the system SHALL use direct attribute access (`result.force_wipe`, `result.crop`, etc.) because the types are known and the type checker can verify correctness.

2.2 WHEN `cli.py` accesses argparse namespace fields that are always registered on the subparser, THEN the system SHALL use direct attribute access (`args.include`, `args.audio_convert`, etc.).

**Section 3 — `runtime_checkable` Protocol**

3.1 WHEN `Phase` is defined, THEN the system SHALL remove `@runtime_checkable` and have every phase class (`JobPhase`, `ExtractionPhase`, `ChunkingPhase`, `OptimizationPhase`, `EncodingPhase`, `AudioPhase`, `MergePhase`) inherit from `Phase` directly so the type checker verifies conformance at definition time without any `isinstance` runtime checks.

**Section 4 — Imports at the top of the file**

4.1 WHEN phase `__init__` methods need dependency phase types, THEN the system SHALL restructure module boundaries (e.g. move shared type stubs or use `TYPE_CHECKING` guards for type hints only) so that all runtime imports are at the top of each file.

4.2 WHEN `api.py` functions need phase types, THEN the system SHALL place those imports at the top of the file (they are already guarded by `TYPE_CHECKING` for type hints; the runtime imports inside function bodies SHALL be moved to the top).

4.3 WHEN any module needs `ConfigManager`, `TimeKey`, `VideoMetadata`, or `json` at runtime, THEN the system SHALL import them at the top of the file.

**Section 5 — `__all__` in internal files**

5.1 WHEN `phase.py` is an internal mechanics file, THEN the system SHALL remove `__all__` from it.

5.2 WHEN `metrics.py` defines `__all__`, THEN the system SHALL remove `__all__` and all `# noqa: F401` task-comment scaffolding, keeping only the imports that are genuinely used.

5.3 WHEN `__init__.py` is a package init for internal use, THEN the system SHALL remove `__all__` from it unless it is the public distribution entry point.

**Section 6 — Unused imports**

6.1 WHEN `metrics.py` imports are all genuinely used in the file, THEN the system SHALL remove all `# noqa: F401` suppressions and task-tracking comments from the import block, leaving clean imports.

6.2 WHEN `models.py` imports `ConfigDict`, THEN the system SHALL verify whether it is used and either remove the import or remove the `# noqa` suppression.

**Section 6b — Other `# noqa` suppressions**

6b.1 WHEN `alive.py` has long `advance` function signatures, THEN the system SHALL reformat them across multiple lines to eliminate the `# noqa: E501` suppressions.

6b.2 WHEN `disk_space.py` needs to read cached-only values from `VideoMetadata` without triggering lazy-load probes, THEN the system SHALL add a `cached_frame_count` / `cached_file_size_bytes` property (or similar) to `VideoMetadata` that returns the backing private field without probing, eliminating the `# noqa: SLF001` suppressions in `disk_space.py`.

6b.3 WHEN `ffmpeg_runner.py` and `metrics.py` need to catch all exceptions from user-supplied callbacks or file I/O, THEN the system SHALL move the `BLE001` suppression to `ruff.toml` as a `per-file-ignores` entry with an explicit comment explaining why broad exception catching is intentional in those specific locations, rather than scattering inline suppressions through the code.

**Section 7 — `| None` arguments**

7.1 WHEN `_minimal_config` in `api.py` accepts `quality_targets: list[QualityTarget] | None`, THEN the system SHALL change the default to `[]` and remove the `None` branch, because an empty list is the correct sentinel for "no targets" and `None` only adds a needless `or []` guard downstream.

7.2 WHEN `_minimal_config` accepts `audio_convert: str | None` (and `PipelineConfig` carries the same field), THEN the system SHALL add an explicit docstring note clarifying that `None` means "use config default" and `""` means "convert nothing", because these two values have distinct runtime semantics that are not currently documented.

7.3 WHEN `ChunkingParams.chunking_mode` is `str | None`, THEN the system SHALL make it a required `str` field. The load path SHALL treat a missing or `None` value in YAML as a mismatch and trigger a re-run, rather than silently skipping the check.

7.4 WHEN `OptimizationParams.metrics_sampling` is `int | None`, THEN the system SHALL make it a required `int` field with the same mismatch-on-missing treatment.

7.5 WHEN `MergeParams.metrics_sampling` is `int | None`, THEN the system SHALL make it a required `int` field with the same mismatch-on-missing treatment.

7.6 All other `| None` parameters and fields identified in the audit table are semantically justified and SHALL NOT be changed.

**Section 8 — Duplicated helpers**

8.1 WHEN `_targets_as_strings` is needed, THEN the system SHALL define it once in `phase.py` or a shared utility and import it in both `merge.py` and `optimization.py`.

8.2 WHEN `_cleanup_tmp_files` is needed, THEN the system SHALL use the single definition from `recovery.py` in `merge.py` instead of redefining it.

8.3 WHEN `_outcome_from_artifacts` and `_recovery_message` logic is duplicated across phase files, THEN the system SHALL consolidate the common pattern into `PhaseResult` or a shared helper in `phase.py`.

**Section 9 — Dangling standalone functions**

9.1 WHEN `_measure_quality`, `_log_metrics_summary`, `_collect_crf_data` are exclusively used by `MergePhase`, THEN the system SHALL move them inside `MergePhase` as private methods.

9.2 WHEN `_build_audio_engine`, `_build_and_display_dry_run_plan` are exclusively used by `AudioPhase`, THEN the system SHALL move them inside `AudioPhase` as private methods.

9.3 WHEN `_make_encoder`, `_select_test_chunks`, `_delete_encoded_result_sidecars` are exclusively used by `OptimizationPhase`, THEN the system SHALL move them inside `OptimizationPhase` as private methods.

9.4 WHEN `_probe_resolution`, `_read_metrics_sidecar`, `_write_metrics_sidecar`, `_hardlink_or_copy`, `_write_encoding_result_sidecar`, `_enc_encoded_strategy_dir`, `_recover_encoding_attempts` are exclusively used by `EncodingPhase`, THEN the system SHALL move them inside `EncodingPhase` as private methods.

9.5 WHEN a standalone function serves multiple phase classes or is a genuine cross-cutting utility (e.g. `_cleanup_tmp_files`, `_chunk_name_duration`, `_expand_scenes`, `_filename_prefix`, `_is_raw_source`), THEN the system SHALL keep it as a module-level function with a comment explaining why it is not a method.

**Section 10 — Legacy tests**

10.1 WHEN `TestMergeFinalVideo` in `test_refactor_core.py` tests a standalone function API that no longer exists, THEN the system SHALL remove those tests and replace them with tests against the current `MergePhase` object API, or remove them entirely if equivalent coverage exists elsewhere.

10.2 WHEN metrics test files exist at the `tests/` root level, THEN the system SHALL move them into `tests/unit/` or `tests/integration/` as appropriate, and all tests SHALL pass after the move.

**Section 11 — Specs**

11.1 WHEN any spec in `.kiro/specs/` was completed before the current codebase state, THEN the system SHALL add a "Current State" summary section at the top of each spec's `requirements.md` describing what is still accurate, what has been superseded, and what has changed since the spec was completed.

**Section 12 — Audio bitrate config**

12.1 WHEN the audio conversion system needs a bitrate for a given channel layout, THEN the system SHALL use a single base bitrate (stereo-equivalent) as the sole user-facing parameter, with proportional scaling to other channel counts applied in exactly one place. The per-layout `bitrate` fields in `AudioConversionProfile` SHALL be removed; the config file SHALL store only the stereo base bitrate. `ConversionStrategy` SHALL accept only the base bitrate and derive per-layout values internally.

12.2 WHEN `audio_base_bitrate` is documented in CLI help, API docstrings, and config, THEN the system SHALL include a clear note that the value is the stereo (2.0) equivalent and that other channel layouts are scaled proportionally by channel count.

**Section 13 — `lossless_threshold` in `MetricVisualStyle`**

13.1 WHEN `MetricVisualStyle` is defined, THEN the system SHALL remove the `lossless_threshold` field entirely. The lossless count in summary boxes SHALL continue to be computed as `vals >= 100.0` (all normalised metrics share the same 0–100 scale). The `lossless_label` field SHALL also be removed; the display label SHALL be derived from `MetricType` directly (e.g. `"∞ dB"` for PSNR, `"100.0"` for SSIM/VMAF) at the point of use.

**Section 14 — `# type: ignore` comments**

14.1 WHEN phase classes inherit from `Phase` (ABC) and `PhaseResult` subclasses are properly typed, THEN the system SHALL have zero `# type: ignore[union-attr]` comments on dependency accesses — the type checker SHALL be able to verify correctness through explicit inheritance and non-optional dependency types.

14.2 WHEN `PhaseResult` subclasses initialise list fields, THEN the system SHALL use `field(default_factory=list)` (dataclass) or an explicit `[]` default rather than `None` with a `# type: ignore[assignment]` suppression.

14.3 WHEN `config_handler.set_global(enrich_print=False)` is called, THEN the `# type: ignore` suppression SHALL remain (it is a third-party stub limitation, not a project code issue) — this is the one acceptable category of suppression.

14.4 WHEN `asyncio.subprocess.Process` pipe attributes are accessed in `ffmpeg_runner.py`, THEN the `# type: ignore[arg-type]` suppressions SHALL remain (stdlib typing limitation) — this is also an acceptable category.

**Section 15 — Phase dependency attributes typed as `| None`**

15.1 WHEN a phase requires a dependency to function, THEN the system SHALL store the dependency reference as a non-optional typed attribute (e.g. `self._job: JobPhase`). The `phases` registry SHALL always be provided to phase constructors in normal operation; the `if phases else None` fallback SHALL be removed.

15.2 WHEN a phase is constructed without a registry (e.g. in tests), THEN the system SHALL require the dependency to be passed explicitly as a constructor argument rather than defaulting to `None`. Tests that need a phase without dependencies SHALL construct a minimal registry or use a stub.

15.3 WHEN `_ensure_dependencies()` is called, THEN it SHALL only need to check that the dependency's `result` is populated (i.e. `scan()`/`run()` has been called), not whether the dependency reference itself is non-None.

**Section 15b — Misplaced constants**

15b.1 WHEN phase YAML filenames are defined as module-level constants in each phase file, THEN the system SHALL move them to a class-level constant named `SIDECAR` on their respective phase class (e.g. `JobPhase.SIDECAR = "job.yaml"`), since they are owned by the class and have no meaning outside it. All seven phase classes and `YamlMetricsCollector` SHALL use the same `SIDECAR` naming convention.

15b.2 WHEN `METRICS_YAML_FILENAME` is defined in `metrics.py`, THEN the system SHALL move it to a class-level constant on `YamlMetricsCollector` (e.g. `YamlMetricsCollector.SIDECAR = "metrics.yaml"`), since it is the output artifact of that class — analogous to how each phase owns its YAML filename. The orchestrator SHALL NOT reference the filename at all; logging of the metrics output path SHALL be owned by `YamlMetricsCollector.flush()` at `debug` level, and `NoOpMetricsCollector.flush()` SHALL log that metrics are disabled at `debug` level.

15b.3 WHEN `_LOUDNORM_TARGET_I`, `_LOUDNORM_TARGET_TP`, `_LOUDNORM_TARGET_LRA` are defined in `audio.py`, THEN the system SHALL move them to `constants.py` under an "Audio processing" group, since they are EBU R128 standard values (domain constants, not implementation details).

15b.4 WHEN `_INTERMEDIATE_CODEC` and `_INTERMEDIATE_EXTENSION` are defined at module level in `audio.py`, THEN the system SHALL move them to class-level constants on the strategy classes that use them (or a shared base class if multiple strategies share them).

15b.5 WHEN `_CHANNEL_COUNTS` is defined at module level in `audio.py`, THEN the system SHALL move it to a class-level constant on `ConversionStrategy`.

15b.6 WHEN `_TEMP_SUFFIX` is defined in `metrics.py`, THEN the system SHALL remove it and import `TEMP_SUFFIX` from `constants.py`.

15b.7 WHEN `_MAX_METRIC = 100.0` is defined in `quality.py`, THEN the system SHALL move it to `constants.py` under a "Metric normalization" group.

15b.8 WHEN `_N_STAT_COLS: int = 3` is defined inside `create_crf_plot` in `visualization.py`, THEN the system SHALL move it to module level alongside the other visualization constants.

15b.9 WHEN `levels` and `keys` lists are defined inside `compute_statistics` in `visualization.py` on every call, THEN the system SHALL define them as module-level constants.

---

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the full pipeline is run end-to-end (`pyqenc auto`), THEN the system SHALL CONTINUE TO produce the same output files with the same quality metrics as before the cleanup.

3.2 WHEN any phase is run standalone (e.g. `pyqenc chunk`, `pyqenc audio`), THEN the system SHALL CONTINUE TO produce correct output and recover from partial work correctly.

3.3 WHEN `pyqenc auto --dry-run` is invoked, THEN the system SHALL CONTINUE TO report what would be done without writing any files.

3.4 WHEN `--force` is provided and a source mismatch is detected, THEN the system SHALL CONTINUE TO wipe and re-run all downstream phases correctly.

3.5 WHEN audio processing runs with `--audio-convert`, `--audio-codec`, and `--audio-bitrate` arguments, THEN the system SHALL CONTINUE TO apply the correct conversion profiles.

3.6 WHEN `None` is a meaningful sentinel for `audio_convert` (meaning "use config default") vs. an empty string (meaning "convert nothing"), THEN the system SHALL CONTINUE TO distinguish these two cases correctly after the `| None` audit.

3.7 WHEN all existing passing tests are run after cleanup, THEN the system SHALL CONTINUE TO pass all tests that were passing before (excluding tests that are explicitly removed as part of this cleanup because they test removed APIs).
