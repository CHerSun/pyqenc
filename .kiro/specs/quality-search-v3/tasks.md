# Implementation Plan: QualitySearchV3

<!-- markdownlint-disable MD024 -->

- Created: 2026-07-14
- Completed:

## Overview

Implement `QualitySearchV3` in `pyqenc/quality.py` by:
1. Replacing `QualitySearchProtocol` with `QualitySearchBase(ABC)` — an abstract base class carrying the shared constructor, config fields, exhaustion flag, and all protected helpers.
2. Updating `QualitySearch` and `QualitySearchV2` to inherit from `QualitySearchBase`.
3. Adding `QualitySearchV3` as a third subclass that replaces V2's binary half-steps with linear extrapolation and adds a midpoint-probe safety net.
4. Extending the unit and property-based test suites to cover V3.

The encoding pipeline is **not** switched to V3 as part of this spec.

## Tasks

- [x] 1. Introduce `QualitySearchBase` and migrate `QualitySearch` / `QualitySearchV2`
  - [x] 1.1 Define `QualitySearchBase(ABC)` in `pyqenc/quality.py`
    - Replace `QualitySearchProtocol` with an abstract base class
    - Constructor accepts `quality_better`, `quality_worse`, `quality_targets`, `granularity`, `quality_max_step`; raises `ValueError` only if `granularity <= 0`; `quality_better == quality_worse` is valid
    - Declare abstract properties and method: `attempts`, `best_quality`, `best_metrics`, `best_targets_met`, `record()`
    - Add shared config fields: `_quality_targets`, `_granularity`, `_quality_max_step`, `_quality_better`, `_quality_worse`, `_exhausted`
    - _Requirements: 12.1_

  - [x] 1.2 Move `_score_attempt` and `_find_worst_target` into `QualitySearchBase` as `_score()` and `_find_worst_target()`
    - Remove the module-level `_score_attempt(metrics, targets)` and `_find_worst_target(metrics, targets)` functions
    - Add `_score(self, metrics)` and `_find_worst_target(self, metrics)` as protected methods on the base class, reading `self._quality_targets` implicitly
    - _Requirements: 12.6_

  - [x] 1.3 Add `_next_or_exhaust`, `_finalize_q`, and `_compute_next_quality` to `QualitySearchBase`
    - `_next_or_exhaust(self, next_q, attempted)`: sets `self._exhausted = True` and returns `None` if `next_q` is `None` or already in `attempted`
    - `_finalize_q(self, raw_q, from_q, worse_point, better_point)`: applies max-step clamp → granularity snap → sentinel-aware range clamp in order; returns `None` when range is exhausted; does NOT set `self._exhausted`
    - `_compute_next_quality(self, new_point, worse_point, better_point)`: promoted from `QualitySearch._compute_next`; always calls `_compute_proportional_candidate` without range clamping; falls back to binary midpoint (0.5) when proportional candidate is `None`; calls `_finalize_q` for the final value
    - _Requirements: 12.1, 12.2_

  - [x] 1.4 Update `QualitySearch` to inherit from `QualitySearchBase`
    - Replace `QualitySearchProtocol` base with `QualitySearchBase`
    - Replace all inline `_score_attempt(metrics, self._quality_targets)` calls with `self._score(metrics)`
    - Replace all inline `_find_worst_target(metrics, self._quality_targets)` calls with `self._find_worst_target(metrics)`
    - Replace inline max-step/snap/clamp sequences with `self._finalize_q(...)`
    - Replace `QualitySearch._compute_next(self, ...)` delegation with `self._compute_next_quality(...)`
    - Remove the `ValueError` raised when `quality_better == quality_worse`
    - _Requirements: 12.3_

  - [x] 1.5 Update `QualitySearchV2` to inherit from `QualitySearchBase`
    - Same migration as 1.4: replace protocol base, inline calls, and remove `ValueError` on equal boundaries
    - _Requirements: 12.3_

  - [x] 1.6 Remove `normalize_metric` module-level function
    - Delete the dead `normalize_metric` function (superseded by `MetricType.info.normalize()`)
    - _Requirements: 12.7_

  - [x] 1.7 Update all `isinstance(x, QualitySearchProtocol)` checks to `isinstance(x, QualitySearchBase)`
    - Search production code and tests for `QualitySearchProtocol` references and update them
    - _Requirements: 12.5_

  - [x] 1.8 Update existing tests broken by the migration
    - Update tests that imported `_score_attempt` or `_find_worst_target` directly — replace with instantiating a search object and calling `search._score(metrics)`, or test through `record()`
    - Update tests that assert `ValueError` when `quality_better == quality_worse` — replace with assertions for single-point search behavior (first `record()` returns `None`)
    - _Requirements: 12.3, 12.4, 12.6_

- [x] 2. Checkpoint — existing tests pass after base class migration
  - Ensure all existing tests pass with no regressions. Ask the user if questions arise.

- [x] 3. Implement `QualitySearchV3`
  - [x] 3.1 Add `QualitySearchV3` class skeleton in `pyqenc/quality.py`
    - Inherit from `QualitySearchBase`; call `super().__init__()` in constructor
    - Declare V3-specific instance fields: `_attempted_points`, `_best_score_point`, `_midpoint_probe_flag`
    - Implement abstract properties: `attempts`, `best_quality`, `best_metrics`, `best_targets_met`
    - Export `QualitySearchV3` from `pyqenc.quality`
    - _Requirements: 1.1, 1.2, 1.5, 1.7, 12.9_

  - [x] 3.2 Implement `record()` — Phase 0: first attempt
    - When `_exhausted` is `True`, return `None` immediately without mutating state
    - On the first call: record the point in `_attempted_points`, update `_best_score_point`
    - Score == 0 (winner): set `_exhausted = True`, return `None`
    - Score > 0 (pass): return next quality = current + half-range toward `quality_worse`, clamped by `quality_max_step`, snapped to granularity
    - Score < 0 (fail): return next quality = current + half-range toward `quality_better`, clamped by `quality_max_step`, snapped to granularity
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 9.2, 9.3_

  - [x] 3.3 Implement `record()` — point selection (2+ attempts)
    - Sort `_attempted_points` by quality value
    - Identify `_best_score_point` (smallest `abs(score)`, pass precedence over fail)
    - Select best + immediate lower neighbour + immediate upper neighbour (up to 3 points)
    - Reset `_midpoint_probe_flag` to `False` whenever `_best_score_point` changes
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 5.5_

  - [x] 3.4 Implement `record()` — 2-point same-side fork (linear extrapolation)
    - When 2 points are same-side and direction is NOT exhausted: call `self._compute_next_quality(new_point, worse_point, better_point)` with `t` allowed outside `[0, 1]`; return `self._next_or_exhaust(result, _attempted_points)`
    - Outward clamp boundary: sentinel if no opposite-side tested point exists, else best-scoring opposite-side tested point
    - Fall back to midpoint of the two points when `_compute_proportional_candidate` returns `None`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 3.5 Implement `record()` — 2-point same-side fork (direction-exhausted cases)
    - Direction toward `quality_better` is exhausted when `quality_better` exists as a key in `_attempted_points`; same for `quality_worse`
    - When direction exhausted and `_midpoint_probe_flag` is `False`: probe midpoint between the 2 points via `self._finalize_q(mid, last_q, p1, p2)`, set `_midpoint_probe_flag = True`, return result
    - When direction exhausted and `_midpoint_probe_flag` is `True`: set `_exhausted = True`, return `None`
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [x] 3.6 Implement `record()` — 2-point different-sides fork (proportional interpolation)
    - When 2 points are on different sides: call `self._compute_next_quality(new_point, fail_point, pass_point)`; return `self._next_or_exhaust(result, _attempted_points)`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x] 3.7 Implement `record()` — 3-point fork
    - Spanning both sides: reduce to 2-point different-sides by selecting the straddling pair; when both pairs straddle, prefer the pair including the worse-quality neighbour
    - All same side: sweet-spot search — compute left/right sub-range sizes, select larger, probe midpoint via `self._finalize_q`; if sub-range ≤ 1 granularity, set `_exhausted = True` and return `None`
    - _Requirements: 7.1, 7.2, 7.3, 7.4_

- [x] 4. Checkpoint — `QualitySearchV3` basic smoke test
  - Ensure the class is importable and the constructor works. Ask the user if questions arise.

- [-] 5. Write unit tests for `QualitySearchV3` in `tests/unit/test_quality.py`
  - [ ] 5.1 Add `TestQualitySearchV3` class with initial-state and constructor tests
    - `test_initial_state`: verify `best_quality is None`, `best_metrics is None`, `best_targets_met is False`, `attempts == 0`
    - `test_raises_if_granularity_zero`: verify `ValueError` on `granularity <= 0`
    - `test_raises_if_better_equals_worse`: update to assert single-point search behavior (first `record()` returns `None`) instead of `ValueError`
    - `test_protocol_compliance`: verify `isinstance(s, QualitySearchBase)`
    - _Requirements: 1.3, 1.4, 1.5, 13.1_

  - [ ] 5.2 Write unit tests for first-attempt behavior
    - `test_first_winner_returns_none`: score == 0 → `record()` returns `None`, `best_quality` set
    - `test_first_pass_steps_toward_worse`: score > 0 → returned value is worse than input
    - `test_first_fail_steps_toward_better`: score < 0 → returned value is better than input
    - _Requirements: 2.1, 2.2, 2.3, 13.2_

  - [ ] 5.3 Write unit tests for 2-point same-side extrapolation
    - `test_two_point_same_side_extrapolation`: verify returned value is outside the two input points (extrapolated, not interpolated)
    - _Requirements: 4.1, 13.3_

  - [ ] 5.4 Write unit tests for direction-exhausted midpoint probe
    - `test_two_point_same_side_direction_exhausted_midpoint_probe`: midpoint returned on first exhaustion
    - `test_two_point_same_side_direction_exhausted_after_probe`: `None` returned after midpoint probe
    - `test_midpoint_probe_flag_resets_on_new_best`: flag resets when best-scoring point changes
    - _Requirements: 5.3, 5.4, 5.5, 13.4_

  - [ ] 5.5 Write unit tests for 2-point different-sides interpolation
    - `test_two_point_different_sides_interpolation`: returned value is between the two input points
    - _Requirements: 6.1, 13.5_

  - [ ] 5.6 Write unit tests for 3-point forks
    - `test_three_point_spanning_both_sides`: returned value is within the straddling sub-range
    - `test_three_point_all_same_side_sweet_spot`: returned value is midpoint of larger sub-range
    - `test_three_point_both_pairs_straddle_prefers_worse_quality`: worse-quality pair preferred
    - _Requirements: 7.1, 7.2, 7.4, 13.6, 13.7_

  - [ ] 5.7 Write unit tests for exhaustion behavior
    - `test_exhaustion_returns_none`: `record()` returns `None` after window collapses
    - `test_subsequent_calls_after_exhaustion_return_none`: all subsequent calls return `None`
    - _Requirements: 9.1, 9.3, 13.8_

  - [ ] 5.8 Write unit tests for linear score curves
    - `test_linear_curve_all_failing`, `test_linear_curve_all_passing`, `test_linear_curve_crossing_zero`
    - For crossing-zero: compute true crossing analytically (`q_cross = -intercept / slope`); assert `abs(float(search.best_quality) - q_cross) <= float(granularity)`
    - _Requirements: 13.9_

  - [ ] 5.9 Write unit tests for quadratic score curves
    - `test_quadratic_curve_all_failing`, `test_quadratic_curve_all_passing`, `test_quadratic_curve_crossing_zero_min_inside`, `test_quadratic_curve_crossing_zero_min_outside`
    - For crossing-zero variants: assert `abs(float(search.best_quality) - q_cross) <= float(granularity)`
    - _Requirements: 13.10_

- [ ] 6. Write property-based tests for `QualitySearchV3` in `tests/test_crf_search_properties.py`
  - [ ] 6.1 Write property test for convergence (Property 1)
    - Class `TestQualitySearchV3Convergence`, method `test_v3_converges_finite_attempts`
    - Tag: `# Feature: quality-search-v3, Property 1: Convergence`
    - Use `st.decimals` for range/granularity, `st.lists` of pass/fail booleans; minimum 200 iterations
    - Assert `record()` returns `None` within a finite number of attempts for any valid input
    - **Property 1: Convergence**
    - **Validates: Requirements 11.1, 14.1, 14.2**

  - [ ] 6.2 Extend finality test to include `QualitySearchV3` (Property 2)
    - Extend `TestFinalityAfterExhaustion` — add V3 to the `impl_idx` parametrization
    - Tag: `# Feature: quality-search-v3, Property 2: Finality`
    - Assert once `record()` returns `None`, all subsequent calls also return `None` and `attempts` does not increment
    - **Property 2: Finality**
    - **Validates: Requirements 9.2, 9.3, 14.3**

  - [ ] 6.3 Extend protocol state invariants test to include `QualitySearchV3` (Property 3)
    - Extend `TestProtocolStateInvariants` — add V3 to the `impl_idx` parametrization
    - Tag: `# Feature: quality-search-v3, Property 3: Protocol State Invariants`
    - Verify Requirements 10.1–10.5 hold for V3 across all generated inputs
    - **Property 3: Protocol State Invariants**
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 14.4**

  - [ ] 6.4 Write property test for output validity (Property 4)
    - Class `TestQualitySearchV3OutputValidity`, method `test_output_validity`
    - Tag: `# Feature: quality-search-v3, Property 4: Output Validity`
    - Assert every non-`None` returned value is snapped to granularity, within range, and not a repeat
    - **Property 4: Output Validity**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 14.1**

  - [ ] 6.5 Write property test for linear curve convergence (Property 5)
    - Class `TestQualitySearchV3LinearConvergence`, method `test_linear_curve_convergence`
    - Tag: `# Feature: quality-search-v3, Property 5: Linear Curve Convergence`
    - Use `st.floats` for slope and crossing point; assert sweet spot within 2 granularity units of true crossing; minimum 200 iterations
    - **Property 5: Linear Curve Convergence**
    - **Validates: Requirements 11.2, 14.5**

  - [ ] 6.6 Write property test for quadratic curve convergence (Property 6)
    - Class `TestQualitySearchV3QuadraticConvergence`, method `test_quadratic_curve_convergence`
    - Tag: `# Feature: quality-search-v3, Property 6: Quadratic Curve Convergence`
    - Use `st.floats` for `a`, `q_root`, `c`; assert sweet spot within 2 granularity units of nearest root; minimum 200 iterations
    - **Property 6: Quadratic Curve Convergence**
    - **Validates: Requirements 11.3, 14.6**

- [ ] 7. Final checkpoint — all tests pass
  - Run `uv run python -m pytest tests/unit/test_quality.py tests/test_crf_search_properties.py` and ensure all tests pass. Ask the user if questions arise.

- [ ] 8. Update spec completion date
  - Set the "Completed:" date in `requirements.md`, `design.md`, and `tasks.md` to today's date.
  - Review this spec against other specs (particularly `crf-search-refactor`) and add a summary at the top of both specs noting what was superseded or changed between them.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- All assertions in tests use only the public protocol API (`record()`, `best_quality`, `best_metrics`, `best_targets_met`, `attempts`) — no internal state inspection
- Property tests use the tag format `# Feature: quality-search-v3, Property N: <property_text>`
- The encoding pipeline (`pyqenc/phases/encoding.py`) is NOT modified as part of this spec — it continues to use `QualitySearchV2`
- Checkpoints ensure incremental validation after each major phase
