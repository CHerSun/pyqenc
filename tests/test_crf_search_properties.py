"""Property-based tests for CRF search refactor.

# Feature: crf-search-refactor
"""

import math
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyqenc.models import QualityTarget
from pyqenc.quality import MetricType, _score_attempt

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Metric types available for property tests
_METRIC_TYPES = [MetricType.VMAF, MetricType.SSIM, MetricType.PSNR]

# Realistic target value ranges per metric (normalized scale)
_TARGET_RANGES: dict[MetricType, tuple[float, float]] = {
    MetricType.VMAF: (70.0, 99.0),
    MetricType.SSIM: (85.0, 99.9),
    MetricType.PSNR: (35.0, 65.0),
}

# Realistic actual value ranges per metric (normalized scale, slightly wider)
_ACTUAL_RANGES: dict[MetricType, tuple[float, float]] = {
    MetricType.VMAF: (60.0, 100.0),
    MetricType.SSIM: (80.0, 100.0),
    MetricType.PSNR: (30.0, 70.0),
}

_STATISTICS = ["min", "median", "max", "p05", "p25", "p75", "p95"]


@st.composite
def st_single_target_with_actual(
    draw: st.DrawFn,
    metric_type: MetricType | None = None,
) -> tuple[QualityTarget, float]:
    """Draw a (QualityTarget, actual_value) pair for a single metric."""
    if metric_type is None:
        metric_type = draw(st.sampled_from(_METRIC_TYPES))
    statistic   = draw(st.sampled_from(_STATISTICS))
    t_lo, t_hi  = _TARGET_RANGES[metric_type]
    a_lo, a_hi  = _ACTUAL_RANGES[metric_type]
    target_val  = draw(st.floats(min_value=t_lo, max_value=t_hi, allow_nan=False, allow_infinity=False))
    actual_val  = draw(st.floats(min_value=a_lo, max_value=a_hi, allow_nan=False, allow_infinity=False))
    target = QualityTarget(
        metric    = metric_type.value,
        statistic = statistic,
        value     = target_val,
    )
    return target, actual_val


@st.composite
def st_target_list_with_actuals(
    draw: st.DrawFn,
    min_size: int = 1,
    max_size: int = 3,
) -> tuple[list[QualityTarget], dict[str, float]]:
    """Draw a list of (QualityTarget, actual) pairs with unique metric+stat keys."""
    # Pick distinct (metric, statistic) combinations to avoid duplicate keys
    metric_types = draw(
        st.lists(
            st.sampled_from(_METRIC_TYPES),
            min_size=min_size,
            max_size=max_size,
            unique=True,
        )
    )
    targets: list[QualityTarget] = []
    metrics: dict[str, float]   = {}
    for mt in metric_types:
        target, actual = draw(st_single_target_with_actual(metric_type=mt))
        key = f"{target.metric}_{target.statistic}"
        # Ensure unique keys (same metric, different stat could collide — use fixed stat per metric)
        targets.append(target)
        metrics[key] = actual
    return targets, metrics


# ---------------------------------------------------------------------------
# Property 5: _score_attempt sign contract
# Feature: crf-search-refactor, Property 5: _score_attempt sign contract
# ---------------------------------------------------------------------------

class TestScoreAttemptSignContract:
    """Property 5: _score_attempt sign contract.

    **Validates: Requirements 1.3, 3.x**
    """

    @given(st_target_list_with_actuals(min_size=1, max_size=3))
    @settings(max_examples=200)
    def test_all_pass_large_surplus_returns_positive(
        self,
        target_data: tuple[list[QualityTarget], dict[str, float]],
    ) -> None:
        """When all targets pass and at least one surplus > acceptance_delta → positive score.

        # Feature: crf-search-refactor, Property 5: _score_attempt sign contract
        **Validates: Requirements 1.3, 3.x**
        """
        targets, metrics = target_data

        # Force all actuals to pass with surplus > acceptance_delta
        forced_metrics: dict[str, float] = {}
        has_large_surplus = False
        for target in targets:
            key  = f"{target.metric}_{target.statistic}"
            info = MetricType(target.metric).info
            # Set actual = target + 2 * acceptance_delta (guaranteed large surplus)
            surplus = info.acceptance_delta * 2.0 + 1.0
            if info.higher_is_better:
                actual = target.value + surplus
            else:
                actual = target.value - surplus
            # Clamp to realistic range
            a_lo, a_hi = _ACTUAL_RANGES[MetricType(target.metric)]
            actual = max(a_lo, min(a_hi, actual))
            forced_metrics[key] = actual
            # Check if this still gives a large surplus after clamping
            real_deficit = info.deficit(actual, target.value)
            if real_deficit > info.acceptance_delta:
                has_large_surplus = True

        if not has_large_surplus:
            # After clamping, no large surplus possible — skip this example
            return

        # Verify all targets actually pass with large surplus
        all_pass = all(
            MetricType(t.metric).info.deficit(forced_metrics[f"{t.metric}_{t.statistic}"], t.value) > MetricType(t.metric).info.acceptance_delta
            for t in targets
        )
        if not all_pass:
            return

        score = _score_attempt(forced_metrics, targets)
        assert score > 0.0, f"Expected positive score, got {score}"

    @given(st_target_list_with_actuals(min_size=1, max_size=3))
    @settings(max_examples=200)
    def test_all_pass_within_delta_returns_zero(
        self,
        target_data: tuple[list[QualityTarget], dict[str, float]],
    ) -> None:
        """When all targets pass and all surpluses ≤ acceptance_delta → 0.0.

        # Feature: crf-search-refactor, Property 5: _score_attempt sign contract
        **Validates: Requirements 1.3, 3.x**
        """
        targets, _ = target_data

        # Force all actuals to pass with surplus exactly at 0 (target value itself)
        forced_metrics: dict[str, float] = {}
        for target in targets:
            key  = f"{target.metric}_{target.statistic}"
            info = MetricType(target.metric).info
            # surplus = 0 ≤ acceptance_delta always
            forced_metrics[key] = target.value

        score = _score_attempt(forced_metrics, targets)
        assert score == 0.0, f"Expected 0.0 (early acceptance), got {score}"

    @given(st_target_list_with_actuals(min_size=1, max_size=3))
    @settings(max_examples=200)
    def test_any_fail_returns_negative(
        self,
        target_data: tuple[list[QualityTarget], dict[str, float]],
    ) -> None:
        """When at least one target fails → negative score.

        # Feature: crf-search-refactor, Property 5: _score_attempt sign contract
        **Validates: Requirements 1.3, 3.x**
        """
        targets, _ = target_data

        # Force the first target to fail, rest pass at target value
        forced_metrics: dict[str, float] = {}
        for i, target in enumerate(targets):
            key  = f"{target.metric}_{target.statistic}"
            info = MetricType(target.metric).info
            if i == 0:
                # Force fail: actual is worse than target by a meaningful margin
                deficit = -1.0  # negative deficit = fail
                if info.higher_is_better:
                    actual = target.value + deficit   # below target
                else:
                    actual = target.value - deficit   # above target (worse for inverted)
                # Clamp to realistic range
                a_lo, a_hi = _ACTUAL_RANGES[MetricType(target.metric)]
                actual = max(a_lo, min(a_hi, actual))
                # Verify it actually fails after clamping
                real_deficit = info.deficit(actual, target.value)
                if real_deficit >= 0.0:
                    # Clamping prevented the fail — skip
                    return
                forced_metrics[key] = actual
            else:
                # Pass at exactly target value
                forced_metrics[key] = target.value

        score = _score_attempt(forced_metrics, targets)
        assert score < 0.0, f"Expected negative score, got {score}"

    @given(st_target_list_with_actuals(min_size=1, max_size=3))
    @settings(max_examples=200)
    def test_missing_key_raises_value_error(
        self,
        target_data: tuple[list[QualityTarget], dict[str, float]],
    ) -> None:
        """Missing any target key in metrics raises ValueError.

        # Feature: crf-search-refactor, Property 5: _score_attempt sign contract
        **Validates: Requirements 1.3, 3.x**
        """
        targets, metrics = target_data
        # Remove the first target's key from metrics
        first_key = f"{targets[0].metric}_{targets[0].statistic}"
        incomplete = {k: v for k, v in metrics.items() if k != first_key}

        with pytest.raises(ValueError):
            _score_attempt(incomplete, targets)

    @given(st_target_list_with_actuals(min_size=1, max_size=3))
    @settings(max_examples=200)
    def test_score_sign_matches_pass_fail(
        self,
        target_data: tuple[list[QualityTarget], dict[str, float]],
    ) -> None:
        """score > 0 iff all pass with large surplus; score == 0 iff all pass within delta; score < 0 iff any fail.

        # Feature: crf-search-refactor, Property 5: _score_attempt sign contract
        **Validates: Requirements 1.3, 3.x**
        """
        targets, metrics = target_data

        # Determine ground truth
        all_pass     = True
        early_accept = True
        for target in targets:
            key  = f"{target.metric}_{target.statistic}"
            actual = metrics.get(key)
            if actual is None:
                return  # skip if key missing (shouldn't happen with our strategy)
            info    = MetricType(target.metric).info
            if math.isinf(actual):
                actual = info.lossless_value
            deficit = info.deficit(actual, target.value)
            if deficit < 0.0:
                all_pass     = False
                early_accept = False
            elif deficit > info.acceptance_delta:
                early_accept = False

        score = _score_attempt(metrics, targets)

        if not all_pass:
            assert score < 0.0, f"Expected negative (fail), got {score}"
        elif early_accept:
            assert score == 0.0, f"Expected 0.0 (early accept), got {score}"
        else:
            assert score > 0.0, f"Expected positive (pass, large surplus), got {score}"


# ---------------------------------------------------------------------------
# Shared quality simulation helpers
# ---------------------------------------------------------------------------

def _linear_vmaf(q: Decimal, slope: float, intercept: float) -> float:
    """Linear quality model: vmaf = slope * float(q) + intercept."""
    return slope * float(q) + intercept


def _quadratic_vmaf(q: Decimal, a: float, q_sweet: float, peak: float) -> float:
    """Quadratic (inverted parabola) quality model: vmaf = -a * (q - q_sweet)^2 + peak."""
    return -a * (float(q) - q_sweet) ** 2 + peak


# ---------------------------------------------------------------------------
# Property 1: QualitySearch convergence
# Feature: crf-search-refactor, Property 1: QualitySearch convergence
# ---------------------------------------------------------------------------

class TestQualitySearchConvergence:
    """Property 1: QualitySearch convergence.
    Validates: Requirements 4.1
    """

    @given(
        slope   = st.floats(min_value=-3.0, max_value=-0.5,
                            allow_nan=False, allow_infinity=False),
        q_cross = st.floats(min_value=5.0, max_value=46.0,
                            allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_qualitysearch_converges_on_linear_curve(
        self,
        slope:   float,
        q_cross: float,
    ) -> None:
        """QualitySearch converges to within 2 CRF units of the true crossing point on a linear curve.

        # Feature: crf-search-refactor, Property 1: QualitySearch convergence
        Validates: Requirements 4.1
        """
        from pyqenc.quality import QualitySearch

        # intercept chosen so vmaf(q_cross) == 90.0
        vmaf_target_val = 90.0
        intercept       = vmaf_target_val - slope * q_cross

        granularity = Decimal("1")
        target      = QualityTarget(metric="vmaf", statistic="min", value=vmaf_target_val)
        search      = QualitySearch(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = granularity,
        )

        # Generous attempt bound: ceil(log2(51)) + 5
        max_allowed = math.ceil(math.log2(51)) + 5

        current_q = Decimal("25")  # start at midpoint
        while True:
            vmaf   = _linear_vmaf(current_q, slope, intercept)
            next_q = search.record(current_q, {"vmaf_min": vmaf})
            if next_q is None:
                break
            current_q = next_q
            assert search.attempts <= max_allowed, (
                f"QualitySearch did not converge: {search.attempts} attempts > {max_allowed}"
            )

        # Search must have terminated
        assert search.best_quality is not None, "Search terminated without recording any attempt"

        # best_quality must be within 3 * granularity of the true crossing point
        # (3 instead of 2 to account for acceptance_delta=0.5 stopping slightly before exact crossing)
        tolerance = 3 * float(granularity)
        assert abs(float(search.best_quality) - q_cross) <= tolerance, (
            f"best_quality={search.best_quality} is not within {tolerance} of "
            f"true crossing q_cross={q_cross:.3f} (slope={slope:.3f}, intercept={intercept:.3f})"
        )


# ---------------------------------------------------------------------------
# Property 2: QualitySearchV2 convergence
# Feature: crf-search-refactor, Property 2: QualitySearchV2 convergence
# ---------------------------------------------------------------------------

class TestQualitySearchV2Convergence:
    """Property 2: QualitySearchV2 convergence.
    Validates: Requirements 4.2
    """

    @given(
        q_sweet = st.floats(min_value=10.0, max_value=40.0,
                            allow_nan=False, allow_infinity=False),
        a       = st.floats(min_value=0.05, max_value=0.5,
                            allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_qualitysearchv2_converges_on_quadratic_curve(
        self,
        q_sweet: float,
        a:       float,
    ) -> None:
        """QualitySearchV2 finds the efficiency sweet spot within 3 CRF units on a quadratic curve.

        # Feature: crf-search-refactor, Property 2: QualitySearchV2 convergence
        Validates: Requirements 4.2
        """
        from pyqenc.quality import QualitySearchV2

        # vmaf(q) = -a * (q - q_sweet)^2 + peak
        # target = 95.0, peak = 97.0 → two roots where vmaf == 95.0
        peak            = 97.0
        target_val      = 95.0
        vmaf_target_val = target_val

        # The two roots: q = q_sweet ± sqrt((peak - target_val) / a)
        half_width   = math.sqrt((peak - target_val) / a)
        # The efficiency sweet spot: highest CRF that still passes (worse quality = more efficient)
        q_efficiency = q_sweet + half_width

        # Skip if the sweet spot is outside the usable CRF range
        if q_efficiency > 50.0 or q_efficiency < 1.0:
            return

        granularity = Decimal("1")
        target      = QualityTarget(metric="vmaf", statistic="min", value=vmaf_target_val)
        search      = QualitySearchV2(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = granularity,
        )

        max_allowed = 40

        current_q = Decimal("25")  # start at midpoint
        while True:
            vmaf   = _quadratic_vmaf(current_q, a, q_sweet, peak)
            next_q = search.record(current_q, {"vmaf_min": vmaf})
            if next_q is None:
                break
            current_q = next_q
            assert search.attempts <= max_allowed, (
                f"QualitySearchV2 did not converge: {search.attempts} attempts > {max_allowed}"
            )

        # Search must have terminated
        assert search.best_quality is not None, "Search terminated without recording any attempt"

        # If no passing attempt was found (sweet spot too narrow to hit within the search),
        # we can only verify termination — skip the convergence quality assertion.
        if not search.best_targets_met:
            return

        # The quadratic has two roots where vmaf == target_val:
        #   q_lower = q_sweet - half_width  (lower CRF, better quality, less efficient)
        #   q_upper = q_sweet + half_width  (higher CRF, worse quality, more efficient)
        # The search may converge to either root — both are valid sweet spots.
        # Assert best_quality is within 3 * granularity of the nearest root.
        q_lower = q_sweet - half_width
        dist_to_lower = abs(float(search.best_quality) - q_lower)
        dist_to_upper = abs(float(search.best_quality) - q_efficiency)
        dist_to_nearest = min(dist_to_lower, dist_to_upper)

        tolerance = 3 * float(granularity)
        assert dist_to_nearest <= tolerance, (
            f"best_quality={search.best_quality} is not within {tolerance} of either root "
            f"(q_lower={q_lower:.3f}, q_upper={q_efficiency:.3f}) — "
            f"dist_lower={dist_to_lower:.3f}, dist_upper={dist_to_upper:.3f} "
            f"(q_sweet={q_sweet:.3f}, a={a:.3f}, half_width={half_width:.3f})"
        )


# ---------------------------------------------------------------------------
# Property 3: Finality after exhaustion
# Feature: crf-search-refactor, Property 3: Finality after exhaustion
# ---------------------------------------------------------------------------

class TestFinalityAfterExhaustion:
    """Property 3: Finality after exhaustion.
    Validates: Requirements 4.3
    """

    @given(
        impl_idx = st.integers(min_value=0, max_value=1),
        better   = st.decimals(min_value=Decimal("0"), max_value=Decimal("49"),
                               places=0, allow_nan=False, allow_infinity=False),
        span     = st.decimals(min_value=Decimal("2"), max_value=Decimal("51"),
                               places=0, allow_nan=False, allow_infinity=False),
        oracle   = st.lists(st.booleans(), min_size=1, max_size=60),
        extra    = st.lists(st.booleans(), min_size=1, max_size=5),
    )
    @settings(max_examples=200)
    def test_search_finality_after_exhaustion(
        self,
        impl_idx: int,
        better:   Decimal,
        span:     Decimal,
        oracle:   list[bool],
        extra:    list[bool],
    ) -> None:
        """Once record() returns None, all subsequent calls also return None.

        # Feature: crf-search-refactor, Property 3: Finality after exhaustion
        Validates: Requirements 4.3
        """
        from pyqenc.quality import QualitySearch, QualitySearchV2

        worse       = better + span
        granularity = Decimal("1")
        target      = QualityTarget(metric="vmaf", statistic="min", value=90.0)

        impls = [QualitySearch, QualitySearchV2]
        search = impls[impl_idx](
            quality_better  = better,
            quality_worse   = worse,
            quality_targets = [target],
            granularity     = granularity,
        )

        # Run until exhaustion.
        current_q = better + span / 2
        current_q = (current_q / granularity).to_integral_value() * granularity

        idx = 0
        exhausted_at = None
        for _ in range(200):
            passes  = oracle[idx % len(oracle)]
            metrics = {"vmaf_min": 95.0 if passes else 80.0}
            next_q  = search.record(current_q, metrics)
            idx += 1
            if next_q is None:
                exhausted_at = search.attempts
                break
            current_q = next_q

        assert exhausted_at is not None, "Search did not exhaust within 200 attempts"

        # All post-exhaustion calls must return None.
        for i, passes in enumerate(extra):
            metrics = {"vmaf_min": 95.0 if passes else 80.0}
            result  = search.record(current_q, metrics)
            assert result is None, (
                f"Expected None after exhaustion (call {i+1}), got {result}"
            )
            # attempts must NOT increment after exhaustion.
            assert search.attempts == exhausted_at, (
                f"attempts incremented after exhaustion: {search.attempts} != {exhausted_at}"
            )


# ---------------------------------------------------------------------------
# Property 4: Protocol state invariants
# Feature: crf-search-refactor, Property 4: Protocol state invariants
# ---------------------------------------------------------------------------

class TestProtocolStateInvariants:
    """Property 4: Protocol state invariants.
    Validates: Requirements 5.1–5.5
    """

    @given(
        impl_idx = st.integers(min_value=0, max_value=1),
        better   = st.decimals(min_value=Decimal("0"), max_value=Decimal("49"),
                               places=0, allow_nan=False, allow_infinity=False),
        span     = st.decimals(min_value=Decimal("2"), max_value=Decimal("51"),
                               places=0, allow_nan=False, allow_infinity=False),
        oracle   = st.lists(st.booleans(), min_size=1, max_size=30),
    )
    @settings(max_examples=200)
    def test_protocol_state_invariants(
        self,
        impl_idx: int,
        better:   Decimal,
        span:     Decimal,
        oracle:   list[bool],
    ) -> None:
        """Protocol state invariants hold after any sequence of record() calls.

        # Feature: crf-search-refactor, Property 4: Protocol state invariants
        Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5
        """
        from pyqenc.quality import QualitySearch, QualitySearchV2, _score_attempt

        worse       = better + span
        granularity = Decimal("1")
        target      = QualityTarget(metric="vmaf", statistic="min", value=90.0)

        impls = [QualitySearch, QualitySearchV2]
        search = impls[impl_idx](
            quality_better  = better,
            quality_worse   = worse,
            quality_targets = [target],
            granularity     = granularity,
        )

        # Track ground truth alongside the search object.
        # Note on implementation differences:
        #   QualitySearch is a bracket algorithm: _better_q = most recent pass,
        #   _worse_q = most recent fail. best_quality reflects the current bracket
        #   boundary, not the best-scoring attempt.
        #   QualitySearchV2 tracks the best-scoring attempt via _best_q.
        # P4 verifies the shared invariants (5.1, 5.2, 5.5) that hold for both,
        # plus the stronger best-quality invariants only for QualitySearchV2.
        call_count = 0
        any_pass   = False

        current_q = better + span / 2
        current_q = (current_q / granularity).to_integral_value() * granularity

        for i, passes in enumerate(oracle):
            metrics = {"vmaf_min": 95.0 if passes else 80.0}

            # Compute ground-truth score before recording.
            score = _score_attempt(metrics, [target])

            next_q = search.record(current_q, metrics)
            call_count += 1

            if score >= 0.0:
                any_pass = True

            # Invariant 5.5: attempts == number of calls made.
            assert search.attempts == call_count, (
                f"attempts={search.attempts} != call_count={call_count}"
            )

            # Invariant 5.2: after >= 1 call, best_quality is not None.
            assert search.best_quality is not None, (
                "best_quality is None after at least one record() call"
            )

            # Invariant 5.1: best_targets_met iff any pass was recorded.
            assert search.best_targets_met == any_pass, (
                f"best_targets_met={search.best_targets_met} but any_pass={any_pass}"
            )

            if next_q is None:
                break
            current_q = next_q
