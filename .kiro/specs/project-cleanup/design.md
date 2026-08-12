# Project Cleanup Bugfix Design

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-11

## Cross-Reference Notes

**Note (2026-06-23 — superseded in part by `config-refactor`):** The `config-refactor` spec (Created: 2026-06-23) makes several of this spec's bug conditions moot:

- **Bug Condition: deferred `ConfigManager` import in `audio.py` / `optimization.py`** — `ConfigManager` is deleted entirely by `config-refactor`. The deferred import and its runtime usage are gone with it.
- **Bug Condition: `AudioConversionProfile.bitrate` field consolidation** — `AudioConversionProfile` is moved to `app_config.py` as a Pydantic model with no `bitrate` field. `base_bitrate` now lives at `AppConfig.audio.audio_base_bitrate`. The scaling logic remains in exactly one place.
- **Bug Condition: `AudioOutputConfig` field structure** — `AudioOutputConfig` is deleted; its responsibilities are absorbed by `AppConfig.audio: AudioConfig`.
- **Bug Condition: `ConfigManager.get_audio_output_config()`** — method gone; audio config is accessed directly via `AppConfig.audio.*`.
- The remaining non-config cleanup items (import hygiene, `__all__`, `getattr` on typed dataclasses, `Phase` ABC, etc.) are unaffected by `config-refactor`.

---

## Overview

The pyqenc codebase has accumulated fourteen categories of structural defects during
rapid feature development. None of these defects cause incorrect runtime output
today, but they collectively make the code harder to refactor safely, hide type
errors at dev-time, and will cause pain as the project grows toward a first
release. This design document formalises the bug condition for each category,
specifies the correct target state, identifies the root causes, and defines the
testing strategy that will confirm the cleanup is complete and regression-free.

The fix is purely structural: no pipeline logic changes, no output changes, no
public API changes. Every existing test must pass after the cleanup.

## Glossary

- **Bug_Condition (C)**: Any of the fifteen structural defect categories currently
  present in the codebase (see Bug Details sections 1–15b).
- **Property (P)**: The desired structural state after the fix — clean imports,
  direct attribute access, proper inheritance, no duplication, correct field
  types, organised tests, and up-to-date specs.
- **Preservation**: All existing pipeline behaviour — output files, quality
  metrics, recovery logic, dry-run reporting, force-wipe, audio conversion —
  must remain byte-for-byte identical after the cleanup.
- **PhaseResult**: Base dataclass in `phase.py` returned by every phase's
  `scan()` / `run()`. Subclasses carry phase-specific typed fields.
- **Phase**: The structural interface (currently a `@runtime_checkable Protocol`)
  that every pipeline phase must satisfy.
- **`_build_registry`**: Factory in `phase.py` that constructs all phase objects
  in execution order and wires their dependencies.
- **Circular import cycle**: The import graph path
  `phase.py → models.py → (would import phases) → phase.py` that currently
  forces all phase imports to be deferred inside function bodies.
- **`TYPE_CHECKING` guard**: `if TYPE_CHECKING:` block whose contents are only
  evaluated by the static type checker, never at runtime — the standard Python
  idiom for breaking import cycles that exist only for type annotations.

## Bug Details

### Bug Condition (unified)

The ten defect categories share a single meta-condition: the codebase contains
patterns that are structurally incorrect for a typed, pre-alpha Python project
targeting clean-state delivery.

```
FUNCTION isBugCondition(location, pattern)
  INPUT:  location — a file path + symbol name in the pyqenc source tree
          pattern  — one of the ten defect categories below
  OUTPUT: boolean

  RETURN (
    pattern == GETATTR_ON_TYPED   AND location uses getattr() on a known typed object
    OR
    pattern == RUNTIME_CHECKABLE  AND Phase is decorated @runtime_checkable
                                  AND a phase class does not inherit from Phase
    OR
    pattern == DEFERRED_IMPORT    AND a runtime import appears inside a function/method body
                                  AND the import is not inside TYPE_CHECKING
    OR
    pattern == ALL_IN_INTERNAL    AND __all__ is defined in phase.py, metrics.py,
                                  or phases/__init__.py
    OR
    pattern == NOQA_SUPPRESSION   AND a # noqa: F401 comment appears on a used import
                                  AND the comment contains a task-tracking annotation
    OR
    pattern == OPTIONAL_FIELD     AND a field is typed X | None
                                  AND the audit table marks it ❌ (no semantic reason for None)
    OR
    pattern == DUPLICATED_HELPER  AND a function body is defined in two or more files
                                  AND the definitions are semantically identical
    OR
    pattern == DANGLING_FUNCTION  AND a module-level function is exclusively used
                                  by a single class in the same file
    OR
    pattern == LEGACY_TEST        AND a test references an API that no longer exists
                                  OR a test file lives outside the canonical test tree
    OR
    pattern == STALE_SPEC         AND a spec in .kiro/specs/ lacks a "Current State" section
    OR
    pattern == AUDIO_BITRATE_SPLIT AND AudioConversionProfile stores per-layout absolute bitrates
                                   AND a separate base_bitrate_override applies scaling again
                                   (two representations of the same concept, scaling duplicated)
    OR
    pattern == DEAD_LOSSLESS_FIELD AND MetricVisualStyle carries lossless_threshold
                                   AND the field is never read during plot rendering
    OR
    pattern == TYPE_IGNORE_UNION   AND a # type: ignore[union-attr] comment appears
                                   AND the suppressed access is on a required dependency
                                   that is typed as | None solely due to the if-phases-else-None
                                   construction pattern
    OR
    pattern == NULLABLE_DEPENDENCY AND a phase stores a required dependency as X | None
                                   AND the dependency is always present in normal operation
  )
END FUNCTION
```

### Examples of Bug Manifestation

**Section 1 — getattr on typed objects**
- `merge.py` `MergePhase.scan()`: `getattr(job_result, "force_wipe", False)` — `job_result` is `JobPhaseResult` which has `force_wipe: bool` directly.
- `merge.py` `MergePhase._execute_merge()`: `getattr(job_result, "crop", None)` and `getattr(job_result, "job", None)` — both are typed fields on `JobPhaseResult`.
- `optimization.py` `OptimizationPhase.run()`: `getattr(job_result, "force_wipe", False)` and `getattr(job_result, "crop", None)` — same issue.
- `cli.py`: `getattr(args, "include", None)` — `include` is always registered on the subparser.

**Section 2 — runtime_checkable Protocol**
- `phase.py` has `@runtime_checkable` on `Phase`.
- `JobPhase`, `MergePhase`, `AudioPhase`, etc. do not inherit from `Phase` — they satisfy it structurally only.
- `isinstance(x, Phase)` checks in tests pass today but would silently pass for any object that happens to have `name`, `dependencies`, `result`, `scan`, `run` attributes.

**Section 3 — Deferred imports**
- `phase.py` `_build_registry`: all seven phase imports are inside the function body.
- `merge.py` `MergePhase.__init__`: imports `AudioPhase`, `EncodingPhase`, `JobPhase` inside `__init__`.
- `optimization.py` `OptimizationPhase.__init__`: imports `ChunkingPhase`, `JobPhase` inside `__init__`.
- `merge.py` `MergePhase.run()`: `from pyqenc.metrics import TimeKey` inside the method body.
- `encoding.py` `_probe_resolution`: `import json as _json` inside the function body.
- `audio.py` `_build_audio_engine` (standalone): `from pyqenc.config import ConfigManager` inside the function.

**Section 4 — `__all__` in internal files**
- `phase.py` exports `__all__` with 8 symbols — it is an internal mechanics file, not a public API.
- `metrics.py` exports `__all__` with task-tracking comments like `# Added in task 7:` and `# Active collector registry (task 19):`.
- `phases/__init__.py` defines `__all__`.

**Section 5 — noqa suppressions**
- `metrics.py`: `import math  # noqa: F401  (used in YamlMetricsCollector — task 7)` — `math` is used in `_compute_convergence`.
- `metrics.py`: `import time as _time  # noqa: F401  (used in YamlMetricsCollector — task 7)` — `_time` is used in `_TimingContext`.
- `models.py`: `from pydantic import ... ConfigDict  # noqa: F401 (ConfigDict used in PipelineConfig)` — `ConfigDict` is used in `model_config = ConfigDict(...)`.

**Section 6 — Excessive `| None` fields**
- `api.py` `_minimal_config`: `quality_targets: list[QualityTarget] | None = None` — callers that want no targets pass nothing; `[]` is the correct sentinel.
- `state.py` `ChunkingParams.chunking_mode: str | None = None` — always written by the code; `None` only exists for old YAML files that have no backward-compat requirement.
- `state.py` `OptimizationParams.metrics_sampling: int | None = None` — same.
- `state.py` `MergeParams.metrics_sampling: int | None = None` — same.

**Section 7 — Duplicated helpers**
- `_targets_as_strings` defined identically in `merge.py` and `optimization.py`.
- `_cleanup_tmp_files` defined in `recovery.py` (canonical) and again in `merge.py` (duplicate).
- `_outcome_from_artifacts` / `_recovery_message` pattern duplicated across `audio.py`, `merge.py`, `chunking.py`, `extraction.py`.

**Section 8 — Dangling standalone functions**
- `merge.py`: `_measure_quality`, `_log_metrics_summary`, `_collect_crf_data` are module-level but only called by `MergePhase`.
- `audio.py`: `_build_audio_engine`, `_build_and_display_dry_run_plan` are module-level but only called by `AudioPhase`.
- `optimization.py`: `_make_encoder`, `_select_test_chunks`, `_delete_encoded_result_sidecars` are module-level but only called by `OptimizationPhase`.
- `encoding.py`: `_probe_resolution`, `_read_metrics_sidecar`, `_write_metrics_sidecar`, `_hardlink_or_copy`, `_write_encoding_result_sidecar`, `_enc_encoded_strategy_dir`, `_recover_encoding_attempts` are module-level but only called by `EncodingPhase` or `ChunkEncoder`.

**Section 9 — Legacy tests**
- `tests/unit/test_refactor_core.py` `TestMergeFinalVideo` calls `merge_final_video(...)` — a standalone function that no longer exists.
- `tests/test_metrics.py`, `tests/test_metrics_integration.py`, `tests/test_metrics_orchestrator.py`, `tests/test_metrics_properties.py` live at `tests/` root instead of `tests/unit/` or `tests/integration/`.

**Section 10 — Stale specs**
- All specs in `.kiro/specs/` lack a "Current State" summary section.

**Section 11 — Audio bitrate config inconsistency**
- `AudioConversionProfile` (in both `config.py` and `audio.py`) stores a per-layout absolute `bitrate` field (e.g. `"192k"` for 2.0, `"576k"` for 5.1).
- The CLI `--audio-bitrate` and API `audio_base_bitrate` parameter is a stereo-equivalent base value; scaling to other layouts is applied in `_build_audio_engine` and again inside `ConversionStrategy._resolve_bitrate` — the same scaling logic in two places.
- `ConversionStrategy` accepts both `profiles` (with absolute bitrates) and `base_bitrate_override` (stereo base), creating two parallel representations with no single source of truth.

**Section 12 — `lossless_threshold` dead field**
- `MetricVisualStyle.lossless_threshold: float | None` is set to `None` for PSNR and `100.0` for SSIM/VMAF, but is never read anywhere in `create_unified_plot` — the lossless count is always computed as `vals >= 100.0`.
- `lossless_label` (`"∞ dB"` / `"100.0"`) encodes metric-specific knowledge inside a generic style struct.

**Section 13 — `# type: ignore` on dependency accesses**
- Every phase has 5–15 `# type: ignore[union-attr]` comments on lines like `self._job.result`, `self._job.scan()`, `self._encoding.result.encoded` — all caused by the `| None` dependency typing.
- `ChunkingPhaseResult.chunks` and `EncodingPhaseResult.encoded` are initialised as `None` with `# type: ignore[assignment]` — the annotation says `list[...]` but the value is `None` at construction.

**Section 14 — Phase dependency attributes typed as `| None`**
- All six dependent phases store their dependency references as `self._job: "_JobPhase | None" = cast(...) if phases else None`.
- The `if phases else None` branch exists to support standalone construction (e.g. in tests), but it means every access to `self._job` downstream requires either a `# type: ignore` or a redundant None check.
- `_ensure_dependencies()` already enforces that dependencies are present before any work is done — the `| None` typing adds no safety, only noise.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- End-to-end pipeline (`pyqenc auto`) produces identical output files and quality metrics.
- All standalone phase invocations (`pyqenc chunk`, `pyqenc audio`, etc.) produce correct output and recover from partial work correctly.
- `pyqenc auto --dry-run` reports what would be done without writing any files.
- `--force` with a source mismatch wipes and re-runs all downstream phases correctly.
- Audio processing with `--audio-convert`, `--audio-codec`, `--audio-bitrate` applies the correct conversion profiles.
- `None` vs `""` semantics for `audio_convert` are preserved: `None` = use config default, `""` = convert nothing.
- All currently-passing tests continue to pass (excluding tests explicitly removed because they test removed APIs).

**Scope:**
All runtime behaviour is out of scope for this cleanup. The fix touches only:
- Import structure (where imports appear, not what is imported)
- Inheritance declarations (adding `Phase` as a base class)
- Attribute access syntax (`result.field` vs `getattr(result, "field", default)`)
- Field type annotations (`str` vs `str | None` for three state fields)
- Function location (module-level vs class method — same logic, different home)
- Test file locations and removal of dead test code
- Spec document additions

**Note:** The actual correct structural state is defined in the Correctness Properties section.
This section focuses on what must NOT change.

## Hypothesized Root Cause

Each defect category has a distinct root cause:

1. **getattr on typed objects**: Defensive coding habit carried over from early
   prototyping when result types were not yet stable. The `getattr` calls were
   never cleaned up once the typed `PhaseResult` subclasses were finalised.

2. **`@runtime_checkable` Protocol**: The `Phase` Protocol was written before
   the phase classes existed. `@runtime_checkable` was added to enable
   `isinstance` checks in tests. Once the phase classes were written, nobody
   went back to add explicit inheritance — structural duck-typing "just worked".

3. **Deferred imports (circular import cycle)**: The real cycle is:
   - `phase.py` imports from `models.py` (for `PhaseOutcome`, `Strategy`, etc.)
   - Phase files (`phases/*.py`) import from `phase.py` (for `Phase`, `PhaseResult`, `Artifact`)
   - If `models.py` were to import from any phase file, a cycle would form.
   - The cycle does NOT actually exist today — `models.py` does not import from
     any phase file. The deferred imports in `_build_registry` and phase
     `__init__` methods were added as a precaution against a cycle that was
     anticipated but never materialised.
   - The correct fix is: move all phase-type imports that are only needed for
     type annotations into `TYPE_CHECKING` guards. Runtime imports (the actual
     class objects needed to construct instances) can then live at the top of
     each file because they do not create a cycle.

4. **`__all__` in internal files**: Added during early development as a habit
   from public library authoring. These files are never imported as a public
   distribution API surface.

5. **noqa suppressions with task comments**: Added incrementally during spec
   execution when linter warnings appeared on imports that were being added
   in stages. The task-tracking comments were never cleaned up after the tasks
   completed.

6. **Excessive `| None` fields**: The three state fields (`ChunkingParams.chunking_mode`,
   `OptimizationParams.metrics_sampling`, `MergeParams.metrics_sampling`) were
   typed `| None` to handle YAML files written before the field was added.
   Since the project is pre-alpha with no backward-compat requirement, the
   correct treatment is to make the field required and treat a missing YAML
   field as a mismatch (triggering a re-run).

7. **Duplicated helpers**: `_targets_as_strings` and `_cleanup_tmp_files` were
   copy-pasted when a second phase needed the same logic. The shared location
   (`phase.py` / `recovery.py`) was not considered at the time.

8. **Dangling standalone functions**: Functions were written as module-level
   helpers during initial implementation and never moved into the class when
   the class was formalised around them.

9. **Legacy tests**: `TestMergeFinalVideo` was written against an older
   standalone-function API that was replaced by the phase-object model. The
   metrics test files were placed at the root `tests/` level before the
   `tests/unit/` / `tests/integration/` structure was established.

10. **Stale specs**: No process existed to update specs after implementation.
    The "Current State" section requirement was added to `agent-specs.md` after
    several specs were already completed.

11. **Audio bitrate split**: The config file was designed with per-layout absolute
    bitrates for flexibility. The CLI/API later added a single base-bitrate override
    for convenience, but instead of replacing the per-layout config approach it was
    layered on top, creating two representations and duplicated scaling logic.

12. **`lossless_threshold` dead field**: Added during early visualization development
    when it was thought each metric might have a different lossless threshold. Once
    all metrics were normalised to 0–100, the threshold became `100.0` for all
    non-PSNR metrics and was never actually used in rendering — but the field was
    never removed.

13. **`# type: ignore` on dependency accesses**: Direct consequence of defect 14
    (nullable dependencies). Once dependencies are typed as non-optional, all
    `# type: ignore[union-attr]` comments on dependency accesses disappear
    automatically. The `# type: ignore[assignment]` on list fields is a separate
    issue — using `field(default_factory=list)` eliminates it.

14. **Nullable required dependencies**: The `if phases else None` pattern was added
    to support constructing phases in isolation (e.g. in early unit tests) without
    wiring a full registry. As the test suite matured, this became unnecessary —
    tests now use `_build_registry` or construct minimal registries. The `| None`
    typing was never cleaned up.

## Correctness Properties

Property 1: Bug Condition — Structural Defects Eliminated

_For any_ location in the pyqenc source tree where `isBugCondition(location, pattern)`
returns `true`, the fixed codebase SHALL satisfy the corresponding structural
requirement: direct attribute access on typed objects, explicit `Phase` inheritance,
top-level imports, no `__all__` in internal files, no task-comment noqa suppressions,
required field types for the three state fields, single canonical definitions for
shared helpers, phase-private methods for single-owner functions, no dead test code,
and "Current State" sections in all specs.

**Validates: Requirements 2.1, 2.2, 3.1, 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 6.1, 6.2,
7.1, 7.2, 7.3, 7.4, 7.5, 8.1, 8.2, 8.3, 9.1, 9.2, 9.3, 9.4, 9.5, 10.1, 10.2, 11.1**

Property 2: Preservation — Pipeline Behaviour Unchanged

_For any_ pipeline invocation where `isBugCondition` does NOT hold (i.e. the
invocation exercises runtime pipeline logic rather than the structural patterns
being cleaned up), the fixed codebase SHALL produce exactly the same output,
logs, and exit codes as the original codebase, preserving all existing
functionality for encoding, audio processing, merging, recovery, dry-run, and
force-wipe flows.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Section 1 — Remove `getattr` on typed objects

**Files:** `pyqenc/phases/merge.py`, `pyqenc/phases/optimization.py`,
`pyqenc/phases/encoding.py`, `pyqenc/phases/audio.py`, `pyqenc/phases/chunking.py`,
`pyqenc/phases/extraction.py`, `pyqenc/cli.py`

**Changes:**
- Replace every `getattr(result, "field", default)` call on a typed `PhaseResult`
  subclass with direct attribute access `result.field`.
- In `cli.py`, replace `getattr(args, "field", None)` with `args.field` for all
  arguments that are always registered on the subparser.
- After the change, run `ruff check` and `mypy` (or pyright) to confirm no new
  type errors are introduced.

Key instances to fix:
- `merge.py` `scan()` and `run()`: `getattr(job_result, "force_wipe", False)` → `job_result.force_wipe`
- `merge.py` `_execute_merge()`: `getattr(job_result, "crop", None)` → `job_result.crop`; `getattr(job_result, "job", None)` → `job_result.job`
- `merge.py` `_get_expected_strategies()`: `getattr(self._encoding.result, "encoded", [])` → `self._encoding.result.encoded`
- `optimization.py` `run()`: `getattr(job_result, "force_wipe", False)` → `job_result.force_wipe`; `getattr(job_result, "crop", None)` → `job_result.crop`
- `optimization.py` `run()`: `getattr(chunking_result, "chunks", [])` → `chunking_result.chunks`

### Section 2 — Convert `Phase` from `@runtime_checkable` Protocol to ABC/base class

**File:** `pyqenc/phase.py` and all seven phase files.

**Architectural decision:** Convert `Phase` from a `Protocol` to an abstract
base class (`ABC`). This is the correct choice because:
- All phase classes are internal — there is no external implementor that needs
  structural duck-typing.
- `ABC` gives compile-time (type-checker) verification that every phase
  implements the required interface.
- `ABC` eliminates the need for `@runtime_checkable` and the fragile
  `isinstance` checks it enables.
- The `Phase` interface has concrete attribute requirements (`name`, `dependencies`,
  `result`) that are better expressed as abstract properties or required
  constructor parameters than as Protocol members.

**Specific changes:**
1. In `phase.py`: remove `@runtime_checkable`, change `class Phase(Protocol)` to
   `class Phase(ABC)`. Mark `scan()` and `run()` as `@abstractmethod`. Keep
   `name`, `dependencies`, `result` as typed class-level annotations (not
   abstract properties — the concrete classes set them in `__init__`).
2. In each phase file: add `Phase` to the class inheritance list, e.g.
   `class MergePhase(Phase):`.
3. Remove `from typing import Protocol, runtime_checkable` from `phase.py`;
   add `from abc import ABC, abstractmethod`.
4. Update any `isinstance(x, Phase)` checks in tests to use direct type checks
   or remove them if they are now redundant.

### Section 3 — Move deferred imports to top of file

**Root cause resolution:** The circular import cycle does not actually exist.
The fix is to audit every deferred import and move it to the top of the file,
using `TYPE_CHECKING` guards only for imports that are needed solely for type
annotations.

**Decision table for each deferred import:**

| Import | Needed at runtime? | Action |
|---|---|---|
| Phase class imports in `_build_registry` | Yes — to call `cls(...)` | Move to top of `phase.py` |
| Phase class imports in phase `__init__` methods | Yes — to call `cast(cls, ...)` | Move to top of each phase file |
| `TYPE_CHECKING` imports already in place | No — annotation only | Keep as-is |
| `TimeKey` in `merge.py` `run()` | Yes — used in `with self._collector.time(TimeKey.RECOVERY)` | Move to top of `merge.py` |
| `ConfigManager` in `audio.py` / `optimization.py` | Yes — instantiated at runtime | Move to top of respective file |
| `json` in `encoding.py` `_probe_resolution` | Yes — used at runtime | Move to top of `encoding.py` |
| `VideoMetadata` in `visualization.py` | Yes — used at runtime | Move to top of `visualization.py` |

**Verification:** After moving all imports, run the full test suite. If any
`ImportError` or `ModuleNotFoundError` appears, it indicates a genuine cycle
that must be resolved by extracting a shared type stub or restructuring.
Expected result: no import errors, because the cycle does not exist.

**Note on `_build_registry`:** Moving the seven phase imports to the top of
`phase.py` is safe because `phase.py` does not import from any phase file at
module level today — the deferred imports were purely precautionary.

### Section 4 — Remove `__all__` from internal files

**Files:** `pyqenc/phase.py`, `pyqenc/metrics.py`, `pyqenc/phases/__init__.py`

**Changes:**
- Delete the `__all__` list from `phase.py`.
- Delete the `__all__` list from `metrics.py`, along with all task-tracking
  comments (`# Added in task 7:`, `# Active collector registry (task 19):`, etc.).
- Delete the `__all__` list from `phases/__init__.py`.
- Verify that no external code relies on `from pyqenc.phase import *` or
  `from pyqenc.metrics import *` (it does not — these are internal modules).

### Section 5 — Remove all `# noqa` suppressions

**Files:** `pyqenc/metrics.py`, `pyqenc/models.py`, `pyqenc/utils/alive.py`, `pyqenc/utils/disk_space.py`, `pyqenc/utils/ffmpeg_runner.py`, `ruff.toml` (or `pyproject.toml` `[tool.ruff]` section)

**F401 suppressions (metrics.py, models.py):**
- Remove all `# noqa: F401` and task-tracking comments from `metrics.py` imports — all imports are genuinely used.
- Remove `# noqa: F401 (ConfigDict used in PipelineConfig)` from `models.py` — `ConfigDict` is used.
- Run `ruff check` after removal to confirm zero F401 warnings.

**E501 suppressions (alive.py):**
- Reformat the two `advance` function signatures across multiple lines to stay within the line length limit. No suppression needed.

**SLF001 suppressions (disk_space.py):**
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
- Update `disk_space.py` to use `video.cached_frame_count` and `video.cached_file_size_bytes` instead of `video._frame_count` and `video._file_size_bytes`. Remove the `# noqa: SLF001` suppressions.

**BLE001 suppressions (ffmpeg_runner.py, metrics.py):**
- These broad `except Exception` catches are intentional: one guards a user-supplied callback, two guard file I/O that must never crash the pipeline.
- Move the suppression to `ruff.toml` (or `pyproject.toml`) as `per-file-ignores`:
  ```toml
  [tool.ruff.lint.per-file-ignores]
  "pyqenc/utils/ffmpeg_runner.py" = ["BLE001"]  # user-supplied callback; must not propagate
  "pyqenc/metrics.py" = ["BLE001"]              # file I/O on resume/flush; must not crash pipeline
  ```
- Remove the inline `# noqa: BLE001` comments from the source files.
- This makes the intent explicit and auditable in one place rather than scattered inline.

### Section 6 — Fix `| None` field types

**Files:** `pyqenc/api.py`, `pyqenc/state.py`

**Changes:**

`api.py` `_minimal_config`:
- Change `quality_targets: list[QualityTarget] | None = None` to
  `quality_targets: list[QualityTarget] = []`.
- Remove the `quality_targets or []` guard in the `PipelineConfig(...)` call.
- Add docstring note to `audio_convert` parameter: `None` means "use config
  default"; `""` means "convert nothing".

`state.py` `ChunkingParams`:
- Change `chunking_mode: str | None = None` to `chunking_mode: str`.
- Update `from_yaml_dict`: if `data.get("chunking_mode")` is `None` or missing,
  raise `ValueError` (or return `None` from `load()` so the caller treats it as
  a mismatch and triggers a re-run). The load path already returns `None` on
  exception, so raising `ValueError` in `from_yaml_dict` is the cleanest approach.
- Update `to_yaml_dict` to always write the field (no change needed — it already does).

`state.py` `OptimizationParams`:
- Change `metrics_sampling: int | None = None` to `metrics_sampling: int`.
- Update `from_yaml_dict`: if `data.get("metrics_sampling")` is `None` or missing,
  raise `ValueError` so `load()` returns `None` and the caller re-runs.
- Update `to_yaml_dict` — no change needed.

`state.py` `MergeParams`:
- Same treatment as `OptimizationParams.metrics_sampling`.

**Load-path implication:** All three fields use the `load()` → `from_yaml_dict()`
pattern. `load()` already catches all exceptions and returns `None`. Making
`from_yaml_dict` raise `ValueError` on a missing/None field means `load()`
returns `None`, which every caller already treats as "no persisted state →
re-run". This is the correct mismatch-on-missing behaviour required by the spec.

**Callers to update:** Any code that currently checks `if params.chunking_mode is None`
or `if params.metrics_sampling is None` must be updated to remove the None branch,
since the field is now always present when `load()` returns a non-None result.

### Section 7 — Consolidate duplicated helpers

**`_targets_as_strings`:**
- Move the definition from `merge.py` to `phase.py` (alongside `PhaseResult`,
  `Artifact`, etc. — it is a shared pipeline utility).
- Delete the definition from `optimization.py`.
- Update imports in both `merge.py` and `optimization.py` to import from `phase.py`.

**`_cleanup_tmp_files`:**
- The canonical definition is in `recovery.py`.
- Delete the duplicate definition from `merge.py`.
- Add `from pyqenc.phases.recovery import _cleanup_tmp_files` to `merge.py`
  (or use the inline `.tmp` cleanup already present in `MergePhase._recover()`
  and remove the standalone call entirely if it is redundant).

**`_outcome_from_artifacts` / `_recovery_message`:**
- These helpers appear in `audio.py`, `merge.py`, `chunking.py`, `extraction.py`
  with identical or near-identical logic.
- Move them to `phase.py` as module-level helpers (they operate on `list[Artifact]`
  which is defined in `phase.py`).
- Delete the per-file definitions and update imports.

### Section 8 — Move dangling standalone functions into their phase class

**`merge.py`:** Move `_measure_quality`, `_log_metrics_summary`, `_collect_crf_data`
into `MergePhase` as private methods (`self._measure_quality`, etc.). Update all
call sites within `MergePhase._execute_merge()`.

**`audio.py`:** Move `_build_audio_engine`, `_build_and_display_dry_run_plan`
into `AudioPhase` as private methods. Update all call sites within `AudioPhase`.

**`optimization.py`:** Move `_make_encoder`, `_select_test_chunks`,
`_delete_encoded_result_sidecars` into `OptimizationPhase` as private methods.
Update all call sites within `OptimizationPhase.run()`.

**`encoding.py`:** Move `_probe_resolution`, `_read_metrics_sidecar`,
`_write_metrics_sidecar`, `_hardlink_or_copy`, `_write_encoding_result_sidecar`,
`_enc_encoded_strategy_dir`, `_recover_encoding_attempts` into `EncodingPhase`
(or `ChunkEncoder` where appropriate — `_probe_resolution`, `_read_metrics_sidecar`,
`_write_metrics_sidecar`, `_hardlink_or_copy`, `_write_encoding_result_sidecar`
are used by `ChunkEncoder`; `_enc_encoded_strategy_dir` and
`_recover_encoding_attempts` are used by `EncodingPhase._recover()`).

**Functions to keep at module level** (cross-cutting or multi-entity):
- `recovery.py`: `_cleanup_tmp_files` — used by multiple phases.
- `chunking.py`: `_chunk_name_duration`, `_expand_scenes` — used by both
  `ChunkingPhase` and recovery logic.
- `audio.py`: `_filename_prefix`, `_is_raw_source` — used by multiple strategy
  classes, not owned by `AudioPhase`.
- `phase.py`: `_targets_as_strings` (after move from merge/optimization) —
  used by multiple phases.

Add a brief comment above each kept module-level function explaining why it is
not a method (e.g. `# Cross-cutting utility used by MergePhase and OptimizationPhase.`).

### Section 9 — Fix legacy tests and test layout

**`tests/unit/test_refactor_core.py`:**
- Remove the `TestMergeFinalVideo` class entirely (it tests a standalone function
  API that no longer exists).
- If equivalent coverage of `MergePhase` does not exist elsewhere, add a minimal
  test against the current `MergePhase` object API. If coverage exists, removal
  alone is sufficient.

**Metrics test files:**
- Move `tests/test_metrics.py` → `tests/unit/test_metrics.py`
- Move `tests/test_metrics_integration.py` → `tests/integration/test_metrics_integration.py`
- Move `tests/test_metrics_orchestrator.py` → `tests/unit/test_metrics_orchestrator.py`
- Move `tests/test_metrics_properties.py` → `tests/unit/test_metrics_properties.py`
- Update any `conftest.py` or `pytest.ini` paths if needed.
- Run the full test suite after the move to confirm all tests pass.

### Section 10 — Add "Current State" sections to all specs

**Files:** All `requirements.md` files under `.kiro/specs/`.

For each spec, add a `## Current State` section at the top of `requirements.md`
(after the header and date metadata, before the Introduction) describing:
- What is still accurate in the spec.
- What has been superseded or changed since the spec was completed.
- Any follow-up items that were deferred.

The section should be concise (3–10 bullet points) and written from the
perspective of a developer reading the spec for the first time today.

### Section 11 — Unify audio bitrate representation

**Files:** `pyqenc/config.py`, `pyqenc/phases/audio.py`, `pyqenc/default_config.yaml`, `pyqenc/models.py`

**Problem:** Two representations of the same concept exist side-by-side:
- `AudioConversionProfile.bitrate` — per-layout absolute bitrate stored in config and passed through the call chain.
- `audio_base_bitrate` / `base_bitrate_override` — stereo-equivalent base value that is scaled proportionally at two separate call sites.

**Changes:**
1. Remove `bitrate` from `AudioConversionProfile` in both `config.py` and `audio.py`. The profile now carries only `codec` and `extension`.
2. Update `default_config.yaml` `audio_output.profiles` to remove per-layout `bitrate` entries. Add a single `audio_output.base_bitrate` key (stereo-equivalent, e.g. `"192k"`).
3. `ConfigManager.get_audio_output_config()` reads `base_bitrate` from the config and stores it on `AudioOutputConfig` as a single `base_bitrate: str` field.
4. The scaling logic (`base_kbps * channels / 2`) lives in exactly one place: `ConversionStrategy._resolve_bitrate`. It reads the base bitrate from `AudioOutputConfig.base_bitrate`, overridden by `audio_base_bitrate` when provided.
5. Remove `base_bitrate_override` from `ConversionStrategy.__init__` — the strategy now always reads from the single source. The `audio_base_bitrate` override is applied by replacing `AudioOutputConfig.base_bitrate` before constructing the strategy.
6. Update all docstrings and CLI help to state clearly: "stereo (2.0) equivalent; other channel layouts are scaled proportionally by channel count."

### Section 12 — Remove `lossless_threshold` and `lossless_label` from `MetricVisualStyle`

**File:** `pyqenc/utils/visualization.py`

**Changes:**
1. Remove `lossless_threshold: float | None` from `MetricVisualStyle`.
2. Remove `lossless_label: str` from `MetricVisualStyle`.
3. In the summary box rendering code, derive the lossless label directly from `MetricType`:
   - `MetricType.PSNR` → `"∞ dB"` (PSNR lossless is infinite)
   - `MetricType.SSIM` / `MetricType.VMAF` → `"100.0"` (normalised scale)
   This is a one-line `match` or `dict` lookup at the point of use.
4. The lossless count computation (`vals >= 100.0`) is already correct and unchanged.
5. Update `DEFAULT_METRIC_STYLES` to remove the two fields from all three entries.

### Section 13 — Eliminate `# type: ignore` on project code

**Files:** All phase files, `pyqenc/phase.py`, `pyqenc/phases/chunking.py`, `pyqenc/phases/encoding.py`

**Changes driven by other sections:**
- Sections 2 and 14 (ABC inheritance + non-optional dependencies) eliminate all `# type: ignore[union-attr]` on dependency accesses automatically.
- Replacing `chunks: list[ChunkMetadata] = None  # type: ignore[assignment]` with `chunks: list[ChunkMetadata] = field(default_factory=list)` in `ChunkingPhaseResult` and `EncodingPhaseResult` eliminates the `# type: ignore[assignment]` suppressions.

**Remaining acceptable suppressions (do not remove):**
- `config_handler.set_global(enrich_print=False)  # type: ignore` — third-party stub limitation in `alive_progress`.
- `# type: ignore[arg-type]` on `proc.stdout` / `proc.stderr` / `proc.returncode` in `ffmpeg_runner.py` — stdlib `asyncio.subprocess` typing limitation.
- `# type: ignore[return-value]` on `dict(zip(keys, stats))` in `visualization.py` — `TypedDict` construction from `zip` is not inferrable by the type checker.

**Goal:** Zero `# type: ignore` comments on project-owned code paths. Only third-party / stdlib limitations remain.

### Section 14 — Make phase dependency attributes non-optional

**Files:** All phase files (`audio.py`, `chunking.py`, `encoding.py`, `extraction.py`, `merge.py`, `optimization.py`)

**Problem:** Every phase stores required dependencies as `X | None` with `cast(...) if phases else None`, forcing `# type: ignore[union-attr]` on every access and making `_ensure_dependencies()` check for `None` instead of just checking `result` readiness.

**Changes:**
1. Remove the `if phases else None` fallback from all phase `__init__` methods. The `phases` registry is always provided in normal operation (via `_build_registry`).
2. Change dependency attribute types from `"_JobPhase | None"` to `"JobPhase"` (non-optional). Use `TYPE_CHECKING` guard for the import if needed.
3. `_ensure_dependencies()` in each phase simplifies to: check that `dep.result` is populated; if not, call `dep.scan()` or `dep.run()`. No more `if self._job is None` guards.
4. For tests that construct phases in isolation: pass a minimal registry built with `_build_registry` or construct a stub `JobPhase` directly. The `phases` parameter remains in `__init__` but is now required (no default `None`).
5. `self.dependencies: list[Phase]` is populated directly from the typed attributes — no more `[d for d in [...] if d is not None]` filter.

### Section 15 — Consolidate misplaced constants

**Files:** `pyqenc/constants.py`, all phase files, `pyqenc/metrics.py`, `pyqenc/quality.py`, `pyqenc/utils/visualization.py`, `pyqenc/phases/audio.py`

**Phase YAML filenames → class-level `SIDECAR` constants:**
Move each phase's YAML filename from module level into the phase class as a class-level constant named `SIDECAR`:
```python
class JobPhase(Phase):
    SIDECAR: str = "job.yaml"

class ChunkingPhase(Phase):
    SIDECAR: str = "chunking.yaml"
# ... etc.
```
Update all references within each class from the old module-level name → `self.SIDECAR` / `ClassName.SIDECAR`.

**`METRICS_YAML_FILENAME` → `YamlMetricsCollector` class constant:**
Move to a class-level constant on `YamlMetricsCollector` (e.g. `YamlMetricsCollector.SIDECAR = "metrics.yaml"`). It is the output artifact of that class, analogous to how each phase owns its YAML filename.

The orchestrator currently logs `"Metrics written to: %s"` after calling `collector.flush()` — this is wrong ownership. The collector itself knows where it writes; the orchestrator does not need to. Move this logging into `YamlMetricsCollector.flush()` at `debug` level. `NoOpMetricsCollector.flush()` SHALL log `"Metrics collection disabled (no-op collector)"` at `debug` level. The orchestrator removes the metrics path logging entirely.

**EBU R128 constants → `constants.py`:**
Move `_LOUDNORM_TARGET_I`, `_LOUDNORM_TARGET_TP`, `_LOUDNORM_TARGET_LRA` from `audio.py` to `constants.py` under an `# Audio processing` group. These are standard values, not implementation details.

**`_INTERMEDIATE_CODEC` / `_INTERMEDIATE_EXTENSION` → strategy class constants:**
These are used only by the audio strategy classes. Move to a class-level constant on `BaseStrategy` (or the specific strategies that use them).

**`_CHANNEL_COUNTS` → `ConversionStrategy` class constant:**
```python
class ConversionStrategy(BaseStrategy):
    _CHANNEL_COUNTS: dict[str, int] = {
        "ch=2.0": 2, "ch=stereo": 2, "ch=5.1": 6, "ch=7.1": 8,
    }
```

**`_TEMP_SUFFIX` in `metrics.py` → remove, use `constants.TEMP_SUFFIX`:**
Delete `_TEMP_SUFFIX = ".tmp"` from `metrics.py` and replace its two usages with `TEMP_SUFFIX` imported from `constants`.

**`_MAX_METRIC` in `quality.py` → `constants.py`:**
Move to `constants.py` under a `# Metric normalization` group. It is the normalized scale upper bound — a domain constant used in `quality.py` and potentially elsewhere.

**`_N_STAT_COLS` inside `create_crf_plot` → module level:**
Move `_N_STAT_COLS: int = 3` to module level in `visualization.py` alongside the other layout constants.

**`levels` / `keys` inside `compute_statistics` → module level:**
```python
_STAT_LEVELS: list[float] = [0.00, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
_STAT_KEYS:   list[str]   = ["min", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "max", "std"]
```
These are fixed — allocating them on every `compute_statistics` call is wasteful.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, confirm the structural
defects are present on the unfixed code (exploratory), then verify the fix
eliminates each defect and preserves all runtime behaviour (fix + preservation).

Because this is a structural cleanup rather than a logic bug, the primary
validation tools are:
- **Static analysis** (`ruff check`, type checker) — catches import errors,
  unused imports, type mismatches introduced by the cleanup.
- **Unit tests** — verify that phase objects can be constructed and that the
  `Phase` ABC is correctly inherited.
- **Property-based tests** — verify that the state field changes (`str` /
  `int` instead of `str | None` / `int | None`) behave correctly across a
  range of YAML inputs.
- **Integration tests** — verify end-to-end pipeline behaviour is unchanged.

### Exploratory Bug Condition Checking

**Goal:** Confirm each defect is present before fixing it. Surface the exact
locations so the fix is targeted.

**Test Plan:** Run `ruff check` and `grep`-based checks on the unfixed code to
enumerate all instances of each defect pattern. Run the existing test suite to
establish a baseline pass/fail count.

**Expected findings before fix:**
- `ruff check` reports no F401 errors (they are suppressed) but the suppressions
  themselves are visible in the source.
- `grep -rn "getattr(" pyqenc/phases/` lists all `getattr` calls on typed results.
- `grep -rn "from pyqenc" pyqenc/phases/*/` inside function bodies lists all
  deferred imports.
- `pytest tests/unit/test_refactor_core.py::TestMergeFinalVideo` fails with
  `AttributeError` or `ImportError` (the standalone function no longer exists).
- `pytest tests/test_metrics*.py` passes but files are at the wrong path level.

### Fix Checking

**Goal:** Verify that for all locations where `isBugCondition` held, the fixed
codebase satisfies the structural requirement.

```
FOR ALL (location, pattern) WHERE isBugCondition(location, pattern) DO
  result := inspect_fixed_codebase(location, pattern)
  ASSERT structurallyCorrect(result, pattern)
END FOR
```

**Verification steps after fix:**
1. `ruff check pyqenc/` — zero warnings (no suppressed imports, no unused imports).
2. Type checker (`mypy` or `pyright`) on `pyqenc/` — zero new errors.
3. `grep -rn "getattr(" pyqenc/phases/` — zero results on typed PhaseResult fields.
4. `grep -rn "^    from pyqenc" pyqenc/` — zero results (no deferred runtime imports inside functions).
5. `grep -rn "__all__" pyqenc/phase.py pyqenc/metrics.py pyqenc/phases/__init__.py` — zero results.
6. `grep -rn "noqa: F401" pyqenc/` — zero results.
7. Confirm `ChunkingParams.chunking_mode`, `OptimizationParams.metrics_sampling`,
   `MergeParams.metrics_sampling` are non-optional in `state.py`.
8. Confirm `_targets_as_strings` exists only in `phase.py`.
9. Confirm `_cleanup_tmp_files` exists only in `recovery.py`.
10. Confirm `_measure_quality`, `_log_metrics_summary`, `_collect_crf_data` are
    methods of `MergePhase`, not module-level functions.
11. `pytest tests/` — all tests pass; `TestMergeFinalVideo` is gone; metrics
    tests are found at their new paths.

### Preservation Checking

**Goal:** Verify that for all inputs where `isBugCondition` does NOT hold (i.e.
runtime pipeline invocations), the fixed codebase produces the same result as
the original.

```
FOR ALL invocation WHERE NOT isBugCondition(invocation) DO
  ASSERT fixed_pipeline(invocation) == original_pipeline(invocation)
END FOR
```

**Testing Approach:** Property-based testing is used for the state field changes
(Section 6) because the load/save round-trip must hold across a wide range of
YAML inputs. Integration tests cover the full pipeline flow.

**Test Cases:**

1. **State field round-trip (PBT):** Generate random valid `ChunkingParams`,
   `OptimizationParams`, `MergeParams` instances, serialise to YAML, deserialise,
   and assert the round-trip is lossless. Also generate YAML dicts with missing
   `chunking_mode` / `metrics_sampling` fields and assert `load()` returns `None`.

2. **Pipeline dry-run preservation:** Run `pyqenc auto --dry-run` on the sample
   video before and after the fix; assert identical output.

3. **Phase construction preservation:** Construct all seven phase objects via
   `_build_registry` and assert `isinstance(phase, Phase)` for each (now
   verified by ABC inheritance rather than structural duck-typing).

4. **Audio conversion preservation:** Run `process_audio()` with `audio_convert=None`
   and `audio_convert=""` and assert the two cases produce different behaviour
   (None = config default, "" = convert nothing).

5. **Force-wipe preservation:** Run a pipeline with `--force` after changing the
   source file; assert all downstream phases are wiped and re-run.

### Unit Tests

- Test that each phase class is a subclass of `Phase` (ABC inheritance check).
- Test that `Phase` cannot be instantiated directly (abstract class enforcement).
- Test `ChunkingParams.from_yaml_dict` raises `ValueError` when `chunking_mode`
  is missing or `None`.
- Test `OptimizationParams.from_yaml_dict` raises `ValueError` when
  `metrics_sampling` is missing or `None`.
- Test `MergeParams.from_yaml_dict` raises `ValueError` when `metrics_sampling`
  is missing or `None`.
- Test `_targets_as_strings` imported from `phase.py` produces correct output.
- Test `_outcome_from_artifacts` and `_recovery_message` imported from `phase.py`.

### Property-Based Tests

- Generate random `ChunkingParams` with valid `chunking_mode` strings; assert
  `load(save(params))` round-trip is lossless.
- Generate random YAML dicts for `ChunkingParams` with `chunking_mode` absent
  or `None`; assert `ChunkingParams.load()` returns `None`.
- Same two properties for `OptimizationParams.metrics_sampling` and
  `MergeParams.metrics_sampling`.
- Generate random `list[QualityTarget]`; assert `_targets_as_strings` output
  can be round-tripped through `QualityTarget.parse`.

### Integration Tests

- Full pipeline run on sample video: assert output files exist and quality
  metrics match pre-fix baseline.
- Standalone `extract_streams`, `chunk_video`, `encode_chunks`, `process_audio`,
  `merge_final` API calls: assert each returns a result with `is_complete=True`.
- Import smoke test: `python -c "from pyqenc.phase import _build_registry"` —
  assert no `ImportError` (confirms deferred imports were safely moved to top level).
