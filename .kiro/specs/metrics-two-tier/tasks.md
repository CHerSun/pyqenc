# Implementation Plan: Two-Tier Metrics System

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-15

## Overview

Replace the flat `TimeKey`-based metrics model with a two-tier `dict[str, float]`
store keyed by dotted strings. Top-level keys (no dot) capture wall-clock elapsed
time; dotted keys capture per-process run time grouped by prefix. Migrate all call
sites, update Pydantic models and YAML serialisation, sanitize strategy name dots at
construction time, and update all property-based and integration tests.

## Tasks

- [x] 1. Add `DOTTED_KEY_SEPARATOR` constant to `pyqenc/constants.py`
  - Add `DOTTED_KEY_SEPARATOR: str = "."` with a docstring explaining its role as the
    metric key separator (distinct from file extension dots and other uses of `"."`)
  - Place it alongside the other separator constants (`TIME_SEPARATOR_SAFE`,
    `TIME_SEPARATOR_MS`, etc.)
  - _Requirements: 1.1, 1.4_

- [x] 2. Replace `TimeKey` with `MetricKey` in `pyqenc/metrics.py`
  - [x] 2.1 Define `MetricKey` StrEnum with 8 flat top-level members
    - `JOB`, `EXTRACTION`, `CHUNKING`, `AUDIO`, `ENCODING`, `OPTIMIZATION`, `MERGE`,
      `RECOVERY` — all values are plain strings with no dot separator
    - Remove `TimeKey` entirely (no deprecation alias — clean break per coding standards)
    - Update `__all__` to export `MetricKey` instead of `TimeKey`
    - _Requirements: 6.1, 6.2_
  - [x] 2.2 Add `MetricsStore` type alias and key helper functions
    - `MetricsStore = dict[str, float]`
    - `_is_top_level(key: str) -> bool` — returns `True` when `DOTTED_KEY_SEPARATOR not in key`
    - `_last_dot_prefix(key: str) -> str` — returns `key.rsplit(DOTTED_KEY_SEPARATOR, 1)[0]`
    - `_build_key(key: MetricKey, *parts: str) -> str` — joins with `DOTTED_KEY_SEPARATOR`
    - Import `DOTTED_KEY_SEPARATOR` from `pyqenc.constants`
    - _Requirements: 1.4, 2.1, 3.1_
  - [x] 2.3 Write property test for `_last_dot_prefix` (Property 1)
    - **Property 1: Prefix extraction is last-dot**
    - Generate random dotted key strings (1–3 dots, random segments); verify
      `_last_dot_prefix(key) == key.rsplit(".", 1)[0]`
    - Tag: `# Feature: metrics-two-tier, Property 1: Prefix extraction is last-dot`
    - **Validates: Requirements 1.4**

- [x] 3. Update `MetricsCollector` Protocol and `NoOpMetricsCollector`
  - Update `time(self, key: MetricKey, *parts: str)` signature in the Protocol
  - Update `step(self, key: MetricKey, *parts: str, convergence_update: ConvergenceUpdate | None = None)` signature
  - Update `NoOpMetricsCollector.time` and `NoOpMetricsCollector.step` to accept the new
    variadic signature — both remain no-ops
  - _Requirements: 6.4, 6.5, 6.6_

- [ ] 4. Replace Pydantic models with two-tier structure in `pyqenc/metrics.py`
  - Remove `TimeEntry` and the old `TimeDistribution` / `PipelineMetrics`
  - Add `TopLevelEntry(key, seconds, duration, percent)`
  - Add `DottedEntry(key, seconds, duration, percent)`
  - Add `DottedGroup(prefix_seconds, prefix_duration, breakdown: list[DottedEntry])`
  - Add updated `TimeDistribution(total_seconds, total_duration, top_level, dotted)`
  - Add updated `PipelineMetrics(time_distribution, convergence)` — no `parallelism` field
  - Update `__all__` to export the new models; remove `TimeEntry`
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ] 5. Update `YamlMetricsCollector` internals
  - [ ] 5.1 Replace `_time_accum` with sparse `_store: MetricsStore`
    - Change `__init__` to initialise `_store: MetricsStore = {}` (no pre-seeding)
    - Update `_active_timers` type to `list[tuple[str, float]]` (stores resolved key strings)
    - _Requirements: 2.2, 3.4_
  - [ ] 5.2 Update `_TimingContext` to use `_build_key` and `_store`
    - Accept `key: MetricKey, *parts: str`; resolve to storage key via `_build_key` in `__init__`
    - Accumulate into `_store[resolved_key]` on exit (create entry on first use)
    - _Requirements: 2.1, 3.1_
  - [ ] 5.3 Update `step()` to accept `MetricKey, *parts: str`
    - Resolve key via `_build_key`; convergence logic unchanged
    - _Requirements: 6.6_
  - [ ] 5.4 Implement two-tier `_build_metrics()`
    - Add `_compute_top_level_entries(store)` — filters top-level keys, computes grand total,
      formats percentages, sorts descending, omits zeros
    - Add `_compute_dotted_groups(store)` — groups dotted keys by `_last_dot_prefix`,
      computes prefix totals, formats percentages, sorts breakdowns descending, omits zeros
    - Assemble `TimeDistribution` with `top_level` and `dotted` from the two helpers
    - Handle zero grand total (all percentages `"0.0%"`) and zero prefix total per group
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, 5.4_
  - [ ] 5.5 Write property test for time accumulation (Property 2)
    - **Property 2: Time accumulation is additive**
    - Generate a random key (top-level or dotted) and a list of non-negative floats;
      inject into `_store`; verify `_store[key] == sum(durations)` within float tolerance
    - Tag: `# Feature: metrics-two-tier, Property 2: Time accumulation is additive`
    - **Validates: Requirements 2.2, 3.4**
  - [ ] 5.6 Write property test for top-level percentages (Property 3)
    - **Property 3: Top-level percentages sum to 100%**
    - Generate random top-level value dicts; inject into `_store`; call `flush()`; parse YAML;
      sum `float(e["percent"].rstrip("%"))` for all `top_level` entries; assert `abs(total - 100.0) < 0.15`
    - Tag: `# Feature: metrics-two-tier, Property 3: Top-level percentages sum to 100%`
    - **Validates: Requirements 4.1, 4.4**
  - [ ] 5.7 Write property test for dotted percentages (Property 4)
    - **Property 4: Dotted percentages sum to 100% per prefix**
    - Generate random groups of sibling dotted keys (same prefix, random suffixes, at least one
      non-zero); inject into `_store`; call `flush()`; parse YAML; for each prefix group sum
      percentages; assert `abs(total - 100.0) < 0.15`
    - Tag: `# Feature: metrics-two-tier, Property 4: Dotted percentages sum to 100% per prefix`
    - **Validates: Requirements 4.2, 4.5**
  - [ ] 5.8 Write property test for top-level sort order (Property 8)
    - **Property 8: Top-level list sorted descending with no zeros**
    - Generate random top-level value dicts; inject into `_store`; call `flush()`; parse YAML
      `top_level` list; assert `seconds` values are non-increasing and all `> 0`
    - Tag: `# Feature: metrics-two-tier, Property 8: Top-level list sorted descending with no zeros`
    - **Validates: Requirements 5.2**
  - [ ] 5.9 Write property test for dotted breakdown sort order (Property 9)
    - **Property 9: Dotted breakdown sorted descending with no zeros**
    - Generate random dotted value dicts (multiple prefix groups); inject into `_store`; call
      `flush()`; parse YAML `dotted` section; for each prefix group assert `breakdown` `seconds`
      values are non-increasing and all `> 0`
    - Tag: `# Feature: metrics-two-tier, Property 9: Dotted breakdown sorted descending with no zeros`
    - **Validates: Requirements 5.3**

- [ ] 6. Update `_try_resume()` for two-tier YAML structure
  - Read `top_level` entries and restore each key's float seconds into `_store`
  - Read all `dotted` prefix groups and restore each `breakdown` entry's key into `_store`
  - Skip unknown keys with `logger.debug` (do not raise)
  - Restore convergence accumulators unchanged (existing Welford resume logic)
  - _Requirements: 5.6_
  - [ ] 6.1 Write property test for resume (Property 6)
    - **Property 6: Resume restores accumulated store**
    - Generate a random `MetricsStore` (mix of top-level and dotted keys, non-zero values);
      inject into a `YamlMetricsCollector`; call `flush()`; construct a second collector with
      the same `work_dir`; for each key assert `abs(resumed._store[key] - int(round(original))) <= 1`
    - Tag: `# Feature: metrics-two-tier, Property 6: Resume restores accumulated store`
    - **Validates: Requirements 5.6**

- [ ] 7. Checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Sanitize ASCII dots in `Strategy` and `BaseStrategy`
  - [ ] 8.1 Add `field_validator` to `Strategy` in `pyqenc/models.py`
    - Add `@field_validator("preset", "profile", mode="before")` that replaces `"."` with
      `TIME_SEPARATOR_MS` (import from `pyqenc.constants`)
    - Import `TIME_SEPARATOR_MS` at the top of `models.py` (add to existing constants import)
    - _Requirements: 8.1, 8.3, 8.4_
  - [ ] 8.2 Sanitize dots in `BaseStrategy.__init__` in `pyqenc/phases/audio.py`
    - Replace `self.name = name` with `self.name = name.replace(".", TIME_SEPARATOR_MS)`
    - Replace `self.strategy_short = strategy_short` with the same replacement
    - Import `TIME_SEPARATOR_MS` from `pyqenc.constants` at the top of `audio.py`
    - _Requirements: 8.2, 8.3, 8.4, 8.5_
  - [ ] 8.3 Write property test for strategy dot sanitization (Property 7)
    - **Property 7: Strategy dot sanitization produces valid metric keys**
    - Generate random strings (possibly containing ASCII dots) as `preset`/`profile`; construct
      `Strategy`; assert `strategy.name` contains no ASCII dot (U+002E); pass `strategy.name`
      as suffix to `_build_key(MetricKey.ENCODING, strategy.name)`; apply `_last_dot_prefix`;
      assert prefix equals `MetricKey.ENCODING`
    - Repeat analogously for `BaseStrategy` with `strategy_short`
    - Tag: `# Feature: metrics-two-tier, Property 7: Strategy dot sanitization produces valid metric keys`
    - **Validates: Requirements 8.1, 8.2, 8.5**

- [ ] 9. Migrate phase call sites from `TimeKey` to `MetricKey`
  - [ ] 9.1 Migrate `pyqenc/phases/job.py`
    - Replace `TimeKey.JOB_PROBE` → `MetricKey.JOB` (top-level, wall-clock)
    - Replace `TimeKey.JOB_CROP_DETECT` → `MetricKey.JOB` (top-level, accumulates)
    - Add dotted calls: `collector.time(MetricKey.JOB, "probe")` and
      `collector.time(MetricKey.JOB, "crop_detect")` wrapping the respective sub-operations
    - Update import: `from pyqenc.metrics import MetricKey` (remove `TimeKey`)
    - _Requirements: 2.5, 3.7, 6.4_
  - [ ] 9.2 Migrate `pyqenc/phases/extraction.py`
    - Replace `TimeKey.EXTRACTION` → `MetricKey.EXTRACTION` (top-level only, no dotted keys)
    - Replace `TimeKey.RECOVERY` → `MetricKey.RECOVERY`
    - Update import
    - _Requirements: 2.5, 6.4_
  - [ ] 9.3 Migrate `pyqenc/phases/chunking.py`
    - Replace `TimeKey.CHUNKING_SCENE_DETECT` → `MetricKey.CHUNKING` (top-level) + add
      `collector.time(MetricKey.CHUNKING, "scene_detect")` wrapping scene detection
    - Replace `TimeKey.CHUNKING_SPLIT` → `MetricKey.CHUNKING` (top-level) + add
      `collector.time(MetricKey.CHUNKING, "split")` wrapping chunk splitting
    - Replace `TimeKey.CHUNKING_SPLIT` in `step()` calls → `MetricKey.CHUNKING, "split"`
    - Replace `TimeKey.RECOVERY` → `MetricKey.RECOVERY`
    - Update import (including the `_TimeKey` alias import)
    - _Requirements: 2.5, 3.7, 6.4_
  - [ ] 9.4 Migrate `pyqenc/phases/audio.py`
    - Replace `TimeKey.AUDIO` → `MetricKey.AUDIO` (top-level, wall-clock for the whole phase)
    - Add dotted call: `collector.time(MetricKey.AUDIO, strategy.strategy_short)` wrapping
      per-strategy audio processing
    - Replace `TimeKey.RECOVERY` → `MetricKey.RECOVERY`
    - Update import
    - _Requirements: 2.5, 3.7, 6.4_
  - [ ] 9.5 Migrate `pyqenc/phases/encoding.py`
    - Replace `TimeKey.ENCODING_MAIN` → `MetricKey.ENCODING` (top-level, wall-clock)
    - Add dotted call: `collector.time(MetricKey.ENCODING, strategy.name)` wrapping
      per-strategy encoding work inside the encoding loop
    - Replace `TimeKey.ENCODING_MAIN` in `step()` calls → `MetricKey.ENCODING`
    - Replace `TimeKey.RECOVERY` → `MetricKey.RECOVERY`
    - Update import
    - _Requirements: 2.5, 3.7, 6.4_
  - [ ] 9.6 Migrate `pyqenc/phases/optimization.py`
    - Replace `TimeKey.ENCODING_OPTIMIZATION` → `MetricKey.OPTIMIZATION` (top-level)
    - Add dotted call: `collector.time(MetricKey.OPTIMIZATION, strategy.name)` wrapping
      per-strategy test encodes
    - Replace `TimeKey.RECOVERY` → `MetricKey.RECOVERY`
    - Update import
    - _Requirements: 2.5, 3.7, 6.4_
  - [ ] 9.7 Migrate `pyqenc/phases/merge.py`
    - Replace `TimeKey.MERGE_CONCAT` → `MetricKey.MERGE` (top-level) + add
      `collector.time(MetricKey.MERGE, "concat")` wrapping concat operation
    - Replace `TimeKey.MERGE_QUALITY_MEASURE` → `MetricKey.MERGE` (top-level) + add
      `collector.time(MetricKey.MERGE, "quality_measure")` wrapping quality measurement
    - Replace `TimeKey.RECOVERY` → `MetricKey.RECOVERY`
    - Update import
    - _Requirements: 2.5, 3.7, 6.4_

- [ ] 10. Update `test_metrics_properties.py` for two-tier model
  - Replace all `TimeKey` imports and usages with `MetricKey`
  - Replace `TimeEntry` / old `TimeDistribution` / old `PipelineMetrics` strategies with
    new `TopLevelEntry`, `DottedEntry`, `DottedGroup`, `TimeDistribution`, `PipelineMetrics`
  - Update `_st_pipeline_metrics()` composite strategy to generate `top_level` and `dotted`
    sections using the new model structure
  - Update `_serialize` / `_deserialize` helpers to use the new YAML structure
  - Update Property 5 (YAML round-trip) to assert equality of `top_level` entries and
    `dotted` groups (replaces old `breakdown` assertions)
  - Add new property tests for Properties 1–4 and 6–9 as sub-tasks under tasks 2, 5, 6, 8
    (already listed there); remove the old Properties 1–3 tests that used `TimeKey`
  - _Requirements: 5.1, 5.6_
  - [ ] 10.1 Write property test for YAML round-trip (Property 5)
    - **Property 5: YAML serialisation round-trip preserves all values**
    - Generate random `PipelineMetrics` instances with `top_level` and `dotted` sections;
      serialise to YAML string; deserialise; assert structural equality of all fields
    - Tag: `# Feature: metrics-two-tier, Property 5: YAML serialisation round-trip preserves all values`
    - **Validates: Requirements 5.1, 5.6**

- [ ] 11. Update `test_metrics_integration.py` for `MetricKey` migration
  - Replace `from pyqenc.metrics import ... TimeKey` with `MetricKey`
  - Update all `TimeKey.X` references per the conversion table in Requirement 6.2:
    - `TimeKey.JOB_PROBE` → `MetricKey.JOB`
    - `TimeKey.JOB_CROP_DETECT` → `MetricKey.JOB`
    - `TimeKey.EXTRACTION` → `MetricKey.EXTRACTION`
    - `TimeKey.CHUNKING_SCENE_DETECT` → `MetricKey.CHUNKING`
    - `TimeKey.CHUNKING_SPLIT` → `MetricKey.CHUNKING`
    - `TimeKey.AUDIO` → `MetricKey.AUDIO`
    - `TimeKey.ENCODING_MAIN` → `MetricKey.ENCODING`
    - `TimeKey.ENCODING_OPTIMIZATION` → `MetricKey.OPTIMIZATION`
    - `TimeKey.MERGE_CONCAT` → `MetricKey.MERGE`
    - `TimeKey.MERGE_QUALITY_MEASURE` → `MetricKey.MERGE`
    - `TimeKey.RECOVERY` → `MetricKey.RECOVERY`
  - Add new smoke tests:
    - `MetricKey` has exactly 8 members with correct string values
    - `time()` accepts `MetricKey` with zero or more suffix parts
    - `step()` accepts `MetricKey` with zero or more suffix parts
    - `NoOpMetricsCollector` accepts both without error
    - `Strategy` construction with dots in `preset`/`profile` produces `name` with no ASCII dots
    - `BaseStrategy` construction with dots in `strategy_short` produces no ASCII dots
    - Top-level and dotted keys coexist in the same store
    - YAML `dotted` section is absent when no dotted keys have non-zero values
  - Update assertions that check `call.args[0]` to also verify suffix parts where applicable
  - _Requirements: 6.1, 6.2, 6.4, 6.5, 6.6_

- [ ] 12. Final checkpoint — ensure all tests pass
  - Run `uv run python -m pytest` to confirm no regressions across the full test suite.
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Review spec against other specs and add cross-spec summary
  - Review `metrics-two-tier` against related specs (`pipeline-metrics-report`,
    `all-in-one-metrics`, `unified-metrics-visualization`) using file timestamps and
    Created/Completed dates to establish timeline
  - Add a cross-spec summary section at the top of `design.md` and `requirements.md`
    noting what was superseded or changed between specs
  - Update `requirements.md` and `design.md` with `- Completed: ` date
  - _Requirements: (spec hygiene)_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties (Properties 1–9 from design)
- Unit/integration tests validate specific examples and edge cases
- The `TimeKey` enum is removed entirely — no legacy alias, no backwards compatibility shim
