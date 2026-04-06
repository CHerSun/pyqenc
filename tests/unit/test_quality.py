"""Unit tests for quality evaluation and CRF adjustment."""

from decimal import Decimal

import pytest

from pyqenc.models import QualityTarget
from pyqenc.quality import CRFHistory, adjust_crf

_CRF_MIN    = Decimal("1")
_CRF_MAX    = Decimal("51")
_GRAN       = Decimal("0.5")   # default granularity used in tests
_GRAN_INT   = Decimal("1")     # integer-step granularity (QP-style)


def _d(v: str | int | float) -> Decimal:
    """Shorthand: convert to Decimal via str to avoid float imprecision."""
    return Decimal(str(v))


def _history(*attempts: tuple[float | str, bool]) -> CRFHistory:
    """Build a CRFHistory from (crf, passed) pairs."""
    h = CRFHistory(fail_crf=_CRF_MAX, pass_crf=_CRF_MIN)
    for crf, passed in attempts:
        h.add(_d(crf), passed)
    return h


class TestCRFHistory:
    """Tests for CRF history tracking."""

    def test_initial_sentinels(self):
        """Bounds start at codec limits when no attempts recorded."""
        h = CRFHistory(fail_crf=_d(51), pass_crf=_d(1))
        assert h.fail_crf == _d(51)
        assert h.pass_crf == _d(1)
        assert h.attempts == 0

    def test_add_pass_narrows_pass_bound(self):
        """A passing attempt raises the pass bound."""
        h = _history(("18.0", True))
        assert h.pass_crf == _d("18.0")
        assert h.attempts == 1

    def test_add_fail_narrows_fail_bound(self):
        """A failing attempt lowers the fail bound."""
        h = _history(("20.0", False))
        assert h.fail_crf == _d("20.0")

    def test_tightest_bounds_kept(self):
        """Only the tightest pass and fail bounds are retained."""
        h = _history(("22.0", False), ("20.0", False), ("16.0", True), ("18.0", True))
        assert h.fail_crf == _d("20.0")   # lowest failing
        assert h.pass_crf == _d("18.0")   # highest passing

    def test_attempt_count(self):
        """attempt_count tracks every recorded attempt."""
        h = _history(("20.0", False), ("18.0", True), ("19.0", False))
        assert h.attempts == 3


class TestAdjustCRF:
    """Tests for CRF adjustment algorithm."""

    def test_targets_met_moves_crf_up(self):
        """When targets are met, next CRF should be higher (less quality, smaller file)."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 96.0}
        history = _history(("18.0", True))

        next_crf = adjust_crf(_d("18.0"), results, targets, history, granularity=_GRAN)
        assert next_crf is not None
        assert next_crf > _d("18.0")

    def test_large_deficit_moves_crf_down(self):
        """A large quality deficit should push CRF down significantly."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 80.0}
        history = _history(("20.0", False))

        next_crf = adjust_crf(_d("20.0"), results, targets, history, granularity=_GRAN)
        assert next_crf is not None
        assert next_crf < _d("20.0")

    def test_small_deficit_moves_crf_down(self):
        """A small quality deficit should still push CRF down."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 93.5}
        history = _history(("20.0", False))

        next_crf = adjust_crf(_d("20.0"), results, targets, history, granularity=_GRAN)
        assert next_crf is not None
        assert next_crf < _d("20.0")

    def test_interpolates_within_bracket(self):
        """With both bounds known, next CRF must lie strictly inside the bracket."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        history = _history(("20.0", False), ("18.0", True))

        results = {"vmaf_min": 94.0}
        next_crf = adjust_crf(_d("19.0"), results, targets, history, granularity=_GRAN)

        assert next_crf is not None
        assert _d("18.0") < next_crf < _d("20.0")

    def test_exhausted_bracket_returns_none(self):
        """When fail and pass bounds are within granularity, return None."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        h = CRFHistory(fail_crf=_d("19.0") + _GRAN, pass_crf=_d("19.0"))

        results = {"vmaf_min": 94.0}
        assert adjust_crf(_d("19.0"), results, targets, h, granularity=_GRAN) is None

    def test_multiple_targets_worst_drives_adjustment(self):
        """The worst-performing target drives the CRF direction."""
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="ssim", statistic="median", value=98.0),
        ]
        results = {
            "vmaf_min":    96.0,   # passes
            "ssim_median": 96.0,   # fails
        }
        history = _history(("20.0", False))

        next_crf = adjust_crf(_d("20.0"), results, targets, history, granularity=_GRAN)
        assert next_crf is not None
        assert next_crf < _d("20.0")

    def test_result_is_multiple_of_granularity(self):
        """Returned value must always be a multiple of granularity."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 94.0}
        history = _history(("20.0", False))

        next_crf = adjust_crf(_d("20.0"), results, targets, history, granularity=_GRAN)
        assert next_crf is not None
        assert next_crf % _GRAN == 0

    def test_result_never_equals_boundary(self):
        """Returned CRF must never equal pass_crf or fail_crf — always strictly interior."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        for vmaf in [94.0, 95.5, 90.0, 99.0]:
            h = _history(("20.0", False), ("10.0", True))
            result = adjust_crf(_d("15.0"), {"vmaf_min": vmaf}, targets, h, granularity=_GRAN)
            if result is not None:
                assert result > h.pass_crf, f"result {result} ≤ pass_crf {h.pass_crf}"
                assert result < h.fail_crf, f"result {result} ≥ fail_crf {h.fail_crf}"

    def test_exhausted_when_only_one_interior_step_possible(self):
        """When bracket is exactly 2×granularity, one interior step exists."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        h = CRFHistory(fail_crf=_d("20.0"), pass_crf=_d("20.0") - 2 * _GRAN)
        result = adjust_crf(_d("19.0"), {"vmaf_min": 94.0}, targets, h, granularity=_GRAN)
        if result is not None:
            assert result > h.pass_crf
            assert result < h.fail_crf

    def test_boundary_without_metrics_is_reachable(self):
        """When a boundary sentinel has no real metrics, the boundary value itself is a valid candidate."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        h = CRFHistory(fail_crf=_d("51"), pass_crf=_d("1"))
        result = adjust_crf(_d("26"), {"vmaf_min": 94.0}, targets, h, granularity=_GRAN)
        assert result is not None

    def test_integer_granularity_qp_style(self):
        """With granularity=1 (QP-style), result must be an integer Decimal with no decimal places."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        h = CRFHistory(fail_crf=_d("60"), pass_crf=_d("30"))
        result = adjust_crf(_d("45"), {"vmaf_min": 94.0}, targets, h, granularity=_GRAN_INT)
        assert result is not None
        assert result == result.to_integral_value(), f"Expected integer result, got {result}"
        assert result < _d("45")
        # str() should produce no decimal point for integer granularity
        assert "." not in str(result), f"Expected no decimal point in '{result}'"

    def test_str_representation_crf_style(self):
        """With granularity=0.5, str() produces exactly one decimal place."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        h = CRFHistory(fail_crf=_d("51"), pass_crf=_d("1"))
        result = adjust_crf(_d("26"), {"vmaf_min": 94.0}, targets, h, granularity=_GRAN)
        assert result is not None
        s = str(result)
        assert "." in s, f"Expected decimal point in '{s}'"
        assert len(s.split(".")[1]) == 1, f"Expected exactly 1 decimal place in '{s}'"
