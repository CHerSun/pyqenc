# Implementation Plan: CRF Search Refactor

<!-- markdownlint-disable MD024 -->

- Created: 2026-04-06
- Completed:

## Overview

Refactor the CRF search implementation in `pyqenc/quality.py` and `pyqenc/phases/encoding.py` to introduce a unified `QualitySearchProtocol`, replace the legacy `CRFHistory`/`adjust_crf()` pair with encapsulated `QualitySearch` and `QualitySearchV2` classes, rename attempt filenames from `.crf<n>` to `.q<n>`, and replace global padding/delta constants with per-instance computed properties.

## Tasks

- [ ] 1. Add `MetricInfo.acceptance_delta` and remove `CRF_METRIC_POSITIVE_DELTA`
  - Add `acceptance_delta: float` field to the `MetricInfo` dataclass in `pyqenc/quality.py`
  - Set values: VMAF=`0.1`, SSIM=`0.02`, PSNR=`0.5`, VIF=`0.005`
  - Remove the `CRF_METRIC_POSITIVE_DELTA` constant from `pyqenc/constants.py`
  - Remove the import of `CRF_METRIC_POSITIVE_DELTA` from `quality.py` and any other callers
  - _Requirements: 10.6_

- [ ] 2. Add `CodecConfig.quality_log_padding` and remove `PADDING_QUALITY_NUMBER`
  - Add `quality_log_padding: int` computed property to `CodecConfig` in `pyqenc/config.py`
  - Implementation: `len(str(Decimal(str(max(abs(self.quality_better), abs(self.quality_worse)))).quantize(self.quality_granularity)))`
  - Remove the `PADDING_QUALITY_NUMBER` constant from `pyqenc/constants.py`
  - Remove the import of `PADDING_QUALITY_NUMBER` from `encoding.py` and any other callers
  - Replace all `PADDING_QUALITY_NUMBER` usages in `encoding.py` with `strategy.codec.quality_log_padding`
  - _Requirements: 10.7_

  - [ ] 2.1 Write unit tests for `CodecConfig.quality_log_padding`
    - Test CRF range `[0, 51]` gran `0.5` → `4`
    - Test VBR range `[0, 100]` gran `0.1` → `5`
    - Test QP range `[0, 63]` gran `1` → `2`
    - _Requirements: 10.7_

- [ ] 3. Update attempt filename constants in `pyqenc/constants.py`
  - Change `ENCODED_ATTEMPT_GLOB_PATTERN` from `"*.crf*.mkv"` to `"*.q*.mkv"`
  - Change `ENCODED_ATTEMPT_NAME_PATTERN` regex: `\.crf(?P<crf>[\d.]+)\.` → `\.q(?P<quality>[\d.]+)\.`
  - Update the docstring for `ENCODED_ATTEMPT_NAME_PATTERN` to reflect the new group name `quality`
  - _Requirements: 10.1, 10.2_

- [ ] 4. Update `ChunkEncoder` filename construction and regex callers in `pyqenc/phases/encoding.py`
  - Update `_get_attempt_path()` to produce `.q{value}` filenames instead of `.crf{value}`
  - Update `_check_existing_encoding()` to use the new glob and regex from task 3
  - Replace all `m.group("crf")` calls with `m.group("quality")` throughout `encoding.py`
  - _Requirements: 10.3, 10.4, 10.5_

- [ ] 5. Implement `_score_attempt()` in `pyqenc/quality.py`
  - Add `_score_attempt(metrics: dict[str, float], quality_targets: list[QualityTarget]) -> float`
  - Raise `ValueError` if any target key is absent from `metrics`
  - Return `0.0` (early acceptance) when all targets pass and every surplus ≤ `metric_info.acceptance_delta`
  - Return positive sum of `surplus / comparison_range` for all targets when all pass but some surplus > `acceptance_delta`
  - Return negative sum of `deficit / comparison_range` for failing targets only when any target fails
  - Add `DEBUG`-level per-target normalized contribution logging
  - _Requirements: 1.3, 3.x_

  - [ ] 5.1 Write unit tests for `_score_attempt` in `tests/unit/test_quality.py`
    - Early acceptance returns `0.0`
    - All-pass with large surplus returns positive float
    - Any-fail returns negative float
    - Missing key raises `ValueError`
    - _Requirements: 1.3_

  - [ ] 5.2 Write property test P5: `_score_attempt` sign contract in `tests/test_crf_search_properties.py`
    - **Property 5: _score_attempt sign contract**
    - **Validates: Requirements 1.3, 3.x**
    - Tag: `# Feature: crf-search-refactor, Property 5: _score_attempt sign contract`
    - Use `st.lists` of `(metric_value, target_value)` pairs; min 200 iterations
    - _Requirements: 9.5_

- [ ] 6. Implement `QualitySearchProtocol` in `pyqenc/quality.py`
  - Define `QualitySearchProtocol` as a `typing.Protocol` with properties: `attempts: int`, `best_quality: Decimal | None`, `best_metrics: dict[str, float] | None`, `best_targets_met: bool`
  - Declare `record(self, quality: Decimal, quality_results: dict[str, float]) -> Decimal | None`
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

- [ ] 7. Implement `QualitySearch` class in `pyqenc/quality.py`
  - Add `QualitySearch` implementing `QualitySearchProtocol`
  - Constructor: `QualitySearch(quality_better, quality_worse, quality_targets, granularity, quality_max_step=None)`
  - Raise `ValueError` at construction if `quality_better == quality_worse` or `granularity <= 0`
  - Internal state: `_better_q`, `_worse_q`, `_better_metrics`, `_worse_metrics`, `_attempts`, `_exhausted`
  - `record()` runs the same proportional interpolation logic as the old `adjust_crf()`, using `_score_attempt()` for scoring; direction-agnostic via `quality_better`/`quality_worse`
  - `record()` returns `None` immediately (without mutating state) when `_exhausted` is `True`
  - Expose `attempts`, `best_quality`, `best_metrics`, `best_targets_met` as properties satisfying the protocol invariants
  - Remove `CRFHistory` dataclass, `adjust_crf()` function, and `_score_failing_attempt()` / `_score_failing_attempt_by_crf()` from `quality.py`
  - _Requirements: 2.1, 2.2, 2.3, 2.6, 2.7, 4.1, 4.3, 5.1–5.5_

  - [ ] 7.1 Write unit tests for `QualitySearch` in `tests/unit/test_quality.py`
    - Initial state: `best_quality is None`, `best_metrics is None`, `best_targets_met is False`, `attempts == 0`
    - Pass attempt narrows better bound; `best_quality` and `best_targets_met` update correctly
    - Fail attempt narrows worse bound; `best_quality` tracks best-fail
    - Exhaustion returns `None`; subsequent calls also return `None`
    - `attempts` increments on every `record()` call
    - _Requirements: 2.1–2.3, 2.6, 2.7, 5.1–5.5_

  - [ ] 7.2 Write property test P1: `QualitySearch` convergence in `tests/test_crf_search_properties.py`
    - **Property 1: QualitySearch convergence**
    - **Validates: Requirements 4.1**
    - Tag: `# Feature: crf-search-refactor, Property 1: QualitySearch convergence`
    - Use `st.decimals` for range/granularity, `st.lists` of pass/fail booleans; min 200 iterations
    - _Requirements: 9.1_

- [ ] 8. Implement `QualitySearchV2` class in `pyqenc/quality.py`
  - Add `QualitySearchV2` implementing `QualitySearchProtocol`
  - Constructor: `QualitySearchV2(quality_better, quality_worse, quality_targets, granularity, quality_max_step=None)`
  - Raise `ValueError` at construction if `quality_better == quality_worse` or `granularity <= 0`
  - Initial sentinel state: `_pass_q = quality_better`, `_pass_metrics = None`, `_best_q = quality_worse`, `_best_metrics = None`, `_best_score = -inf`, `_fail_q = quality_worse`, `_fail_metrics = None`, `_attempts = 0`, `_exhausted = False`
  - Implement all Phase 1 (all-failing and all-passing sub-cases) and Phase 2 (3-point mode) state transitions exactly as specified in the design's QualitySearchV2 State Transitions section
  - Early acceptance (`score == 0.0`): update best, set `_exhausted = True`, return `None`
  - Exhaustion (both ranges ≤ granularity): set `_exhausted = True`, return `None`
  - `record()` returns `None` immediately when `_exhausted` is `True`
  - Expose `attempts`, `best_quality`, `best_metrics`, `best_targets_met` as properties satisfying the protocol invariants
  - _Requirements: 3.1–3.12, 4.2, 4.3, 5.1–5.5_

  - [ ] 8.1 Write unit tests for `QualitySearchV2` state transitions in `tests/unit/test_quality.py`
    - One concrete test per Phase 1 sub-case (all-failing new-best, all-failing sweet-spot-passed, all-passing new-best, all-passing sweet-spot-passed)
    - One concrete test per Phase 2 case (Range B new-best promote, Range A new-best demote, Range B tighten, Range A tighten)
    - Initial sentinel state test
    - Early acceptance (`score == 0.0`) sets `_exhausted`, returns `None`
    - Exhaustion (both ranges ≤ granularity) returns `None`; subsequent calls also return `None`
    - _Requirements: 3.3–3.10, 4.3_

  - [ ] 8.2 Write property test P2: `QualitySearchV2` convergence in `tests/test_crf_search_properties.py`
    - **Property 2: QualitySearchV2 convergence**
    - **Validates: Requirements 4.2**
    - Tag: `# Feature: crf-search-refactor, Property 2: QualitySearchV2 convergence`
    - Use `st.decimals` for range/granularity, `st.lists` of pass/fail booleans; min 200 iterations
    - _Requirements: 9.2_

  - [ ] 8.3 Write property test P3: finality after exhaustion in `tests/test_crf_search_properties.py`
    - **Property 3: Finality after exhaustion**
    - **Validates: Requirements 4.3**
    - Tag: `# Feature: crf-search-refactor, Property 3: Finality after exhaustion`
    - Parametrize over both `QualitySearch` and `QualitySearchV2`; min 200 iterations
    - _Requirements: 9.x_

  - [ ] 8.4 Write property test P4: protocol state invariants in `tests/test_crf_search_properties.py`
    - **Property 4: Protocol state invariants**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5**
    - Tag: `# Feature: crf-search-refactor, Property 4: Protocol state invariants`
    - Parametrize over both implementations; use `st.lists` of `(quality, quality_results)` pairs; min 200 iterations
    - _Requirements: 9.3_

- [ ] 9. Checkpoint — ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. Refactor `ChunkEncoder.encode_chunk()` in `pyqenc/phases/encoding.py`
  - Replace `CRFHistory` instantiation and `adjust_crf()` calls with `QualitySearchV2` as the default search object
  - Remove local variables `best_failing_crf`, `best_failing_metrics`, `best_failing_attempt`
  - Use `search.best_quality`, `search.best_metrics`, `search.best_targets_met` for finalization decisions
  - Rename loop variable `current_crf` → `current_q` and `next_crf` → `next_q` throughout `encode_chunk()`
  - Add `_any_real_work: bool = False` tracking; set to `True` on any actual encode or metric measurement (not on pure cache hits)
  - When `search.record()` returns `None` and `not _any_real_work`, advance progress bar with `AdvanceState.SKIPPED` and set `ChunkEncodingResult.reused = True`
  - Remove `_load_history_from_sidecars()` method from `ChunkEncoder`
  - _Requirements: 6.1–6.5, 7.1–7.6, 8.1–8.3_

  - [ ] 10.1 Write unit tests for `encode_chunk` integration in `tests/unit/test_quality.py`
    - `encode_chunk` instantiates `QualitySearchV2` by default (verified via mock/patch)
    - `QualitySearch` is usable as a drop-in alternative
    - Fully-recovered chunk (all cache hits) advances progress bar as `SKIPPED`
    - _Requirements: 8.1–8.3, 7.5–7.6_

  - [ ] 10.2 Write integration tests in `tests/integration/test_encoding_quality.py`
    - `encode_chunk` with mocked quality evaluator converges correctly using `QualitySearchV2`
    - Artifact-check-per-step recovery replays cached attempts and reaches un-encoded value
    - Fully-recovered chunk (all cache hits) produces `SKIPPED` progress bar advance
    - _Requirements: 6.1–6.5, 7.1–7.6_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The design takes precedence over requirements.md on `CRFHistory`/`adjust_crf` — both are removed (not retained)
- `QualitySearchV2` state machine uses `_pass_metrics`/`_fail_metrics` to distinguish all-failing vs all-passing phase; follow the design's detailed state transitions, not the simplified requirements.md Req 3 description
- Property tests live in `tests/test_crf_search_properties.py` (new file); each property is a separate sub-task
- Each property test must run a minimum of 200 Hypothesis iterations and carry the tag `# Feature: crf-search-refactor, Property N: ...`
