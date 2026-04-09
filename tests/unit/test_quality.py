"""Unit tests for quality evaluation and CRF adjustment."""

from decimal import Decimal

import pytest

from pyqenc.models import CodecConfig, QualityTarget
from pyqenc.quality import QualitySearch, _score_attempt

_CRF_MIN    = Decimal("0")
_CRF_MAX    = Decimal("51")
_GRAN       = Decimal("0.5")   # default granularity used in tests
_GRAN_INT   = Decimal("1")     # integer-step granularity (QP-style)


def _d(v: str | int | float) -> Decimal:
    """Shorthand: convert to Decimal via str to avoid float imprecision."""
    return Decimal(str(v))


def _vmaf_target(value: float = 95.0) -> QualityTarget:
    return QualityTarget(metric="vmaf", statistic="min", value=value)


def _pass_metrics(vmaf: float = 96.0) -> dict[str, float]:
    """Metrics that pass a vmaf-min=95 target."""
    return {"vmaf_min": vmaf}


def _fail_metrics(vmaf: float = 80.0) -> dict[str, float]:
    """Metrics that fail a vmaf-min=95 target."""
    return {"vmaf_min": vmaf}


def _make_search(
    better: str = "0",
    worse:  str = "51",
    gran:   str = "0.5",
    target: float = 95.0,
) -> QualitySearch:
    return QualitySearch(
        quality_better  = _d(better),
        quality_worse   = _d(worse),
        quality_targets = [_vmaf_target(target)],
        granularity     = _d(gran),
    )


class TestQualitySearch:
    """Unit tests for QualitySearch. Validates: Requirements 2.1–2.3, 2.6, 2.7, 5.1–5.5"""

    _BETTER = Decimal("0")
    _WORSE  = Decimal("51")
    _GRAN   = Decimal("0.5")
    _TARGET = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]

    def _make(self, **kw: object) -> QualitySearch:
        return QualitySearch(
            quality_better  = kw.get("better", self._BETTER),  # type: ignore[arg-type]
            quality_worse   = kw.get("worse",  self._WORSE),   # type: ignore[arg-type]
            quality_targets = kw.get("targets", self._TARGET),  # type: ignore[arg-type]
            granularity     = kw.get("gran",   self._GRAN),    # type: ignore[arg-type]
        )

    def test_initial_state(self) -> None:
        s = self._make()
        assert s.best_quality is None
        assert s.best_metrics is None
        assert s.best_targets_met is False
        assert s.attempts == 0

    def test_pass_updates_best(self) -> None:
        s = self._make()
        next_q = s.record(Decimal("18.0"), {"vmaf_min": 96.0})
        assert s.best_targets_met is True
        assert s.best_quality == Decimal("18.0")
        assert s.attempts == 1

    def test_fail_updates_best_fail(self) -> None:
        s = self._make()
        s.record(Decimal("25.0"), {"vmaf_min": 80.0})
        assert s.best_targets_met is False
        assert s.best_quality == Decimal("25.0")
        assert s.attempts == 1

    def test_attempts_increments_every_call(self) -> None:
        s = self._make()
        s.record(Decimal("25.0"), {"vmaf_min": 80.0})
        s.record(Decimal("15.0"), {"vmaf_min": 96.0})
        assert s.attempts == 2

    def test_exhaustion_returns_none(self) -> None:
        # Tight bracket: better=19.0, worse=19.5 (span == granularity)
        s = QualitySearch(
            quality_better  = Decimal("19.0"),
            quality_worse   = Decimal("19.5"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        # First record a pass at better, then a fail at worse — bracket collapses
        s.record(Decimal("19.0"), {"vmaf_min": 96.0})
        result = s.record(Decimal("19.5"), {"vmaf_min": 80.0})
        assert result is None

    def test_subsequent_calls_after_exhaustion_return_none(self) -> None:
        s = QualitySearch(
            quality_better  = Decimal("19.0"),
            quality_worse   = Decimal("19.5"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        s.record(Decimal("19.0"), {"vmaf_min": 96.0})
        s.record(Decimal("19.5"), {"vmaf_min": 80.0})
        # All subsequent calls must return None
        assert s.record(Decimal("19.0"), {"vmaf_min": 96.0}) is None
        assert s.record(Decimal("19.5"), {"vmaf_min": 80.0}) is None

    def test_raises_if_better_equals_worse(self) -> None:
        with pytest.raises(ValueError):
            QualitySearch(
                quality_better  = Decimal("18"),
                quality_worse   = Decimal("18"),
                quality_targets = self._TARGET,
                granularity     = self._GRAN,
            )

    def test_raises_if_granularity_zero(self) -> None:
        with pytest.raises(ValueError):
            QualitySearch(
                quality_better  = Decimal("0"),
                quality_worse   = Decimal("51"),
                quality_targets = self._TARGET,
                granularity     = Decimal("0"),
            )

    def test_early_acceptance_returns_none(self) -> None:
        # surplus well within acceptance_delta → early acceptance
        from pyqenc.quality import MetricType
        delta  = MetricType.VMAF.info.acceptance_delta
        s      = self._make()
        result = s.record(Decimal("18.0"), {"vmaf_min": 95.0 + delta * 0.5})
        assert result is None
        assert s.best_targets_met is True
        assert s._exhausted is True

    def test_pass_then_fail_best_quality_is_pass(self) -> None:
        s = self._make()
        s.record(Decimal("18.0"), {"vmaf_min": 96.0})  # pass
        s.record(Decimal("20.0"), {"vmaf_min": 80.0})  # fail
        assert s.best_targets_met is True
        assert s.best_quality == Decimal("18.0")


class TestCodecConfigQualityLogPadding:
    """Tests for CodecConfig.quality_log_padding.

    Validates: Requirements 10.7
    """

    def test_crf_range_padding(self):
        """CRF range [0, 51] with granularity 0.5 → padding 4 (len("51.0") == 4)."""
        codec = CodecConfig(
            name="test-crf",
            default_quality=Decimal("18"),
            quality_range=(Decimal("0"), Decimal("51")),
            quality_granularity=Decimal("0.5"),
            encoder_args=["-i", "{input}", "{quality}"],
            presets=["slow"],
        )
        assert codec.quality_log_padding == 4

    def test_vbr_range_padding(self):
        """VBR range [0, 100] with granularity 0.1 → padding 5 (len("100.0") == 5)."""
        codec = CodecConfig(
            name="test-vbr",
            default_quality=Decimal("50"),
            quality_range=(Decimal("0"), Decimal("100")),
            quality_granularity=Decimal("0.1"),
            encoder_args=["-i", "{input}", "{quality}"],
            presets=["slow"],
        )
        assert codec.quality_log_padding == 5

    def test_qp_range_padding(self):
        """QP range [0, 63] with granularity 1 → padding 2 (len("63") == 2)."""
        codec = CodecConfig(
            name="test-qp",
            default_quality=Decimal("32"),
            quality_range=(Decimal("0"), Decimal("63")),
            quality_granularity=Decimal("1"),
            encoder_args=["-i", "{input}", "{quality}"],
            presets=["slow"],
        )
        assert codec.quality_log_padding == 2


class TestScoreAttempt:
    """Unit tests for _score_attempt().

    Validates: Requirements 1.3
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vmaf_target(value: float = 95.0) -> QualityTarget:
        return QualityTarget(metric="vmaf", statistic="min", value=value)

    @staticmethod
    def _psnr_target(value: float = 45.0) -> QualityTarget:
        return QualityTarget(metric="psnr", statistic="median", value=value)

    # ------------------------------------------------------------------
    # Early acceptance (returns 0.0)
    # ------------------------------------------------------------------

    def test_early_acceptance_single_target_at_delta(self):
        """All targets pass and surplus well within acceptance_delta → 0.0."""
        from pyqenc.quality import MetricType
        target = self._vmaf_target(95.0)
        delta  = MetricType.VMAF.info.acceptance_delta
        # surplus = delta * 0.5 — clearly within, avoids float boundary issues
        metrics = {"vmaf_min": 95.0 + delta * 0.5}
        assert _score_attempt(metrics, [target]) == 0.0

    def test_early_acceptance_surplus_below_delta(self):
        """Surplus well below acceptance_delta → 0.0."""
        from pyqenc.quality import MetricType
        target = self._vmaf_target(95.0)
        delta  = MetricType.VMAF.info.acceptance_delta
        metrics = {"vmaf_min": 95.0 + delta * 0.1}
        assert _score_attempt(metrics, [target]) == 0.0

    def test_early_acceptance_multiple_targets_all_within_delta(self):
        """Multiple targets all within acceptance_delta → 0.0."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        vmaf_delta = MetricType.VMAF.info.acceptance_delta
        psnr_delta = MetricType.PSNR.info.acceptance_delta
        metrics = {
            "vmaf_min":    95.0 + vmaf_delta * 0.5,
            "psnr_median": 45.0 + psnr_delta * 0.5,
        }
        assert _score_attempt(metrics, targets) == 0.0

    # ------------------------------------------------------------------
    # Pass (returns positive)
    # ------------------------------------------------------------------

    def test_pass_large_surplus_returns_positive(self):
        """All targets pass with surplus > acceptance_delta → positive score."""
        target  = self._vmaf_target(80.0)
        metrics = {"vmaf_min": 95.0}   # surplus = 15, well above delta
        score   = _score_attempt(metrics, [target])
        assert score > 0.0

    def test_pass_score_sums_all_targets(self):
        """Pass score sums surplus/comparison_range for ALL targets."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=80.0),
            QualityTarget(metric="psnr", statistic="median", value=40.0),
        ]
        metrics = {
            "vmaf_min":    95.0,   # surplus = 15
            "psnr_median": 50.0,   # surplus = 10
        }
        score = _score_attempt(metrics, targets)
        vmaf_info = MetricType.VMAF.info
        psnr_info = MetricType.PSNR.info
        expected  = (15.0 / vmaf_info.comparison_range) + (10.0 / psnr_info.comparison_range)
        assert abs(score - expected) < 1e-9
        assert score > 0.0

    def test_pass_one_target_above_delta_one_below(self):
        """One surplus > delta, one ≤ delta → positive (not early acceptance)."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=80.0),
            QualityTarget(metric="psnr", statistic="median", value=44.9),
        ]
        psnr_delta = MetricType.PSNR.info.acceptance_delta
        metrics = {
            "vmaf_min":    95.0,                    # large surplus > delta
            "psnr_median": 44.9 + psnr_delta * 0.5, # tiny surplus ≤ delta
        }
        score = _score_attempt(metrics, targets)
        assert score > 0.0

    # ------------------------------------------------------------------
    # Fail (returns negative)
    # ------------------------------------------------------------------

    def test_fail_single_target_returns_negative(self):
        """Any target not met → negative score."""
        target  = self._vmaf_target(95.0)
        metrics = {"vmaf_min": 90.0}   # deficit = -5
        score   = _score_attempt(metrics, [target])
        assert score < 0.0

    def test_fail_score_sums_only_failing_targets(self):
        """Fail score sums deficit/comparison_range for FAILING targets only."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        metrics = {
            "vmaf_min":    90.0,   # fails: deficit = -5
            "psnr_median": 50.0,   # passes: surplus = 5 (should NOT contribute)
        }
        score     = _score_attempt(metrics, targets)
        vmaf_info = MetricType.VMAF.info
        expected  = -5.0 / vmaf_info.comparison_range   # only vmaf contributes
        assert abs(score - expected) < 1e-9
        assert score < 0.0

    def test_fail_multiple_failing_targets(self):
        """Multiple failing targets all contribute to the negative score."""
        from pyqenc.quality import MetricType
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        metrics = {
            "vmaf_min":    90.0,   # deficit = -5
            "psnr_median": 40.0,   # deficit = -5
        }
        score     = _score_attempt(metrics, targets)
        vmaf_info = MetricType.VMAF.info
        psnr_info = MetricType.PSNR.info
        expected  = (-5.0 / vmaf_info.comparison_range) + (-5.0 / psnr_info.comparison_range)
        assert abs(score - expected) < 1e-9
        assert score < 0.0

    # ------------------------------------------------------------------
    # Missing key raises ValueError
    # ------------------------------------------------------------------

    def test_missing_key_raises_value_error(self):
        """Missing metric key in metrics dict raises ValueError."""
        target  = self._vmaf_target(95.0)
        metrics = {"psnr_min": 50.0}   # wrong key
        with pytest.raises(ValueError, match="vmaf_min"):
            _score_attempt(metrics, [target])

    def test_missing_one_of_multiple_keys_raises(self):
        """Missing any one key among multiple targets raises ValueError."""
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="psnr", statistic="median", value=45.0),
        ]
        metrics = {"vmaf_min": 96.0}   # psnr_median missing
        with pytest.raises(ValueError, match="psnr_median"):
            _score_attempt(metrics, targets)

    def test_empty_targets_returns_zero(self):
        """No targets → all pass trivially → early acceptance → 0.0."""
        assert _score_attempt({}, []) == 0.0

    # ------------------------------------------------------------------
    # Inf handling
    # ------------------------------------------------------------------

    def test_inf_actual_not_expected_but_handled_gracefully(self):
        """inf actual value is not expected (metrics are pre-normalized) but doesn't crash."""
        target  = QualityTarget(metric="psnr", statistic="min", value=45.0)
        # inf would be a bug upstream, but _score_attempt should not crash —
        # it just computes deficit(inf, 45.0) = inf - 45.0 = inf → positive score
        metrics = {"psnr_min": float("inf")}
        score   = _score_attempt(metrics, [target])
        # inf - 45.0 = inf → surplus is inf → positive score
        assert score > 0.0


from pyqenc.quality import QualitySearchV2


class TestQualitySearchV2:
    """Unit tests for QualitySearchV2 state transitions.

    Uses CRF range [0, 51] (lower=better), granularity 0.5, VMAF target min=95.0.
    """

    _BETTER  = Decimal("0")
    _WORSE   = Decimal("51")
    _GRAN    = Decimal("0.5")
    _TARGET  = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]

    # pass metrics: vmaf_min=96.0 → surplus=1.0 > delta → positive score
    _PASS_M  = {"vmaf_min": 96.0}
    # fail metrics: vmaf_min=80.0 → deficit=-15 → negative score
    _FAIL_M  = {"vmaf_min": 80.0}

    @staticmethod
    def _early_m() -> dict[str, float]:
        """Metrics that trigger early acceptance: surplus = delta * 0.5."""
        from pyqenc.quality import MetricType
        delta = MetricType.VMAF.info.acceptance_delta
        return {"vmaf_min": 95.0 + delta * 0.5}

    def _make(self) -> QualitySearchV2:
        return QualitySearchV2(
            quality_better  = self._BETTER,
            quality_worse   = self._WORSE,
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------

    def test_initial_sentinel_state(self) -> None:
        """_pass_q == quality_better, _best_q == quality_worse, _fail_q == quality_worse."""
        s = self._make()
        assert s._better_q == self._BETTER
        assert s._worse_q == self._WORSE
        assert s._middle_q == self._WORSE
        assert s.best_quality is None
        assert s.best_metrics is None
        assert s.best_targets_met is False
        assert s.attempts == 0

    # ------------------------------------------------------------------
    # All-failing phase
    # ------------------------------------------------------------------

    def test_all_failing_first_attempt_becomes_best(self) -> None:
        """First fail: _best_q = quality, _fail_q = quality_worse (unchanged sentinel)."""
        s = self._make()
        s.record(Decimal("25"), self._FAIL_M)
        assert s._worse_q == Decimal("25")
        assert s._middle_q == self._WORSE   # sentinel unchanged
        assert s.best_quality == Decimal("25")
        assert s.best_targets_met is False

    def test_all_failing_new_best_updates_state(self) -> None:
        """Two fails: second is closer to sweet spot → _fail_q = old _best_q, _best_q = new."""
        s = self._make()
        # First fail at 25 (vmaf=80, score≈-0.75)
        s.record(Decimal("25"), {"vmaf_min": 80.0})
        # Second fail at 15 (vmaf=88, score≈-0.35 — closer to sweet spot)
        s.record(Decimal("15"), {"vmaf_min": 88.0})
        assert s._worse_q == Decimal("15")
        assert s._middle_q == Decimal("25")   # old _best_q lagged here
        assert s.best_quality == Decimal("15")

    def test_all_failing_sweet_spot_passed_transitions_to_3point(self) -> None:
        """Two fails: second is worse → _pass_q = second quality, _pass_metrics set."""
        s = self._make()
        # First fail at 15 (vmaf=88, closer to sweet spot)
        s.record(Decimal("15"), {"vmaf_min": 88.0})
        # Second fail at 25 (vmaf=80, worse) → sweet spot passed → _pass_q = 25
        s.record(Decimal("25"), {"vmaf_min": 80.0})
        # _pass_q should now be 25 (the worse attempt triggers transition)
        assert s._better_q == Decimal("25")
        assert s._better_stats == {"vmaf_min": 80.0}
        assert s._worse_q == Decimal("15")
        # _pass_metrics is set; _fail_metrics may still be None (only one new-best seen)
        assert s._better_stats is not None

    # ------------------------------------------------------------------
    # All-passing phase
    # ------------------------------------------------------------------

    def test_all_passing_first_attempt_becomes_best(self) -> None:
        """First pass: _best_q = quality, _pass_q = quality_better (unchanged sentinel)."""
        s = self._make()
        s.record(Decimal("18"), self._PASS_M)
        assert s._worse_q == Decimal("18")
        assert s._better_q == self._BETTER   # sentinel unchanged
        assert s.best_quality == Decimal("18")
        assert s.best_targets_met is True

    def test_all_passing_new_best_updates_state(self) -> None:
        """Two passes: second is closer to sweet spot → _pass_q = old _best_q, _best_q = new."""
        s = self._make()
        # First pass at 18 (vmaf=96, score≈0.05)
        s.record(Decimal("18"), {"vmaf_min": 96.0})
        # Second pass at 22 (vmaf=95.6, score≈0.03 — closer to sweet spot)
        s.record(Decimal("22"), {"vmaf_min": 95.6})
        assert s._worse_q == Decimal("22")
        assert s._better_q == Decimal("18")   # old _best_q lagged here
        assert s.best_quality == Decimal("22")
        assert s.best_targets_met is True

    def test_all_passing_sweet_spot_passed_transitions_to_3point(self) -> None:
        """Two passes: second is worse → _fail_q = second quality, _fail_metrics set."""
        s = self._make()
        # First pass at 22 (vmaf=95.6, closer to sweet spot)
        s.record(Decimal("22"), {"vmaf_min": 95.6})
        # Second pass at 18 (vmaf=96.0, worse — further from sweet spot)
        s.record(Decimal("18"), {"vmaf_min": 96.0})
        # _fail_q should now be 18 (the worse attempt triggers transition)
        assert s._middle_q == Decimal("18")
        assert s._middle_stats == {"vmaf_min": 96.0}
        assert s._worse_q == Decimal("22")
        # _fail_metrics is set; _pass_metrics may still be None (only one new-best seen)
        assert s._middle_stats is not None

    # ------------------------------------------------------------------
    # 3-point mode helpers
    # ------------------------------------------------------------------

    def _setup_3point(self) -> QualitySearchV2:
        """Set up 3-point mode via three all-failing calls.

        Call 1: fail at 25 (vmaf=80) → first best, _fail_q=51(sentinel), _fail_metrics=None
        Call 2: fail at 15 (vmaf=88) → new best, _fail_q=25, _fail_metrics={vmaf=80}, _best_q=15
        Call 3: fail at 20 (vmaf=82) → worse than best → _pass_q=20, _pass_metrics={vmaf=82}

        Final state: _pass_q=20, _best_q=15, _fail_q=25
        Range A = |15-20| = 5, Range B = |25-15| = 10
        """
        s = self._make()
        s.record(Decimal("25"), {"vmaf_min": 80.0})   # first best
        s.record(Decimal("15"), {"vmaf_min": 88.0})   # new best → _fail_q=25
        s.record(Decimal("20"), {"vmaf_min": 82.0})   # worse → _pass_q=20
        # Verify 3-point mode is active.
        assert s._better_stats is not None, "Expected _pass_metrics to be set"
        assert s._middle_stats is not None, "Expected _fail_metrics to be set"
        assert s._better_q == Decimal("20")
        assert s._worse_q == Decimal("15")
        assert s._middle_q == Decimal("25")
        return s

    # ------------------------------------------------------------------
    # Phase 2 (3-point mode) tests
    # ------------------------------------------------------------------

    def test_phase2_range_b_new_best_promotes(self) -> None:
        """In 3-point mode, Range B attempt with better score: _pass_q = old _best_q."""
        s = self._setup_3point()
        # State: _pass_q=20, _best_q=15, _fail_q=25
        # Range B is [_best_q=15 ... _fail_q=25]; quality=22 is in Range B.
        # vmaf=92 → score closer to 0 than vmaf=88 (current best) → new best
        old_best_q = s._worse_q   # 15
        s.record(Decimal("22"), {"vmaf_min": 92.0})
        assert s._better_q == old_best_q   # old _best_q promoted to _pass_q
        assert s._worse_q == Decimal("22")

    def test_phase2_range_a_new_best_demotes(self) -> None:
        """In 3-point mode, Range A attempt with better score: _fail_q = old _best_q."""
        s = self._setup_3point()
        # State: _pass_q=20, _best_q=15, _fail_q=25
        # Range A is (_best_q=15 ... _pass_q=20) exclusive of _best_q; quality=18 is in Range A.
        # vmaf=92 → score closer to 0 than vmaf=88 → new best
        old_best_q = s._worse_q   # 15
        s.record(Decimal("18"), {"vmaf_min": 92.0})
        assert s._middle_q == old_best_q   # old _best_q demoted to _fail_q
        assert s._worse_q == Decimal("18")

    def test_phase2_range_b_tighten(self) -> None:
        """In 3-point mode, Range B attempt with worse score: _fail_q = quality."""
        s = self._setup_3point()
        # State: _pass_q=20, _best_q=15, _fail_q=25
        # Range B: quality=23 (vmaf=79, worse than vmaf=88) → tighten _fail_q
        s.record(Decimal("23"), {"vmaf_min": 79.0})
        assert s._middle_q == Decimal("23")
        assert s._worse_q == Decimal("15")   # unchanged

    def test_phase2_range_a_tighten(self) -> None:
        """In 3-point mode, Range A attempt with worse score: _pass_q = quality."""
        s = self._setup_3point()
        # State: _pass_q=20, _best_q=15, _fail_q=25
        # Range A: quality=18 (vmaf=79, worse than vmaf=88) → tighten _pass_q
        s.record(Decimal("18"), {"vmaf_min": 79.0})
        assert s._better_q == Decimal("18")
        assert s._worse_q == Decimal("15")   # unchanged

    # ------------------------------------------------------------------
    # Early acceptance and exhaustion
    # ------------------------------------------------------------------

    def test_early_acceptance_sets_exhausted(self) -> None:
        """score == 0.0 → _exhausted = True, returns None, best_targets_met = True."""
        s = self._make()
        result = s.record(Decimal("20"), self._early_m())
        assert result is None
        assert s._exhausted is True
        assert s.best_targets_met is True
        assert s.best_quality == Decimal("20")

    def test_exhaustion_both_ranges_le_granularity(self) -> None:
        """When both ranges collapse to <= granularity, returns None."""
        # Use a very tight range so it exhausts quickly.
        s = QualitySearchV2(
            quality_better  = Decimal("19"),
            quality_worse   = Decimal("21"),
            quality_targets = self._TARGET,
            granularity     = self._GRAN,
        )
        # Drive it to exhaustion by recording attempts.
        current_q = Decimal("20")
        for _ in range(20):
            result = s.record(current_q, self._FAIL_M)
            if result is None:
                break
            current_q = result
        assert s._exhausted is True

    def test_subsequent_calls_after_exhaustion_return_none(self) -> None:
        """After exhaustion, all subsequent record() calls return None."""
        s = self._make()
        # Early acceptance exhausts immediately.
        s.record(Decimal("20"), self._early_m())
        assert s.record(Decimal("20"), self._PASS_M) is None
        assert s.record(Decimal("25"), self._FAIL_M) is None

    # ------------------------------------------------------------------
    # Constructor validation
    # ------------------------------------------------------------------

    def test_raises_if_better_equals_worse(self) -> None:
        """ValueError when quality_better == quality_worse."""
        with pytest.raises(ValueError):
            QualitySearchV2(
                quality_better  = Decimal("18"),
                quality_worse   = Decimal("18"),
                quality_targets = self._TARGET,
                granularity     = self._GRAN,
            )

    def test_raises_if_granularity_zero(self) -> None:
        """ValueError when granularity <= 0."""
        with pytest.raises(ValueError):
            QualitySearchV2(
                quality_better  = Decimal("0"),
                quality_worse   = Decimal("51"),
                quality_targets = self._TARGET,
                granularity     = Decimal("0"),
            )


class TestEncodeChunkIntegration:
    """Unit tests for encode_chunk QualitySearchV2 integration.
    Validates: Requirements 8.1–8.3, 7.5–7.6
    """

    def test_qualitysearchv2_imported_in_encoding(self) -> None:
        """QualitySearchV2 is importable from pyqenc.phases.encoding (verifies the import exists)."""
        import importlib

        import pyqenc.phases.encoding as enc_mod

        # Verify QualitySearchV2 is accessible via the module's quality import
        from pyqenc.quality import QualitySearchV2
        assert QualitySearchV2 is not None
        # Verify encoding module uses QualitySearchV2 (it's referenced in encode_chunk source)
        import inspect
        src = inspect.getsource(enc_mod.ChunkEncoder.encode_chunk)
        assert "QualitySearchV2" in src, "encode_chunk must instantiate QualitySearchV2"

    def test_qualitysearch_usable_as_drop_in(self) -> None:
        """QualitySearch satisfies QualitySearchProtocol and can be used where V2 is expected."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearch, QualitySearchProtocol

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearch(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        # Verify all protocol properties exist and have correct types.
        assert isinstance(s.attempts, int)
        assert s.best_quality is None
        assert s.best_metrics is None
        assert s.best_targets_met is False
        result = s.record(Decimal("25"), {"vmaf_min": 90.0})
        assert result is None or isinstance(result, Decimal)
        # After one record call, attempts == 1 and best_quality is set.
        assert s.attempts == 1
        assert s.best_quality is not None

    def test_qualitysearch_protocol_structural_compatibility(self) -> None:
        """QualitySearch is structurally compatible with QualitySearchProtocol (runtime check)."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearch, QualitySearchProtocol

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearch(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        # Protocol is runtime_checkable — verify isinstance works.
        assert isinstance(s, QualitySearchProtocol)

    def test_qualitysearchv2_protocol_structural_compatibility(self) -> None:
        """QualitySearchV2 is structurally compatible with QualitySearchProtocol."""
        from pyqenc.models import QualityTarget
        from pyqenc.quality import QualitySearchProtocol, QualitySearchV2

        target = QualityTarget(metric="vmaf", statistic="min", value=95.0)
        s = QualitySearchV2(
            quality_better  = Decimal("0"),
            quality_worse   = Decimal("51"),
            quality_targets = [target],
            granularity     = Decimal("0.5"),
        )
        assert isinstance(s, QualitySearchProtocol)
