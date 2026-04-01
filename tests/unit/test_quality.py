"""Unit tests for quality evaluation and CRF adjustment."""

import pytest

from pyqenc.constants import CRF_GRANULARITY
from pyqenc.models import QualityTarget
from pyqenc.quality import CRFHistory, adjust_crf

_CRF_MIN = 1.0
_CRF_MAX = 51.0


def _history(*attempts: tuple[float, bool]) -> CRFHistory:
    """Build a CRFHistory from (crf, passed) pairs."""
    h = CRFHistory(fail_crf=_CRF_MAX, pass_crf=_CRF_MIN)
    for crf, passed in attempts:
        h.add(crf, passed)
    return h


class TestCRFHistory:
    """Tests for CRF history tracking."""

    def test_initial_sentinels(self):
        """Bounds start at codec limits when no attempts recorded."""
        h = CRFHistory(fail_crf=51.0, pass_crf=1.0)
        assert h.fail_crf == 51.0
        assert h.pass_crf == 1.0
        assert h.attempts == 0

    def test_add_pass_narrows_pass_bound(self):
        """A passing attempt raises the pass bound."""
        h = _history((18.0, True))
        assert h.pass_crf == 18.0
        assert h.attempts == 1

    def test_add_fail_narrows_fail_bound(self):
        """A failing attempt lowers the fail bound."""
        h = _history((20.0, False))
        assert h.fail_crf == 20.0

    def test_tightest_bounds_kept(self):
        """Only the tightest pass and fail bounds are retained."""
        h = _history((22.0, False), (20.0, False), (16.0, True), (18.0, True))
        assert h.fail_crf == 20.0   # lowest failing
        assert h.pass_crf == 18.0   # highest passing

    def test_attempt_count(self):
        """attempt_count tracks every recorded attempt."""
        h = _history((20.0, False), (18.0, True), (19.0, False))
        assert h.attempts == 3


class TestAdjustCRF:
    """Tests for CRF adjustment algorithm."""

    def test_targets_met_moves_crf_up(self):
        """When targets are met, next CRF should be higher (less quality, smaller file)."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 96.0}
        history = _history((18.0, True))

        next_crf = adjust_crf(18.0, results, targets, history)
        assert next_crf is not None
        assert next_crf > 18.0

    def test_large_deficit_moves_crf_down(self):
        """A large quality deficit should push CRF down significantly."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 80.0}
        history = _history((20.0, False))

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        assert next_crf < 20.0

    def test_small_deficit_moves_crf_down(self):
        """A small quality deficit should still push CRF down."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 93.5}
        history = _history((20.0, False))

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        assert next_crf < 20.0

    def test_interpolates_within_bracket(self):
        """With both bounds known, next CRF must lie strictly inside the bracket."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        history = _history((20.0, False), (18.0, True))

        results = {"vmaf_min": 94.0}
        next_crf = adjust_crf(19.0, results, targets, history)

        assert next_crf is not None
        assert 18.0 < next_crf < 20.0

    def test_exhausted_bracket_returns_none(self):
        """When fail and pass bounds are within CRF_GRANULARITY, return None."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        h = CRFHistory(fail_crf=19.0 + CRF_GRANULARITY, pass_crf=19.0)

        results = {"vmaf_min": 94.0}
        assert adjust_crf(19.0, results, targets, h) is None

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
        history = _history((20.0, False))

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        assert next_crf < 20.0

    def test_result_is_multiple_of_granularity(self):
        """Returned CRF must always be a multiple of CRF_GRANULARITY."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 94.0}
        history = _history((20.0, False))

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        assert round(next_crf / CRF_GRANULARITY, 10) % 1 == 0
