# Implementation Plan

<!-- markdownlint-disable MD024 -->

- [ ] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Structural Defects Present in Unfixed Codebase
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the defects exist
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected structural state — it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate each structural defect category exists
  - **Scoped PBT Approach**: For deterministic structural checks, scope each assertion to the concrete failing location(s)
  - Create `tests/unit/test_cleanup_properties.py` with a `TestBugCondition` class
  - Assert `grep`-equivalent checks via AST/source inspection:
    - `getattr` calls on typed `PhaseResult` subclasses exist in `phases/merge.py`, `phases/optimization.py`
    - `Phase` in `phase.py` is decorated `@runtime_checkable` (not ABC)
    - At least one phase class (`MergePhase`, `AudioPhase`, etc.) does NOT inherit from `Phase`
    - Deferred runtime imports exist inside function/method bodies in `phase.py` (`_build_registry`), `phases/merge.py`, `phases/audio.py`
    - `__all__` is defined in `phase.py`, `metrics.py`, `phases/__init__.py`
    - `# noqa: F401` comments with task-tracking text exist in `metrics.py`
    - `ChunkingParams.chunking_mode` is typed `str | None` in `state.py`
    - `OptimizationParams.metrics_sampling` is typed `int | None` in `state.py`
    - `MergeParams.metrics_sampling` is typed `int | None` in `state.py`
    - `_targets_as_strings` is defined in both `merge.py` and `optimization.py`
    - `_measure_quality` is a module-level function in `merge.py` (not a method)
    - `TestMergeFinalVideo` class exists in `tests/unit/test_refactor_core.py`
    - Metrics test files exist at `tests/` root (not in `tests/unit/` or `tests/integration/`)
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct — it proves the defects exist)
  - Document counterexamples found to understand scope of cleanup
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 2.1, 2.2, 3.1, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 6.1, 6.2, 7.1, 7.2, 7.3, 7.4, 7.5, 8.1, 8.2, 8.3, 9.1, 9.2, 9.3, 9.4, 9.5, 10.1, 10.2, 11.1_


- [ ] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - State Field Round-Trip and Pipeline Behaviour Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Observe: `ChunkingParams(chunking_mode="scene", ...)` serialises and deserialises correctly on unfixed code
  - Observe: `OptimizationParams(metrics_sampling=4, ...)` round-trips correctly on unfixed code
  - Observe: `MergeParams(metrics_sampling=4, ...)` round-trips correctly on unfixed code
  - Observe: `_build_registry(config, collector)` constructs all seven phases without error on unfixed code
  - Write property-based tests in `tests/unit/test_cleanup_properties.py`:
    - **PBT**: For all valid `ChunkingParams` instances, `load(save(params))` round-trip is lossless (from Preservation Requirements in design)
    - **PBT**: For all valid `OptimizationParams` instances, `load(save(params))` round-trip is lossless
    - **PBT**: For all valid `MergeParams` instances, `load(save(params))` round-trip is lossless
    - **PBT**: For all `list[QualityTarget]`, `_targets_as_strings` output is a list of non-empty strings with correct length
    - **Unit**: `_build_registry(config, collector)` returns a dict with all seven phase classes as keys
    - **Unit**: Each phase object returned by `_build_registry` has `name`, `dependencies`, `result` attributes
  - Verify all tests PASS on UNFIXED code (confirms baseline behaviour to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_


- [ ] 3. Convert `Phase` from `@runtime_checkable` Protocol to ABC (Section 2)

  - [ ] 3.1 Convert `Phase` to ABC in `phase.py`
    - Remove `@runtime_checkable` decorator and `Protocol` base
    - Add `from abc import ABC, abstractmethod`; change `class Phase(Protocol)` → `class Phase(ABC)`
    - Mark `scan()` and `run()` with `@abstractmethod`
    - Remove `from typing import Protocol, runtime_checkable`
    - Keep `name`, `dependencies`, `result` as typed class-level annotations (not abstract properties)
    - _Bug_Condition: `Phase` is `@runtime_checkable Protocol`; phase classes do not inherit from `Phase`_
    - _Expected_Behavior: `Phase` is ABC; all phase classes inherit from it; type checker verifies conformance_
    - _Preservation: `_build_registry` still constructs all seven phases; `isinstance(phase, Phase)` still works_
    - _Requirements: 3.1_

  - [ ] 3.2 Add `Phase` inheritance to all seven phase classes
    - `JobPhase(Phase)`, `ExtractionPhase(Phase)`, `ChunkingPhase(Phase)`, `OptimizationPhase(Phase)`, `EncodingPhase(Phase)`, `AudioPhase(Phase)`, `MergePhase(Phase)`
    - Each phase file: add `Phase` to the class inheritance list
    - Update any `isinstance(x, Phase)` checks in tests — they now work via ABC, not structural duck-typing
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 3.1_


- [ ] 4. Make phase dependency attributes non-optional (Section 14)

  - [ ] 4.1 Remove `| None` from dependency attributes in all six dependent phase classes
    - Files: `audio.py`, `chunking.py`, `encoding.py`, `extraction.py`, `merge.py`, `optimization.py`
    - Remove the `if phases else None` fallback from all phase `__init__` methods
    - Change dependency attribute types from `"_JobPhase | None"` to `"JobPhase"` (non-optional)
    - Use `TYPE_CHECKING` guard for the import if needed to avoid circular imports
    - `phases` parameter in `__init__` remains but is now required (no default `None`)
    - _Bug_Condition: `self._job: "_JobPhase | None" = cast(...) if phases else None` pattern in all dependent phases_
    - _Expected_Behavior: `self._job: JobPhase` — non-optional; always set from registry_
    - _Preservation: `_build_registry` always provides the registry; all phases construct correctly_
    - _Requirements: 15.1, 15.2, 15.3_

  - [ ] 4.2 Simplify `_ensure_dependencies()` in each phase
    - Remove `if self._job is None` guards — dependency reference is now always non-None
    - `_ensure_dependencies()` only checks that `dep.result` is populated; calls `dep.scan()` if not
    - `self.dependencies: list[Phase]` populated directly from typed attributes — remove `[d for d in [...] if d is not None]` filter
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 15.3_


- [ ] 5. Remove `getattr` on typed objects (Section 1)

  - [ ] 5.1 Replace `getattr` calls in phase files with direct attribute access
    - `merge.py` `scan()` and `run()`: `getattr(job_result, "force_wipe", False)` → `job_result.force_wipe`
    - `merge.py` `_execute_merge()`: `getattr(job_result, "crop", None)` → `job_result.crop`; `getattr(job_result, "job", None)` → `job_result.job`
    - `merge.py` `_get_expected_strategies()`: `getattr(self._encoding.result, "encoded", [])` → `self._encoding.result.encoded`
    - `optimization.py` `run()`: `getattr(job_result, "force_wipe", False)` → `job_result.force_wipe`; `getattr(job_result, "crop", None)` → `job_result.crop`
    - `optimization.py` `run()`: `getattr(chunking_result, "chunks", [])` → `chunking_result.chunks`
    - Audit all other phase files for any remaining `getattr` on typed results
    - _Bug_Condition: `getattr(result, "field", default)` on typed `PhaseResult` subclasses_
    - _Expected_Behavior: direct attribute access `result.field` — type checker verifies correctness_
    - _Preservation: same values accessed; no logic change_
    - _Requirements: 2.1_

  - [ ] 5.2 Replace `getattr` calls in `cli.py` with direct attribute access
    - `getattr(args, "include", None)` → `args.include`
    - `getattr(args, "exclude", None)` → `args.exclude`
    - `getattr(args, "audio_convert", None)` → `args.audio_convert`
    - `getattr(args, "audio_codec", None)` → `args.audio_codec`
    - `getattr(args, "audio_bitrate", None)` → `args.audio_bitrate`
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 2.2_


- [ ] 6. Move deferred imports to top of file (Section 3)

  - [ ] 6.1 Move phase imports in `phase.py` `_build_registry` to top of file
    - Move all seven `from pyqenc.phases.X import XPhase` imports from inside `_build_registry` to the top of `phase.py`
    - Verify no `ImportError` — the circular import cycle does not actually exist
    - Run `uv run python -c "from pyqenc.phase import _build_registry"` — must succeed
    - _Bug_Condition: all seven phase imports are deferred inside `_build_registry` function body_
    - _Expected_Behavior: imports at top of `phase.py`; no deferred runtime imports_
    - _Preservation: `_build_registry` constructs all phases identically_
    - _Requirements: 4.1_

  - [ ] 6.2 Move deferred imports in phase `__init__` methods to top of each file
    - `merge.py`: move `AudioPhase`, `EncodingPhase`, `JobPhase` imports from `__init__` to top of file
    - `optimization.py`: move `ChunkingPhase`, `JobPhase` imports from `__init__` to top of file
    - All other phase files: audit and move any remaining deferred runtime imports
    - Use `TYPE_CHECKING` guard only for imports needed solely for type annotations
    - _Requirements: 4.1_

  - [ ] 6.3 Move remaining deferred imports in other files
    - `merge.py` `run()`: move `from pyqenc.metrics import TimeKey` to top of file
    - `audio.py` `_build_audio_engine`: move `from pyqenc.config import ConfigManager` to top of file
    - `encoding.py` `_probe_resolution`: move `import json` to top of file
    - `visualization.py`: move `VideoMetadata` import to top of file
    - `api.py`: move any phase-type imports from function bodies to top of file
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 4.2, 4.3_


- [ ] 7. Remove `__all__` from internal files (Section 4)

  - [ ] 7.1 Remove `__all__` from `phase.py`, `metrics.py`, and `phases/__init__.py`
    - Delete the `__all__` list from `phase.py`
    - Delete the `__all__` list from `metrics.py`, along with all task-tracking comments (`# Added in task 7:`, `# Active collector registry (task 19):`, etc.)
    - Delete the `__all__` list from `phases/__init__.py`
    - Verify no external code uses `from pyqenc.phase import *` or `from pyqenc.metrics import *`
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Bug_Condition: `__all__` defined in internal files `phase.py`, `metrics.py`, `phases/__init__.py`_
    - _Expected_Behavior: no `__all__` in internal files; no task-tracking comments in import blocks_
    - _Preservation: all imports that were in `__all__` remain importable by name_
    - _Requirements: 5.1, 5.2, 5.3_


- [ ] 8. Fix `| None` field types in `state.py` and `api.py` (Section 6)

  - [ ] 8.1 Write property-based tests for state field round-trips (BEFORE changing field types)
    - In `tests/unit/test_cleanup_properties.py`, add PBT tests:
      - Generate random valid `ChunkingParams` with `chunking_mode: str`; assert `load(save(params))` is lossless
      - Generate YAML dicts with `chunking_mode` absent or `None`; assert `ChunkingParams.load()` returns `None`
      - Same two properties for `OptimizationParams.metrics_sampling` and `MergeParams.metrics_sampling`
    - Run on UNFIXED code — round-trip tests pass; missing-field tests fail (currently `None` is accepted)
    - These tests define the target behaviour; they will pass after the fix
    - _Requirements: 7.3, 7.4, 7.5_

  - [ ] 8.2 Make `ChunkingParams.chunking_mode`, `OptimizationParams.metrics_sampling`, `MergeParams.metrics_sampling` required non-optional fields
    - `state.py` `ChunkingParams`: change `chunking_mode: str | None = None` → `chunking_mode: str`
    - `state.py` `OptimizationParams`: change `metrics_sampling: int | None = None` → `metrics_sampling: int`
    - `state.py` `MergeParams`: change `metrics_sampling: int | None = None` → `metrics_sampling: int`
    - Update `from_yaml_dict` for each: raise `ValueError` when field is missing or `None` (so `load()` returns `None` → triggers re-run)
    - Remove any `if params.chunking_mode is None` / `if params.metrics_sampling is None` guards in callers
    - _Bug_Condition: fields typed `X | None` with no semantic reason for `None`; `None` only exists for old YAML files_
    - _Expected_Behavior: required `str`/`int` fields; missing YAML field → `load()` returns `None` → re-run_
    - _Preservation: valid YAML with all fields present round-trips losslessly_
    - _Requirements: 7.3, 7.4, 7.5_

  - [ ] 8.3 Fix `quality_targets` default in `api.py` and add `audio_convert` docstring
    - `api.py` `_minimal_config`: change `quality_targets: list[QualityTarget] | None = None` → `quality_targets: list[QualityTarget] = []`
    - Remove the `quality_targets or []` guard in the `PipelineConfig(...)` call
    - Add docstring note to `audio_convert` parameter: `None` = use config default; `""` = convert nothing
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 7.1, 7.2_


- [ ] 9. Consolidate duplicated helpers (Section 7)

  - [ ] 9.1 Move `_targets_as_strings` to `phase.py` and remove duplicates
    - Move the definition from `merge.py` to `phase.py` (alongside `PhaseResult`, `Artifact`)
    - Delete the definition from `optimization.py`
    - Update `merge.py` and `optimization.py` to import `_targets_as_strings` from `phase.py`
    - Add comment: `# Cross-cutting utility used by MergePhase and OptimizationPhase.`
    - _Bug_Condition: `_targets_as_strings` defined identically in `merge.py` and `optimization.py`_
    - _Expected_Behavior: single definition in `phase.py`; both files import from there_
    - _Requirements: 8.1_

  - [ ] 9.2 Remove duplicate `_cleanup_tmp_files` from `merge.py`
    - Delete the duplicate definition from `merge.py`
    - Add `from pyqenc.phases.recovery import _cleanup_tmp_files` to `merge.py` (or verify the inline `.tmp` cleanup in `MergePhase._recover()` is sufficient and remove the standalone call)
    - _Bug_Condition: `_cleanup_tmp_files` defined in both `recovery.py` (canonical) and `merge.py` (duplicate)_
    - _Expected_Behavior: single definition in `recovery.py`; `merge.py` imports from there_
    - _Requirements: 8.2_

  - [ ] 9.3 Consolidate `_outcome_from_artifacts` / `_recovery_message` into `phase.py`
    - Move the common pattern from `audio.py`, `merge.py`, `chunking.py`, `extraction.py` to `phase.py` as module-level helpers
    - Delete per-file definitions; update imports in each file
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 8.3_


- [ ] 10. Move dangling standalone functions into their phase classes (Section 8)

  - [ ] 10.1 Move `merge.py` module-level functions into `MergePhase`
    - Move `_measure_quality`, `_log_metrics_summary`, `_collect_crf_data` into `MergePhase` as private methods
    - Update all call sites within `MergePhase._execute_merge()` to use `self._measure_quality(...)` etc.
    - _Bug_Condition: `_measure_quality`, `_log_metrics_summary`, `_collect_crf_data` are module-level but only called by `MergePhase`_
    - _Expected_Behavior: private methods on `MergePhase`; class boundary is clear_
    - _Preservation: identical logic; only location changes_
    - _Requirements: 9.1_

  - [ ] 10.2 Move `audio.py` module-level functions into `AudioPhase`
    - Move `_build_audio_engine`, `_build_and_display_dry_run_plan` into `AudioPhase` as private methods
    - Update all call sites within `AudioPhase`
    - Keep `_filename_prefix` and `_is_raw_source` at module level (used by multiple strategy classes) with comment explaining why
    - _Requirements: 9.2_

  - [ ] 10.3 Move `optimization.py` module-level functions into `OptimizationPhase`
    - Move `_make_encoder`, `_select_test_chunks`, `_delete_encoded_result_sidecars` into `OptimizationPhase` as private methods
    - Update all call sites within `OptimizationPhase.run()`
    - _Requirements: 9.3_

  - [ ] 10.4 Move `encoding.py` module-level functions into `EncodingPhase` / `ChunkEncoder`
    - Move `_probe_resolution`, `_read_metrics_sidecar`, `_write_metrics_sidecar`, `_hardlink_or_copy`, `_write_encoding_result_sidecar` into `ChunkEncoder` as private methods (used by `ChunkEncoder`)
    - Move `_enc_encoded_strategy_dir`, `_recover_encoding_attempts` into `EncodingPhase` as private methods (used by `EncodingPhase._recover()`)
    - Update all call sites
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 9.4, 9.5_


- [ ] 11. Remove all `# noqa` suppressions (Section 5)

  - [ ] 11.1 Add `cached_frame_count` and `cached_file_size_bytes` to `VideoMetadata`
    - Add two cache-read-only properties to `VideoMetadata` in `models.py`:
      ```python
      @property
      def cached_frame_count(self) -> int | None:
          """Return the cached frame count without triggering a probe."""
          return self._frame_count

      @property
      def cached_file_size_bytes(self) -> int | None:
          """Return the cached file size without triggering a stat() call."""
          return self._file_size_bytes
      ```
    - Update `disk_space.py` to use `video.cached_frame_count` and `video.cached_file_size_bytes` instead of `video._frame_count` and `video._file_size_bytes`
    - Remove the `# noqa: SLF001` suppressions from `disk_space.py`
    - _Bug_Condition: `disk_space.py` accesses `video._frame_count` and `video._file_size_bytes` with `# noqa: SLF001`_
    - _Expected_Behavior: `VideoMetadata` exposes cache-only read properties; no private member access_
    - _Requirements: 6b.2_

  - [ ] 11.2 Remove `# noqa: F401` suppressions from `metrics.py` and `models.py`
    - Remove all `# noqa: F401` and task-tracking comments from `metrics.py` import block — all imports are genuinely used
    - Remove `# noqa: F401 (ConfigDict used in PipelineConfig)` from `models.py`
    - Run `ruff check pyqenc/metrics.py pyqenc/models.py` — zero F401 warnings
    - _Requirements: 6.1, 6.2_

  - [ ] 11.3 Reformat `alive.py` to remove `# noqa: E501` suppressions
    - Reformat the two `advance` function signatures across multiple lines to stay within the line length limit
    - Remove the `# noqa: E501` suppressions
    - _Requirements: 6b.1_

  - [ ] 11.4 Move `BLE001` suppressions to `ruff.toml` as `per-file-ignores`
    - Remove inline `# noqa: BLE001` comments from `ffmpeg_runner.py` and `metrics.py`
    - Add to `ruff.toml` (or `pyproject.toml` `[tool.ruff.lint.per-file-ignores]`):
      ```toml
      "pyqenc/utils/ffmpeg_runner.py" = ["BLE001"]  # user-supplied callback; must not propagate
      "pyqenc/metrics.py" = ["BLE001"]              # file I/O on resume/flush; must not crash pipeline
      ```
    - Run `ruff check pyqenc/` — zero inline noqa suppressions for BLE001
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 6b.3_


- [ ] 12. Eliminate remaining `# type: ignore` on project code (Section 13)

  - [ ] 12.1 Replace `# type: ignore[assignment]` on list fields with `field(default_factory=list)`
    - `ChunkingPhaseResult`: change `chunks: list[ChunkMetadata] = None  # type: ignore[assignment]` → `chunks: list[ChunkMetadata] = field(default_factory=list)`
    - `EncodingPhaseResult`: same treatment for `encoded` field
    - Audit all other `PhaseResult` subclasses for the same pattern
    - _Bug_Condition: `PhaseResult` subclasses initialise list fields as `None` with `# type: ignore[assignment]`_
    - _Expected_Behavior: `field(default_factory=list)` — annotation matches actual value_
    - _Requirements: 14.2_

  - [ ] 12.2 Verify zero `# type: ignore[union-attr]` remain after Sections 2 and 14
    - Sections 3 (ABC) and 4 (non-optional deps) should have eliminated all `# type: ignore[union-attr]` automatically
    - Run `grep -rn "type: ignore\[union-attr\]" pyqenc/` — must return zero results
    - If any remain, fix the root cause (missing ABC inheritance or still-nullable dependency)
    - _Requirements: 14.1_

  - [ ] 12.3 Confirm acceptable suppressions remain untouched
    - `config_handler.set_global(enrich_print=False)  # type: ignore` — keep (third-party stub limitation)
    - `# type: ignore[arg-type]` on `proc.stdout` / `proc.stderr` in `ffmpeg_runner.py` — keep (stdlib limitation)
    - `# type: ignore[return-value]` on `dict(zip(keys, stats))` in `visualization.py` — keep (TypedDict limitation)
    - Run `ruff check pyqenc/` and type checker — zero new errors
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 14.3, 14.4_


- [ ] 13. Unify audio bitrate representation (Section 11)

  - [ ] 13.1 Remove per-layout `bitrate` from `AudioConversionProfile` and update config
    - Remove `bitrate` field from `AudioConversionProfile` in `config.py` and `audio.py` — profile now carries only `codec` and `extension`
    - Update `default_config.yaml`: remove per-layout `bitrate` entries from `audio_output.profiles`; add single `audio_output.base_bitrate` key (e.g. `"192k"`)
    - `ConfigManager.get_audio_output_config()`: read `base_bitrate` from config; store on `AudioOutputConfig` as `base_bitrate: str`
    - _Bug_Condition: `AudioConversionProfile` stores per-layout absolute bitrates AND `base_bitrate_override` applies scaling again — two representations, scaling duplicated_
    - _Expected_Behavior: single `base_bitrate` (stereo-equivalent); scaling in exactly one place_
    - _Preservation: same effective bitrates produced for each channel layout_
    - _Requirements: 12.1, 12.2_

  - [ ] 13.2 Consolidate bitrate scaling into `ConversionStrategy._resolve_bitrate`
    - Remove `base_bitrate_override` from `ConversionStrategy.__init__` — strategy reads from `AudioOutputConfig.base_bitrate`
    - The `audio_base_bitrate` CLI/API override is applied by replacing `AudioOutputConfig.base_bitrate` before constructing the strategy
    - Scaling logic (`base_kbps * channels / 2`) lives in exactly one place: `ConversionStrategy._resolve_bitrate`
    - Remove duplicate scaling logic from `_build_audio_engine` (now a method on `AudioPhase`)
    - Update all docstrings and CLI help: "stereo (2.0) equivalent; other channel layouts are scaled proportionally by channel count"
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 12.1, 12.2_


- [ ] 14. Remove `lossless_threshold` and `lossless_label` from `MetricVisualStyle` (Section 12)

  - [ ] 14.1 Remove dead fields from `MetricVisualStyle` and update rendering
    - Remove `lossless_threshold: float | None` from `MetricVisualStyle` in `visualization.py`
    - Remove `lossless_label: str` from `MetricVisualStyle`
    - In summary box rendering code, derive lossless label directly from `MetricType`:
      - `MetricType.PSNR` → `"∞ dB"`
      - `MetricType.SSIM` / `MetricType.VMAF` → `"100.0"`
      Use a `match` statement or `dict` lookup at the point of use
    - Update `DEFAULT_METRIC_STYLES` to remove the two fields from all three entries
    - Lossless count computation (`vals >= 100.0`) is unchanged
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Bug_Condition: `MetricVisualStyle.lossless_threshold` is set but never read during plot rendering — dead field_
    - _Expected_Behavior: no `lossless_threshold` or `lossless_label` fields; label derived from `MetricType` at point of use_
    - _Preservation: lossless count and label display identical to before_
    - _Requirements: 13.1_


- [ ] 15. Consolidate misplaced constants — SIDECAR names and others (Section 15b)

  - [ ] 15.1 Move phase YAML filenames to class-level `SIDECAR` constants
    - Add `SIDECAR: str = "job.yaml"` to `JobPhase`; remove `_JOB_YAML_FILENAME` module constant
    - Add `SIDECAR: str = "extraction.yaml"` to `ExtractionPhase`; remove module constant
    - Add `SIDECAR: str = "chunking.yaml"` to `ChunkingPhase`; remove module constant
    - Add `SIDECAR: str = "optimization.yaml"` to `OptimizationPhase`; remove module constant
    - Add `SIDECAR: str = "encoding.yaml"` to `EncodingPhase`; remove module constant
    - Add `SIDECAR: str = "audio.yaml"` to `AudioPhase`; remove module constant
    - Add `SIDECAR: str = "merge.yaml"` to `MergePhase`; remove module constant
    - Update all references within each class from old module-level name → `self.SIDECAR` / `ClassName.SIDECAR`
    - _Bug_Condition: phase YAML filenames scattered as module-level constants across seven files_
    - _Expected_Behavior: class-level `SIDECAR` constant on each phase class_
    - _Requirements: 15b.1_

  - [ ] 15.2 Move `METRICS_YAML_FILENAME` to `YamlMetricsCollector.SIDECAR`
    - Add `SIDECAR: str = "metrics.yaml"` to `YamlMetricsCollector`; remove `METRICS_YAML_FILENAME` module constant
    - Move metrics output path logging from orchestrator into `YamlMetricsCollector.flush()` at `debug` level
    - Add `debug` log to `NoOpMetricsCollector.flush()`: `"Metrics collection disabled (no-op collector)"`
    - Remove metrics path logging from orchestrator entirely
    - _Requirements: 15b.2_

  - [ ] 15.3 Move EBU R128 constants to `constants.py`
    - Move `_LOUDNORM_TARGET_I`, `_LOUDNORM_TARGET_TP`, `_LOUDNORM_TARGET_LRA` from `audio.py` to `constants.py` under `# Audio processing` group
    - Update `audio.py` to import them from `constants`
    - _Requirements: 15b.3_

  - [ ] 15.4 Move `_INTERMEDIATE_CODEC` / `_INTERMEDIATE_EXTENSION` to strategy class constants
    - Move from module level in `audio.py` to class-level constants on the strategy class(es) that use them (or a shared `BaseStrategy` if multiple strategies share them)
    - _Requirements: 15b.4_

  - [ ] 15.5 Move `_CHANNEL_COUNTS` to `ConversionStrategy` class constant
    - Move `_CHANNEL_COUNTS: dict[str, int]` from module level in `audio.py` to `ConversionStrategy._CHANNEL_COUNTS`
    - _Requirements: 15b.5_

  - [ ] 15.6 Remove `_TEMP_SUFFIX` from `metrics.py`; use `constants.TEMP_SUFFIX`
    - Delete `_TEMP_SUFFIX = ".tmp"` from `metrics.py`
    - Replace its usages with `TEMP_SUFFIX` imported from `pyqenc.constants`
    - _Requirements: 15b.6_

  - [ ] 15.7 Move `_MAX_METRIC` from `quality.py` to `constants.py`
    - Move `_MAX_METRIC = 100.0` to `constants.py` under `# Metric normalization` group
    - Update `quality.py` to import from `constants`
    - _Requirements: 15b.7_

  - [ ] 15.8 Move `_N_STAT_COLS`, `levels`, `keys` to module level in `visualization.py`
    - Move `_N_STAT_COLS: int = 3` from inside `create_crf_plot` to module level
    - Extract `levels` and `keys` from inside `compute_statistics` to module-level constants:
      ```python
      _STAT_LEVELS: list[float] = [0.00, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
      _STAT_KEYS:   list[str]   = ["min", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "max", "std"]
      ```
    - Run `uv run python -m pytest tests/` — all tests must pass
    - _Requirements: 15b.8, 15b.9_


- [ ] 16. Fix legacy tests and test layout (Section 9)

  - [ ] 16.1 Remove `TestMergeFinalVideo` from `test_refactor_core.py`
    - Delete the `TestMergeFinalVideo` class from `tests/unit/test_refactor_core.py` — it tests a standalone function API that no longer exists
    - If equivalent `MergePhase` coverage does not exist elsewhere, add a minimal test against the current `MergePhase` object API (construct via `_build_registry`, call `scan()`, assert result type)
    - Run `uv run python -m pytest tests/unit/test_refactor_core.py` — must pass with no errors
    - _Bug_Condition: `TestMergeFinalVideo` calls `merge_final_video(...)` — a standalone function that no longer exists_
    - _Expected_Behavior: no dead test code; any `MergePhase` coverage uses the current phase-object API_
    - _Requirements: 10.1_

  - [ ] 16.2 Move metrics test files to canonical locations
    - Move `tests/test_metrics.py` → `tests/unit/test_metrics.py`
    - Move `tests/test_metrics_integration.py` → `tests/integration/test_metrics_integration.py`
    - Move `tests/test_metrics_orchestrator.py` → `tests/unit/test_metrics_orchestrator.py`
    - Move `tests/test_metrics_properties.py` → `tests/unit/test_metrics_properties.py`
    - Update any `conftest.py` or `pytest.ini` paths if needed
    - Run `uv run python -m pytest tests/` — all tests pass at their new paths; no tests found at old paths
    - _Bug_Condition: metrics test files at `tests/` root instead of `tests/unit/` or `tests/integration/`_
    - _Expected_Behavior: all test files under `tests/unit/` or `tests/integration/`_
    - _Requirements: 10.2_


- [ ] 17. Add "Current State" sections to all specs (Section 10)

  - [ ] 17.1 Add "Current State" section to each completed spec's `requirements.md`
    - For each spec under `.kiro/specs/` (excluding `project-cleanup` itself):
      - Read the spec's `requirements.md` and `design.md`
      - Check file timestamps and `Created`/`Completed` dates to establish timeline
      - Add a `## Current State` section after the header/date metadata and before the Introduction
      - Section should cover (3–10 bullet points): what is still accurate, what has been superseded, any deferred follow-up items
    - Specs to update: `ffmpeg-unified-runner`, `pipeline-metrics-report`, and any others present
    - _Bug_Condition: specs lack a "Current State" section; hard to understand current system state from spec history_
    - _Expected_Behavior: every completed spec has a concise "Current State" section at the top_
    - _Requirements: 11.1_


- [ ] 18. Fix for structural defects — verify exploration test now passes

  - [ ] 18.1 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Structural Defects Eliminated
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected structural state
    - When this test passes, it confirms all structural defects have been eliminated
    - Run `uv run python -m pytest tests/unit/test_cleanup_properties.py::TestBugCondition`
    - **EXPECTED OUTCOME**: Test PASSES (confirms all defects are fixed)
    - _Requirements: 2.1, 2.2, 3.1, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 6.1, 6.2, 7.1, 7.2, 7.3, 7.4, 7.5, 8.1, 8.2, 8.3, 9.1, 9.2, 9.3, 9.4, 9.5, 10.1, 10.2, 11.1_

  - [ ] 18.2 Verify preservation tests still pass
    - **Property 2: Preservation** - Pipeline Behaviour Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run `uv run python -m pytest tests/unit/test_cleanup_properties.py` (preservation tests)
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions in state round-trips or phase construction)
    - Confirm all tests still pass after fix (no regressions)

- [ ] 19. Checkpoint — Ensure all tests pass
  - Run `ruff check pyqenc/` — zero warnings
  - Run `uv run python -m pytest tests/` — all tests pass
  - Run `grep -rn "getattr(" pyqenc/phases/` — zero results on typed PhaseResult fields
  - Run `grep -rn "__all__" pyqenc/phase.py pyqenc/metrics.py pyqenc/phases/__init__.py` — zero results
  - Run `grep -rn "noqa: F401" pyqenc/` — zero results
  - Run `grep -rn "type: ignore\[union-attr\]" pyqenc/` — zero results
  - Confirm `ChunkingParams.chunking_mode`, `OptimizationParams.metrics_sampling`, `MergeParams.metrics_sampling` are non-optional in `state.py`
  - Confirm `_targets_as_strings` exists only in `phase.py`
  - Confirm `_measure_quality`, `_log_metrics_summary`, `_collect_crf_data` are methods of `MergePhase`
  - Ensure all tests pass; ask the user if questions arise

- [ ] 20. Mark spec completed
  - Add `- Completed: YYYY-MM-DD` date to `bugfix.md` header metadata
  - Review this spec against other specs in `.kiro/specs/` and add cross-spec summary notes where relevant (per `agent-specs.md` requirements)

