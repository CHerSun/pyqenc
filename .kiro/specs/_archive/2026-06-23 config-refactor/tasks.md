# Implementation Plan: Config Refactor

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-23
- Completed: 2026-07-06

## Notes

Implementation tasks for the config refactor. Sequenced so new infrastructure is created first, tests second, then phases are migrated bottom-up (JobPhase → downstream phases → CLI/API), and legacy types are deleted last.

## Overview

This refactor replaces `ConfigManager` + `PipelineConfig` with `AppConfig` (layered Pydantic-validated). Volatile per-run parameters are plain kwargs passed to `_build_registry` and only to `JobPhase`, which stores them as typed fields on `JobPhaseResult`. Tasks are sequenced: new infrastructure first, then tests, then phases bottom-up, then CLI/API, then legacy deletion, then smoke test.

## Task Dependency Graph

```json
{
  "waves": [
    {"wave": 1, "tasks": ["1", "2"]},
    {"wave": 2, "tasks": ["3"]},
    {"wave": 3, "tasks": ["4"]},
    {"wave": 4, "tasks": ["5"]},
    {"wave": 5, "tasks": ["6", "7", "8", "9", "10", "11", "12"]},
    {"wave": 6, "tasks": ["13"]},
    {"wave": 7, "tasks": ["14", "15"]},
    {"wave": 8, "tasks": ["16"]},
    {"wave": 9, "tasks": ["17"]},
    {"wave": 10, "tasks": ["18"]}
  ]
}
```

## Tasks

- [x] 1. Create `pyqenc/app_config.py` — AppConfig and loader
  - [x] 1.1 Implement `_deep_merge(base: dict, override: dict) -> dict` — scalar override wins, dict keys merge recursively, lists fully replace
  - [x] 1.2 Implement `AudioConversionProfile` as Pydantic `BaseModel` (codec, bitrate, extension fields) — replaces dataclass from `config.py`
  - [x] 1.3 Implement `ProfileConfig` as Pydantic `BaseModel` (codec, description, extra_args fields) — replaces `EncodingProfile` dataclass
  - [x] 1.4 Implement `ExtractionConfig(BaseModel)` with `include: str | None` and `exclude: str | None` fields
  - [x] 1.5 Implement `ChunkingConfig(BaseModel)` with `mode: ChunkingMode`, `scene_threshold: float`, `min_scene_length: int` fields
  - [x] 1.6 Implement `EncodingConfig(BaseModel)` with all encoding fields (`quality_targets: list[str]`, `strategies: list[str]`, `optimize`, `max_parallel`, `metrics_sampling`, `visual_hash`, `strategy_selection_tolerance`, `crop_params`); add `PrivateAttr` fields `_resolved_targets` and `_resolved_strategies`; implement `resolve(codecs, profiles)` method that populates private fields exactly once
  - [x] 1.7 Implement `AudioConfig(BaseModel)` with `convert_filter`, `profiles: dict[str, AudioConversionProfile]`, `audio_codec`, `audio_base_bitrate` fields
  - [x] 1.8 Implement `AppConfig(BaseModel)` with `extraction`, `chunking`, `encoding`, `audio`, `codecs: dict[str, CodecConfig]`, `profiles: dict[str, ProfileConfig]` fields; add `model_validator(mode='after')` that calls `self.encoding.resolve(self.codecs, self.profiles)`
  - [x] 1.9 Implement `load_app_config() -> AppConfig` — discovers bundled default, optional home config, optional cwd config; deep-merges in priority order; calls `AppConfig.model_validate(merged)`
  - [x] 1.10 Add `resolved_targets` and `resolved_strategies` properties on `EncodingConfig` that return the cached private fields (populated by `resolve()`)

- [x] 2. Update `pyqenc/default_config.yaml` — restructure to new namespace
  - [x] 2.1 Rename `default_targets` → `encoding.quality_targets`
  - [x] 2.2 Rename `default_strategies` → `encoding.strategies`
  - [x] 2.3 Rename `metrics.sampling` → `encoding.metrics_sampling`; add remaining `EncodingConfig` defaults (`optimize: true`, `max_parallel: 1`, `visual_hash: true`, `strategy_selection_tolerance: 5.0`)
  - [x] 2.4 Rename `streams.include/exclude` → `extraction.include/exclude`
  - [x] 2.5 Rename `audio_output.convert_filter/profiles` → `audio.convert_filter/profiles`; add `audio.audio_codec: null` and `audio.audio_base_bitrate: null`
  - [x] 2.6 Add `chunking` section with `mode: lossless`, `scene_threshold: 0.3`, `min_scene_length: 24`
  - [x] 2.7 Keep `codecs` and `profiles` sections unchanged

- [x] 3. Write property-based tests for `_deep_merge` and `AppConfig`
  - [x] 3.1 Write PBT for Property 1 (deep-merge preserves base scalar when override absent)
  - [x] 3.2 Write PBT for Property 2 (deep-merge override wins on scalar conflict)
  - [x] 3.3 Write PBT for Property 3 (deep-merge recursively merges nested dicts)
  - [x] 3.4 Write PBT for Property 4 (deep-merge fully replaces lists)
  - [x] 3.5 Write PBT for Property 8 (layer priority ordering — three-layer merge)
  - [x] 3.6 Write PBT for Property 5 (AppConfig model_dump → model_validate round-trip)
  - [x] 3.7 Write PBT for Property 7 (strategy resolution is deterministic and idempotent)
  - [x] 3.8 Write PBT for Property 9 (strategy deduplication by preset+profile)
  - [x] 3.9 Write test for Property 10 (ValidationError on invalid strategy/target strings)
  - [x] 3.10 Write test: `load_app_config()` with only bundled default produces valid `AppConfig`

- [x] 4. Update `pyqenc/phase.py` — `_build_registry` signature
  - [x] 4.1 Update `_build_registry` signature to `(config: AppConfig, source: Path, work_dir: Path, force: bool, cleanup: CleanupLevel, no_metrics: bool, collector: MetricsCollector)`; update the `JobPhase` constructor call to forward all volatile kwargs; leave all other phase constructor calls as `(config, registry, collector=collector)`; update TYPE_CHECKING imports

- [x] 5. Update `pyqenc/phases/job.py` — JobPhase and JobPhaseResult
  - [x] 5.1 Add fields `config: AppConfig`, `work_dir: Path`, `source: Path`, `cleanup: CleanupLevel`, `no_metrics: bool` to `JobPhaseResult` dataclass (all default `None`/zero-value as needed for `field(default=...)`)
  - [x] 5.2 Update `JobPhase.__init__` to accept plain volatile kwargs `(config, phases, *, source, work_dir, force, cleanup, no_metrics, collector)`; store each as `self._source`, `self._work_dir`, `self._force`, `self._cleanup`, `self._no_metrics`
  - [x] 5.3 Replace all `self._config.source_video` → `self._source`, `self._config.work_dir` → `self._work_dir`, `self._config.force` → `self._force`
  - [x] 5.4 Replace all `self._config.crop_params` → `self._config.encoding.crop_params`, `self._config.chunking_mode` → `self._config.chunking.mode`, `self._config.strategies` → `self._config.encoding.resolved_strategies`, `self._config.optimize` → `self._config.encoding.optimize`
  - [x] 5.5 Populate all new `JobPhaseResult` fields (`config`, `work_dir`, `source`, `cleanup`, `no_metrics`) in every `JobPhaseResult(...)` construction path (scan, run-dry, run-execute)

- [x] 6. Update `pyqenc/phases/extraction.py` — ExtractionPhase
  - [x] 6.1 Constructor stays `(config, phases, *, collector)` — remove any `context` parameter if present
  - [x] 6.2 Replace `self.params = ExtractionParams(include=config.include, exclude=config.exclude)` → `ExtractionParams(include=config.extraction.include, exclude=config.extraction.exclude)`
  - [x] 6.3 Replace all `self._config.source_video` → `self._job.result.source`, `self._config.work_dir` → `self._job.result.work_dir`
  - [x] 6.4 Replace all other `self._config.*` reads with `self._job.result.config.*` equivalents

- [x] 7. Update `pyqenc/phases/chunking.py` — ChunkingPhase
  - [x] 7.1 Constructor stays `(config, phases, *, collector)` — no context parameter
  - [x] 7.2 Replace `self._config.chunking_mode` → `self._job.result.config.chunking.mode`
  - [x] 7.3 Replace `self._config.source_video`/`work_dir` → `self._job.result.source`/`work_dir`
  - [x] 7.4 Replace scene threshold / min scene length to come from `self._job.result.config.chunking.scene_threshold` / `min_scene_length`

- [x] 8. Update `pyqenc/phases/optimization.py` — OptimizationPhase
  - [x] 8.1 Constructor stays `(config, phases, *, collector)` — no context parameter
  - [x] 8.2 Replace all `self._config.quality_targets` → `self._job.result.config.encoding.resolved_targets`
  - [x] 8.3 Replace all `self._config.strategies` → `self._job.result.config.encoding.resolved_strategies`
  - [x] 8.4 Replace `self._config.metrics_sampling` → `self._job.result.config.encoding.metrics_sampling`
  - [x] 8.5 Replace `self._config.strategy_selection_tolerance` → `self._job.result.config.encoding.strategy_selection_tolerance`
  - [x] 8.6 Replace `self._config.optimize` → `self._job.result.config.encoding.optimize`
  - [x] 8.7 Replace `self._config.source_video`/`work_dir`/`no_metrics` → `self._job.result.source`/`work_dir`/`no_metrics`

- [x] 9. Update `pyqenc/phases/encoding.py` — EncodingPhase
  - [x] 9.1 Constructor stays `(config, phases, *, collector)` — no context parameter
  - [x] 9.2 Replace all `self._config.quality_targets` → `self._job.result.config.encoding.resolved_targets`
  - [x] 9.3 Replace all `self._config.strategies` → `self._job.result.config.encoding.resolved_strategies`
  - [x] 9.4 Replace `self._config.metrics_sampling` → `self._job.result.config.encoding.metrics_sampling`
  - [x] 9.5 Replace `self._config.max_parallel` → `self._job.result.config.encoding.max_parallel`
  - [x] 9.6 Replace `self._config.visual_hash` → `self._job.result.config.encoding.visual_hash`
  - [x] 9.7 Replace `self._config.source_video`/`work_dir`/`cleanup`/`no_metrics` → `self._job.result.source`/`work_dir`/`cleanup`/`no_metrics`

- [x] 10. Update `pyqenc/phases/audio.py` — AudioPhase
  - [x] 10.1 Constructor stays `(config, phases, *, collector)` — no context parameter
  - [x] 10.2 Replace `self._config.audio_convert` → `self._job.result.config.audio.convert_filter`
  - [x] 10.3 Replace `self._config.audio_codec` → `self._job.result.config.audio.audio_codec`
  - [x] 10.4 Replace `self._config.audio_base_bitrate` → `self._job.result.config.audio.audio_base_bitrate`
  - [x] 10.5 Replace audio output profiles access → `self._job.result.config.audio.profiles`
  - [x] 10.6 Replace `self._config.source_video`/`work_dir`/`cleanup` → `self._job.result.source`/`work_dir`/`cleanup`

- [x] 11. Update `pyqenc/phases/merge.py` — MergePhase
  - [x] 11.1 Constructor stays `(config, phases, *, collector)` — no context parameter
  - [x] 11.2 Replace `self._config.quality_targets` → `self._job.result.config.encoding.resolved_targets`
  - [x] 11.3 Replace `self._config.metrics_sampling` → `self._job.result.config.encoding.metrics_sampling`
  - [x] 11.4 Replace `self._config.source_video`/`work_dir`/`cleanup`/`no_metrics` → `self._job.result.source`/`work_dir`/`cleanup`/`no_metrics`

- [x] 12. Update `pyqenc/phases/measure.py` and `pyqenc/phases/recovery.py`
  - [x] 12.1 Audit `measure.py` for any `PipelineConfig` references; replace with `AppConfig` equivalents (measure phase uses config independently — it gets `AppConfig` directly since it doesn't use `JobPhase`)
  - [x] 12.2 Audit `recovery.py` for any `PipelineConfig` references; replace with `AppConfig`/`job_result` equivalents

- [x] 13. Update `pyqenc/orchestrator.py`
  - [x] 13.1 Update `PipelineOrchestrator` to receive the already-built registry; remove direct `config`/`context` constructor args if it currently holds them
  - [x] 13.2 Replace any remaining `self._config.*` references with reads from `job_phase.result.*`

- [x] 14. Update `pyqenc/cli.py` — assemble config and pass volatile kwargs
  - [x] 14.1 Add `load_app_config` import from `pyqenc.app_config`; remove `RunContext`/`ConfigManager` imports
  - [x] 14.2 Remove `PipelineConfig` import and all `PipelineConfig(...)` construction blocks; remove `ConfigManager()` instantiation calls
  - [x] 14.3 In `_cmd_auto`: call `load_app_config()`, apply CLI overrides via direct attribute assignment, call `_build_registry(config, source=..., work_dir=..., force=..., cleanup=..., no_metrics=..., collector=...)`
  - [x] 14.4 In `_cmd_extract`: same pattern
  - [x] 14.5 In `_cmd_chunk`: same pattern — include `chunking.mode`, `chunking.scene_threshold`, `chunking.min_scene_length` overrides
  - [x] 14.6 In `_cmd_encode`, `_cmd_audio`, `_cmd_merge`: same pattern
  - [x] 14.7 In `_cmd_measure`: use `load_app_config()` for `metrics_sampling` default (replace `ConfigManager().get_metrics_sampling()`)
  - [x] 14.8 Update `_cmd_config` to call `load_app_config()` and inspect its source paths (no more `find_config_source`)

- [x] 15. Update `pyqenc/api.py` — update public API functions
  - [x] 15.1 Remove `_minimal_config()` helper; replace with `load_app_config()` + direct overrides
  - [x] 15.2 Update all public API function signatures to pass volatile params as plain kwargs to `_build_registry`
  - [x] 15.3 Remove `PipelineConfig` import

- [x] 16. Delete legacy types — only after all references are migrated
  - [x] 16.1 Delete `ConfigManager` class from `config.py`
  - [x] 16.2 Delete `find_config_source()` from `config.py`
  - [x] 16.3 Delete `AudioConversionProfile`, `AudioOutputConfig`, `EncodingProfile` dataclasses from `config.py`; delete `config.py` entirely if now empty
  - [x] 16.4 Delete `PipelineConfig` from `models.py`
  - [x] 16.5 Run `uv run python -m pytest` and `uv run ruff check pyqenc/` to confirm zero references to deleted types remain

- [x] 17. End-to-end smoke test
  - [x] 17.1 Run `uv run pyqenc auto D:\_encoding\orig.mkv --work-dir D:\_encoding` (dry-run, no `-y`) and confirm it exits 0 with coherent dry-run plan log
  - [x] 17.2 Confirm `uv run pyqenc config .` still prints the correct dry-run output

- [x] 18. Update spec cross-references and mark completed
  - [x] 18.1 Review this spec against other existing specs in `.kiro/specs/`; add a summary section noting what was superseded or changed
  - [x] 18.2 Set `Completed:` date in design.md, requirements.md, and tasks.md
