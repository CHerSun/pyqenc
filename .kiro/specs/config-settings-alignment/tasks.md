# Implementation Plan: Config Settings Alignment

<!-- markdownlint-disable MD024 -->

- Created: 2026-08-12
- Completed: 2026-08-29

## Notes

All changes are renames and restructuring — no new pipeline behavior. Tasks are sequenced so infrastructure changes (constants, models) come first, then dependent code (phases, CLI), then YAML, then tests, then verification.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1"]},
    {"wave": 2, "tasks": ["2"]},
    {"wave": 3, "tasks": ["3", "4", "5"]},
    {"wave": 4, "tasks": ["6"]},
    {"wave": 5, "tasks": ["7"]},
    {"wave": 6, "tasks": ["8"]},
    {"wave": 7, "tasks": ["9"]},
    {"wave": 8, "tasks": ["10"]}
  ]
}
```

## Tasks

- [x] 1. Delete `DEFAULT_MAX_PARALLEL` and `DEFAULT_METRICS_SAMPLING` from `constants.py`
  - [x] 1.1 Delete `DEFAULT_MAX_PARALLEL` from `pyqenc/constants.py` and its docstring
  - [x] 1.2 Delete `DEFAULT_METRICS_SAMPLING` from `pyqenc/constants.py` and its docstring
  - [x] 1.3 Verify no remaining callers: search for `DEFAULT_MAX_PARALLEL` and `DEFAULT_METRICS_SAMPLING` across the codebase; any remaining use is a bug to fix before proceeding

- [x] 2. Restructure `app_config.py` — rename fields, add `MeasurementConfig`, make value-bearing fields required
  - [x] 2.1 Add `MeasurementConfig(BaseModel)` with `sampling: int` — no default (required); add docstring
  - [x] 2.2 Update `EncodingConfig`: rename `quality_targets` → `targets`; rename `max_parallel` → `concurrency`; rename `strategy_selection_tolerance` → `optimize_tolerance`; remove `metrics_sampling`; remove `crop_params`; **no** `measurement` field here; remove all Python defaults from value-bearing fields (`= True`, `= 5.0`, etc.) — make them required; update all docstrings
  - [x] 2.3 Update `EncodingConfig.resolve()` and all internal references within `app_config.py` to use new field names
  - [x] 2.4 Replace `AudioConfig` entirely: remove `convert_filter`, `profiles`, `audio_codec`, `audio_base_bitrate`; add `convert_pattern: str`, `codec: str`, `bitrate_per_channel: str`, `extension: str` — all required (no Python defaults); update docstring
  - [x] 2.5 Delete `AudioConversionProfile` class from `app_config.py`
  - [x] 2.6 Add `measurement: MeasurementConfig` as a **top-level** required field on `AppConfig` (no `Field(default_factory=...)`); make all `AppConfig` sub-model fields required; `ExtractionConfig` sentinel fields (`include: str | None = None`, `exclude: str | None = None`) stay as-is
  - [x] 2.7 Update `AppConfig` docstring to reference new field names and note required-field semantics
  - [x] 2.8 Remove `DEFAULT_MAX_PARALLEL` and `DEFAULT_METRICS_SAMPLING` imports from `app_config.py`

- [x] 3. Update `pyqenc/phases/audio.py` — flat `AudioConfig` and bitrate scaling
  - [x] 3.1 Remove all profile lookup logic (`config.audio.profiles`, `AudioConversionProfile` references)
  - [x] 3.2 Read `config.audio.codec`, `config.audio.extension` directly for conversion output
  - [x] 3.3 Implement bitrate scaling: parse `config.audio.bitrate_per_channel` (strip `k`/`K` suffix, parse int, multiply by channel count from stream filename tags, re-append `k`); handle `2.0`/`stereo` → ×2, `5.1` → ×6, `7.1` → ×8
  - [x] 3.4 Replace `config.audio.convert_filter` → `config.audio.convert_pattern`
  - [x] 3.5 Replace `config.audio.audio_codec` and `config.audio.audio_base_bitrate` references with `config.audio.codec` and the computed bitrate

- [x] 4. Update all other phases — field renames
  - [x] 4.1 Replace `config.encoding.quality_targets` → `config.encoding.targets` in all phase files (`optimization.py`, `encoding.py`, `merge.py`, and any other that reads quality targets)
  - [x] 4.2 Replace `config.encoding.max_parallel` → `config.encoding.concurrency` in `encoding.py` (and any other phase)
  - [x] 4.3 Replace `config.encoding.metrics_sampling` → `config.measurement.sampling` in all phase files (`optimization.py`, `encoding.py`, `merge.py`, `measure.py`, any others)
  - [x] 4.4 Replace `config.encoding.strategy_selection_tolerance` → `config.encoding.optimize_tolerance` in `optimization.py` and any other reader
  - [x] 4.5 Replace `config.encoding.crop_params` (or `self._job.result.config.encoding.crop_params`) → `self._job.result.crop` in all phase files

- [x] 5. Update `pyqenc/phases/job.py` — remove `crop_params` from config path
  - [x] 5.1 Remove any code reading `self._config.encoding.crop_params`; read crop from `self._crop_params` stored at construction (already arrives via `JobPhaseResult.crop`)
  - [x] 5.2 Confirm `JobPhaseResult.crop` is populated correctly from the volatile value (no change needed if already wired; verify only)

- [x] 6. Update `pyqenc/phase.py` — thread `crop_params` as volatile kwarg
  - [x] 6.1 Add `crop_params: CropParams | None = None` parameter to `_build_registry`
  - [x] 6.2 Forward `crop_params` to the `JobPhase` constructor call

- [x] 7. Update `pyqenc/cli.py` — rename args, remove audio CLI args, fix `default=None`
  - [x] 7.1 Rename `--chunking` → `--chunking-mode`, dest `chunking` → `chunking_mode`; update help text
  - [x] 7.2 Rename `--all-strategies` → `--no-optimize`, dest `all_strategies` → `no_optimize`; update help text
  - [x] 7.3 Rename `--max-parallel` → `--concurrency`, dest `max_parallel` → `concurrency`; change `default=DEFAULT_MAX_PARALLEL` → `default=None`; update help text
  - [x] 7.4 Update `--targets` dest from `quality_target` to `targets`
  - [x] 7.5 Change `--scene-threshold` and `--min-scene-length` to `default=None` (currently repeat config values)
  - [x] 7.6 Delete `_add_audio_convert_arguments()` function
  - [x] 7.7 Remove `_add_audio_convert_arguments()` call from every subcommand that had it (`auto`, `audio`, `merge`)
  - [x] 7.8 In `_build_config()`: update all config assignment paths to new names; remove audio override block (`audio_convert`, `audio_codec`, `audio_bitrate`); update `--sampling` assignment to `config.measurement.sampling`; update `--no-optimize` handling (was `--all-strategies`); update `--concurrency` (was `--max-parallel`); update `--chunking-mode` (was `--chunking`); update `--targets` dest reference; update `--scene-threshold` and `--min-scene-length` guards to check `is not None`
  - [x] 7.9 Move `crop_params` resolution out of `_build_config()`; in each command handler that uses `--crop`, pass `crop_params` as kwarg to `_build_registry` instead of assigning to config
  - [x] 7.10 Remove `DEFAULT_MAX_PARALLEL` and `DEFAULT_METRICS_SAMPLING` imports from `cli.py`

- [x] 8. Rewrite `pyqenc/default_config.yaml`
  - [x] 8.1 Rewrite with revised structure: all settings present, comments short and inline, no multi-paragraph block headers
  - [x] 8.2 `encoding.quality_targets` → `encoding.targets`
  - [x] 8.3 `encoding.max_parallel` → `encoding.concurrency`
  - [x] 8.4 `encoding.metrics_sampling` → top-level `measurement.sampling` section (same level as `encoding:`, not nested inside it)
  - [x] 8.5 `encoding.strategy_selection_tolerance` → `encoding.optimize_tolerance`
  - [x] 8.6 `audio` section: replace profiles/audio_codec/audio_base_bitrate with `convert_pattern`, `codec`, `bitrate_per_channel`, `extension`
  - [x] 8.7 Confirm all `AppConfig` fields (including `MeasurementConfig.sampling`) are present with their defaults; confirm no `crop_params` entry
  - [x] 8.8 Shorten strategy pattern syntax guide to the 6 essential examples inline in the commented-out strategy list
  - [x] 8.9 Retain codec and profile sections with existing inline comments (they are already well-commented)

- [x] 9. Add/update tests for the changed areas
  - [x] 9.1 Update existing `AppConfig` round-trip and property-based tests to use new field names (`targets`, `concurrency`, `optimize_tolerance`, `measurement.sampling` as top-level `config.measurement.sampling`)
  - [x] 9.2 Add unit test for audio bitrate scaling: given `bitrate_per_channel="96k"`, verify 2.0 → `"192k"`, 5.1 → `"576k"`, 7.1 → `"768k"`, `stereo` → `"192k"` — bug condition: wrong channel count multiplier or incorrect string parsing
  - [x] 9.3 Add unit test verifying `AppConfig.model_validate({})` raises `ValidationError` — bug condition: Python defaults silently masking missing YAML keys
  - [x] 9.4 Run `uv run python -m pytest` and confirm all tests pass

- [x] 10. Smoke test and cross-reference update
  - [x] 10.2 Run `uv run pyqenc config .` and confirm it reflects new field names in the displayed config
  - [x] 10.3 Review this spec against `config-refactor` and other related specs; update `config-refactor/design.md` cross-reference table to note fields superseded by this spec
  - [x] 10.4 Set `Completed:` date in `design.md`, `requirements.md`, and `tasks.md`
