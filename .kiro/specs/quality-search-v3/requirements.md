# Requirements: QualitySearchV3

<!-- markdownlint-disable MD024 -->

- Created: 2026-07-14
- Completed:

---

## Introduction

`QualitySearchV2` (introduced in the `crf-search-refactor` spec) uses binary half-steps toward the codec boundary when both known points are on the same side of the target (all-failing or all-passing). This is safe but slow on monotonic curves — it takes O(log N) steps just to find the first crossing point before the 3-point sweet-spot phase can begin.

`QualitySearchV3` replaces that outward-search phase with **linear extrapolation**: given two same-side points, it projects where the quality curve would cross zero and jumps directly there. On a monotonic linear curve this finds the crossing in one step instead of many. A **midpoint-probe safety net** is added for the edge case where both points are same-side and the boundary in that direction has already been tested or hit — one midpoint probe is inserted before declaring full exhaustion, catching non-monotonic curves that V2 would miss.

V3 is a drop-in replacement for V2 (same `QualitySearchProtocol` interface, same constructor signature). It lives alongside V1 and V2 in `pyqenc/quality.py`. The encoding pipeline is **not** switched to V3 until tests prove it works.

---

## Glossary

- **QualitySearchProtocol**: The `typing.Protocol` in `pyqenc/quality.py` that all quality search implementations must satisfy. Defines `record()`, `attempts`, `best_quality`, `best_metrics`, `best_targets_met`.
- **QualitySearchV3**: The new algorithm class defined by this spec. Implements `QualitySearchProtocol`.
- **QualityPoint**: Dataclass `(q: Decimal, score: float, metrics: dict|None)` already defined in `quality.py`. Sentinel when `metrics is None`.
- **Score**: The signed float returned by `_score_attempt()`. Positive = pass (all targets met, but the least-proficit target's surplus exceeds its `acceptance_delta`). Zero = winner (all targets pass AND the least-proficit target's surplus ≤ its `acceptance_delta` — only the tightest constraint is checked). Negative = fail (at least one target not met).
- **Best-scoring point**: The `QualityPoint` with the smallest `abs(score)` among all recorded attempts, with passing attempts taking precedence over failing ones — a failing attempt with a smaller `abs(score)` cannot displace a passing attempt; only a passing attempt can displace a passing best.
- **Granularity**: Minimum step size between quality values, expressed as a `Decimal`.
- **quality_better**: The better-quality boundary of the codec range (e.g. CRF 0). Sentinel for the "better" side.
- **quality_worse**: The worse-quality boundary of the codec range (e.g. CRF 51). Sentinel for the "worse" side.
- **quality_max_step**: Optional `Decimal` cap on the absolute step size per `record()` call.
- **Sentinel point**: A `QualityPoint` with `metrics is None`, representing an untested boundary.
- **Direction-exhausted**: The condition where, for a pair of same-side points, the outward boundary has actually been tested (exists in recorded attempts as a real encoding result) and both points are still on the same side — no room to extrapolate further outward. The sentinel value is only the hard clamp boundary for output values; reaching the sentinel does not by itself constitute direction-exhaustion.
- **Range-exhausted**: The terminal condition where the active search window has collapsed to ≤ 1 granularity — no untested point can be placed inside it. This causes `record()` to return `None`.
- **Midpoint-probe flag**: A per-instance boolean that tracks whether the single midpoint probe (inserted when direction-exhausted) has already been used.
- **Linear extrapolation**: Given two points `(q1, score1)` and `(q2, score2)`, project the quality value where the score would cross zero: `q_zero = q1 - score1 * (q2 - q1) / (score2 - score1)`. The interpolation parameter `t` is allowed outside `[0, 1]` (extrapolation). Implemented via `_compute_proportional_candidate(..., clamp_range=False)`.
- **`_compute_proportional_candidate`**: Existing helper in `quality.py`. Returns interpolation fraction `t` for a given target metric. When `clamp_range=False`, `t` outside `[0, 1]` is returned as-is (extrapolation).
- **`_clamp_to_range`**: Existing helper in `quality.py`. Clamps a quality value to the valid interior of a range, respecting sentinels and granularity.
- **`_score_attempt`**: Existing function in `quality.py`. Returns the signed composite score for an attempt.
- **`_in_range`**: Existing helper in `quality.py`. Checks whether a value falls within a range (handles inverted ranges).

---

## Requirements

### Requirement 1: QualitySearchV3 Class and Base Class Compliance

**User Story:** As a developer, I want a `QualitySearchV3` class that inherits from `QualitySearchBase`, so that it can be used as a drop-in replacement for `QualitySearchV2` without changing any caller code.

#### Acceptance Criteria

1. THE `QualitySearchV3` class SHALL be defined in `pyqenc/quality.py` and SHALL inherit from `QualitySearchBase`.
2. THE `QualitySearchV3` constructor SHALL call `super().__init__()` and accept the same parameters as `QualitySearchV2`: `quality_better: Decimal`, `quality_worse: Decimal`, `quality_targets: list[QualityTarget]`, `granularity: Decimal`, `quality_max_step: Decimal | None = None`.
3. IF `quality_better == quality_worse`, THE `QualitySearchV3` constructor SHALL accept the input without raising. The first `record()` call SHALL record the result and return `None` (single fixed quality value, no search). All subsequent calls SHALL also return `None`.
4. IF `granularity <= 0`, THEN THE `QualitySearchV3` constructor SHALL raise `ValueError` (enforced by `QualitySearchBase.__init__`).
5. WHEN `record()` has never been called, THE `QualitySearchV3` SHALL return `None` for `best_quality` and `best_metrics`, `False` for `best_targets_met`, and `0` for `attempts`.
6. THE `QualitySearchV3` SHALL use `self._compute_next_quality(...)` (inherited from `QualitySearchBase`) rather than any standalone function or delegation to another class.
7. THE `QualitySearchV3` SHALL be importable from `pyqenc.quality` alongside `QualitySearch` and `QualitySearchV2`.

---

### Requirement 2: Initial Point Handling (Exactly 1 Recorded Attempt)

**User Story:** As a developer, I want the first recorded attempt to immediately steer the search toward the likely crossing region, so that the algorithm does not waste the first step on an uninformative midpoint.

#### Acceptance Criteria

1. WHEN `record()` is called for the first time and the score equals `0.0` (winner), THE `QualitySearchV3` SHALL set `best_quality` to that quality value, set `_exhausted = True`, and return `None`.
2. WHEN `record()` is called for the first time and the score is positive (pass), THE `QualitySearchV3` SHALL return a next quality value that is worse than the recorded point by half the range toward `quality_worse`, clamped by `quality_max_step` and snapped to granularity.
3. WHEN `record()` is called for the first time and the score is negative (fail), THE `QualitySearchV3` SHALL return a next quality value that is better than the recorded point by half the range toward `quality_better`, clamped by `quality_max_step` and snapped to granularity.
4. THE half-range step on the first attempt SHALL be computed as half the distance from the recorded quality value to the relevant boundary (`quality_worse` for a pass, `quality_better` for a fail), clamped by `quality_max_step`.
5. THE half-range step on the first attempt SHALL occur exactly once — only when `attempts == 1` after the `record()` call.

---

### Requirement 3: Point Selection (2+ Recorded Attempts)

**User Story:** As a developer, I want the algorithm to always work with the best-scoring point and its two immediate neighbours, so that the search focuses on the most informative region of the quality curve.

#### Acceptance Criteria

1. WHEN two or more attempts have been recorded, THE `QualitySearchV3` SHALL identify the **best-scoring point** as the recorded attempt with the smallest `abs(score)`, as computed by `_score_attempt` (which already encodes pass/fail precedence via sign).
2. THE `QualitySearchV3` SHALL select up to three points for the next decision: the best-scoring point, its immediate lower neighbour (the recorded attempt with the next lower quality value), and its immediate upper neighbour (the recorded attempt with the next higher quality value), determined by sorting all recorded attempts by quality parameter value.
3. WHEN the best-scoring point is at the lower edge of all recorded attempts (no lower neighbour), THE `QualitySearchV3` SHALL use only the best-scoring point and its upper neighbour (2-point decision).
4. WHEN the best-scoring point is at the upper edge of all recorded attempts (no upper neighbour), THE `QualitySearchV3` SHALL use only the best-scoring point and its lower neighbour (2-point decision).

---

### Requirement 4: Two-Point Same-Side Fork — Linear Extrapolation

**User Story:** As a developer, I want the algorithm to extrapolate linearly outside the two known same-side points, so that it converges to the crossing region in fewer steps on monotonic curves.

#### Acceptance Criteria

1. WHEN exactly 2 points are available for the decision and both are on the same side (both pass or both fail), AND the direction is not exhausted, THE `QualitySearchV3` SHALL compute the next quality value by linear extrapolation outside the two points using `_compute_proportional_candidate(..., clamp_range=False)`.
2. THE extrapolated quality value SHALL be clamped to the hard boundary on the outward side: the sentinel value (`quality_better` or `quality_worse`) if no opposite-side point has been tested, or the best-scoring opposite-side value if one exists.
3. THE extrapolated quality value SHALL be further clamped by `quality_max_step` from the last tested point (if `quality_max_step` is set).
4. THE extrapolated quality value SHALL be snapped to granularity using `ROUND_HALF_EVEN`.
5. THE extrapolated quality value SHALL never repeat a previously tested quality value; IF the snapped result collides with an already-tested value, THE `QualitySearchV3` SHALL move one granularity step in the outward direction if possible, otherwise treat the direction as exhausted.
6. WHEN the `_compute_proportional_candidate` call returns `None` (e.g. flat curve, missing metrics), THE `QualitySearchV3` SHALL fall back to the midpoint of the two points as the next candidate.

---

### Requirement 5: Two-Point Same-Side Fork — Direction-Exhausted Cases

**User Story:** As a developer, I want a midpoint-probe safety net when the outward direction is exhausted, so that non-monotonic curves are not prematurely declared exhausted.

#### Acceptance Criteria

1. THE direction toward `quality_better` SHALL be considered exhausted when the outward boundary in that direction has actually been tested (exists in recorded attempts as a real encoding result) and both same-side points are still on the same side — the sentinel value `quality_better` alone does NOT constitute exhaustion.
2. THE direction toward `quality_worse` SHALL be considered exhausted when the outward boundary in that direction has actually been tested (exists in recorded attempts as a real encoding result) and both same-side points are still on the same side — the sentinel value `quality_worse` alone does NOT constitute exhaustion.
3. WHEN 2 same-side points are present AND the direction is exhausted AND the midpoint-probe flag is NOT set, THE `QualitySearchV3` SHALL probe exactly the midpoint between the 2 points (snapped to granularity), set the midpoint-probe flag to `True`, and return that midpoint as the next quality value.
4. WHEN 2 same-side points are present AND the direction is exhausted AND the midpoint-probe flag IS set, THE `QualitySearchV3` SHALL set `_exhausted = True` and return `None` (range-exhausted).
5. THE midpoint-probe flag SHALL be reset to `False` whenever the best-scoring point changes (a new best is found), because the new configuration may allow further extrapolation.

---

### Requirement 6: Two-Point Different-Sides Fork — Proportional Interpolation

**User Story:** As a developer, I want the algorithm to use proportional interpolation when the two points straddle the target, so that it converges quickly to the crossing point.

#### Acceptance Criteria

1. WHEN exactly 2 points are available for the decision and they are on different sides (one pass, one fail), THE `QualitySearchV3` SHALL compute the next quality value by proportional interpolation between them using `_compute_proportional_candidate(..., clamp_range=True)`.
2. THE interpolated quality value SHALL be clamped to the interior of the range `[pass_point, fail_point]` (exclusive of both endpoints, inclusive of sentinel boundaries) using `_clamp_to_range`.
3. THE interpolated quality value SHALL be clamped by `quality_max_step` from the last tested point (if `quality_max_step` is set).
4. THE interpolated quality value SHALL be snapped to granularity using `ROUND_HALF_EVEN`.
5. WHEN `_compute_proportional_candidate` returns `None`, THE `QualitySearchV3` SHALL fall back to the midpoint of the two points.
6. WHEN the clamped, snapped result equals an already-tested quality value or `_clamp_to_range` returns `None`, THE `QualitySearchV3` SHALL set `_exhausted = True` and return `None`.

---

### Requirement 7: Three-Point Fork

**User Story:** As a developer, I want the algorithm to handle three-point configurations correctly, so that it narrows the search window efficiently regardless of whether the three points span both sides or are all on the same side.

#### Acceptance Criteria

1. WHEN 3 points are available (best + left neighbour + right neighbour) and they span both sides (at least one pass and at least one fail among the 3), THE `QualitySearchV3` SHALL reduce to the 2-point different-sides case by selecting the pair of adjacent points that are on opposite sides, then continue as per Requirement 6.
2. WHEN 3 points are available and all 3 are on the same side (all pass or all fail), THE `QualitySearchV3` SHALL perform a sweet-spot search: compute the sizes of the two sub-ranges (left-of-best and right-of-best), select the larger sub-range, and probe its midpoint.
3. THE midpoint of the selected sub-range SHALL be snapped to granularity and clamped by `quality_max_step`. WHEN the sub-range has collapsed to ≤ 1 granularity (no untested point can be placed inside it), THE `QualitySearchV3` SHALL set `_exhausted = True` and return `None`.
4. WHEN 3 points span both sides and both adjacent pairs straddle the target (lower-of-best and upper-of-best both contain a crossing), THE `QualitySearchV3` SHALL prefer the pair that includes the worse-quality neighbour, to bias toward more efficient encodings.

---

### Requirement 8: Output Constraints (All Returned Quality Values)

**User Story:** As a developer, I want every quality value returned by `record()` to be valid and non-repeating, so that the encoding loop never encodes the same quality twice or produces out-of-range values.

#### Acceptance Criteria

1. THE `QualitySearchV3` SHALL snap every returned quality value to granularity using `ROUND_HALF_EVEN` before returning it.
2. THE `QualitySearchV3` SHALL clamp every returned quality value by `quality_max_step` from the most recently tested quality value (if `quality_max_step` is set).
3. THE `QualitySearchV3` SHALL clamp every returned quality value to the current search range: sentinel-inclusive on open sides (where no real attempt has been made), one-granularity-exclusive on tested-point sides.
4. THE `QualitySearchV3` SHALL never return a quality value that has already been tested (exists in recorded attempts); IF a collision occurs after snapping and clamping, THE `QualitySearchV3` SHALL move one granularity step in the direction away from the boundary if possible, otherwise set `_exhausted = True` and return `None`.
5. THE `QualitySearchV3` SHALL never return a quality value outside `[min(quality_better, quality_worse), max(quality_better, quality_worse)]`.

---

### Requirement 9: Exhaustion and Finality

**User Story:** As a developer, I want the search to terminate cleanly and never return a non-None value after exhaustion, so that the encoding loop cannot run indefinitely.

#### Acceptance Criteria

1. WHEN the active search window has collapsed to ≤ 1 granularity (range-exhausted), THE `QualitySearchV3.record()` SHALL set `_exhausted = True` and return `None`.
2. WHEN `record()` returns `None` for any reason (winner, range-exhausted, direction-exhausted after midpoint probe), THE `QualitySearchV3` SHALL set `_exhausted = True`.
3. WHEN `_exhausted` is `True`, ALL subsequent `record()` calls SHALL return `None` immediately without mutating any state.
4. WHEN `record()` returns `None` due to exhaustion, THE `QualitySearchV3.best_quality` SHALL equal the best-efficiency passing quality value if any pass was recorded, otherwise the quality value of the attempt with the highest `_score_attempt()` score (closest to zero) among all failing attempts.
5. THE `QualitySearchV3.best_targets_met` SHALL be `True` if and only if at least one passing attempt was recorded.

---

### Requirement 10: Protocol State Invariants

**User Story:** As a developer, I want the protocol properties to always reflect a consistent state, so that callers can rely on them without defensive checks.

#### Acceptance Criteria

1. THE `QualitySearchV3.attempts` property SHALL equal the total number of `record()` calls made, regardless of pass/fail outcome or exhaustion state.
2. WHEN at least one `record()` call has been made, THE `QualitySearchV3.best_quality` SHALL NOT be `None`.
3. WHEN `best_targets_met` is `True`, THE `QualitySearchV3.best_quality` SHALL be the quality value of the passing attempt with the smallest `abs(score)` among all passing attempts recorded so far.
4. WHEN `best_targets_met` is `False`, THE `QualitySearchV3.best_quality` SHALL be the quality value of the attempt with the highest `_score_attempt()` score (closest to zero, i.e. smallest `abs(score)`) among all failing attempts recorded so far.
5. THE `QualitySearchV3.best_metrics` SHALL be the metrics dict associated with `best_quality`, or `None` before any `record()` call.

---

### Requirement 11: Convergence Guarantee

**User Story:** As a developer, I want `QualitySearchV3` to always terminate within a bounded number of attempts, so that the encoding pipeline cannot loop indefinitely.

#### Acceptance Criteria

1. FOR ALL valid inputs `(quality_better, quality_worse, granularity)` where `quality_better != quality_worse` and `granularity > 0`, THE `QualitySearchV3` SHALL terminate (return `None` from `record()`) within a finite number of attempts.
2. FOR ALL valid inputs, THE `QualitySearchV3` SHALL terminate in at most `O(log N)` attempts on monotonic score curves, where N is the number of distinct quality values in the range — the linear extrapolation phase SHALL converge faster than V2's binary half-steps on such curves.
3. FOR ALL valid inputs, THE `QualitySearchV3` SHALL terminate in at most `O(log N)` attempts on non-monotonic score curves — the 3-point sweet-spot phase and midpoint-probe safety net together ensure the search window halves on each step after the crossing is bracketed.

---

### Requirement 12: Minimal Changes to Existing Code

**User Story:** As a developer, I want V3 to be added with minimal changes to existing code, so that the encoding pipeline continues to use V2 until V3 is proven correct.

#### Acceptance Criteria

1. `QualitySearchProtocol` SHALL be replaced by `QualitySearchBase(ABC)` — an abstract base class with:
   - Shared constructor (raises `ValueError` only if `granularity <= 0`; `quality_better == quality_worse` is valid and results in single-point search)
   - Abstract `attempts`, `best_quality`, `best_metrics`, `best_targets_met`, and `record()` members (same contract as the former Protocol)
   - Protected helpers: `_score(metrics)`, `_find_worst_target(metrics)`, `_next_or_exhaust(next_q, attempted)`, `_finalize_q(raw_q, from_q, worse_point, better_point)`, `_compute_next_quality(new_point, worse_point, better_point)`
2. `_finalize_q` SHALL apply the full post-processing pipeline in order: max-step clamp → granularity snap → sentinel-aware range clamp. It SHALL return `None` when the range is exhausted. Every return path in every subclass SHALL use `_finalize_q` — no inline max-step/snap/clamp sequences in subclass code.
3. `QualitySearch` and `QualitySearchV2` SHALL be updated to inherit from `QualitySearchBase`, use `self._score(...)`, `self._find_worst_target(...)`, `self._next_or_exhaust(...)`, `self._finalize_q(...)`, and `self._compute_next_quality(...)` instead of the current inline code and module-level calls. The existing `ValueError` on `quality_better == quality_worse` SHALL be removed from both — the base class constructor accepts it.
4. Existing tests that assert `ValueError` when `quality_better == quality_worse` SHALL be updated to assert the single-point search behavior instead.
5. All `isinstance(x, QualitySearchProtocol)` checks in tests and production code SHALL be updated to `isinstance(x, QualitySearchBase)`.
6. THE module-level functions `_score_attempt` and `_find_worst_target` SHALL be removed and replaced by `self._score(metrics)` and `self._find_worst_target(metrics)` on the base class. All callers (including tests) SHALL be updated accordingly. The module-level `_compute_proportional_candidate`, `_clamp_to_range`, and `_in_range` SHALL remain unchanged.
7. THE module-level `normalize_metric` function SHALL be removed — it is dead code (never called) and is superseded by `MetricType.info.normalize()` directly.
8. THE encoding pipeline (`pyqenc/phases/encoding.py`) SHALL NOT be modified to use `QualitySearchV3` as part of this spec — it SHALL continue to use `QualitySearchV2`.
9. THE `QualitySearchV3` class SHALL be exportable from `pyqenc.quality` so it is importable by tests.

---

### Requirement 13: Unit Test Coverage

**User Story:** As a developer, I want unit tests with precalculated curves and known expected results, so that the algorithm's correctness on specific inputs is verified deterministically.

#### Acceptance Criteria

1. THE test suite SHALL include unit tests in `tests/unit/test_quality.py` covering `QualitySearchV3` initial state (all protocol properties before any `record()` call).
2. THE test suite SHALL include unit tests for the first-attempt half-range step: pass → step toward `quality_worse`, fail → step toward `quality_better`, winner → return `None`.
3. THE test suite SHALL include unit tests for the 2-point same-side extrapolation case: verify the returned quality value is outside the two input points (extrapolated, not interpolated).
4. THE test suite SHALL include unit tests for the 2-point same-side direction-exhausted case: verify the midpoint probe is returned on the first exhaustion, and `None` is returned on the second.
5. THE test suite SHALL include unit tests for the 2-point different-sides interpolation case: verify the returned quality value is between the two input points.
6. THE test suite SHALL include unit tests for the 3-point spanning-both-sides case: verify the returned quality value is within the straddling sub-range.
7. THE test suite SHALL include unit tests for the 3-point all-same-side case: verify the returned quality value is the midpoint of the larger sub-range.
8. THE test suite SHALL include unit tests for exhaustion: verify `record()` returns `None` after the search window collapses, and all subsequent calls also return `None`.
9. THE test suite SHALL include unit tests with a precalculated **linear score curve** (all-failing, all-passing, and crossing-zero variants) verifying that `QualitySearchV3` finds the sweet spot within 1 granularity of the true crossing point.
10. THE test suite SHALL include unit tests with a precalculated **quadratic score curve** (all-failing, all-passing, crossing-zero with minimum inside range, crossing-zero with minimum outside range) verifying that `QualitySearchV3` finds the sweet spot within 1 granularity of the true crossing point.
11. THE test suite SHALL NOT inspect internal state of `QualitySearchV3` — all assertions SHALL use only the public protocol API (`record()`, `best_quality`, `best_metrics`, `best_targets_met`, `attempts`).

---

### Requirement 14: Property-Based Test Coverage

**User Story:** As a developer, I want property-based tests covering `QualitySearchV3`'s convergence, finality, and state invariants, so that regressions are caught automatically across a wide range of inputs.

#### Acceptance Criteria

1. THE test suite SHALL include a property-based test in `tests/test_crf_search_properties.py` verifying that `QualitySearchV3` always terminates within a finite bound for any valid `(quality_better, quality_worse, granularity, score_sequence)`.
2. THE property-based test for convergence SHALL run a minimum of **200 iterations** using Hypothesis.
3. THE property-based test for finality SHALL verify that once `record()` returns `None`, all subsequent calls also return `None` — parametrized to include `QualitySearchV3` alongside `QualitySearch` and `QualitySearchV2`.
4. THE property-based test for protocol state invariants SHALL verify Requirements 10.1–10.5 for `QualitySearchV3` — parametrized to include `QualitySearchV3` alongside `QualitySearch` and `QualitySearchV2`.
5. THE property-based test for convergence on linear curves SHALL verify that `QualitySearchV3` finds the sweet spot within 2 granularity units of the true crossing point for any linear score curve that crosses zero within the quality range.
6. THE property-based test for convergence on quadratic curves SHALL verify that `QualitySearchV3` finds the sweet spot within 2 granularity units of the nearest root for any quadratic score curve with a root within the quality range.
7. ALL property-based tests for `QualitySearchV3` SHALL use the tag format `# Feature: quality-search-v3, Property N: <property_text>`.

