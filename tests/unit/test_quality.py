"""Unit tests for quality evaluation and CRF adjustment."""

import pytest

from pyqenc.constants import CRF_GRANULARITY
from pyqenc.models import QualityTarget
from pyqenc.quality import (
    CRFHistory,
    MetricType,
    adjust_crf,
    normalize_metric_deficit,
)


class TestNormalizeMetricDeficit:
    """Tests for metric deficit normalization."""

    def test_normalize_ssim(self):
        """Test SSIM deficit with pre-normalized values (0–100 scale)."""
        # Below target
        deficit = normalize_metric_deficit(MetricType.SSIM, 95.0, 98.0)
        assert deficit == pytest.approx(-3.0)

        # Above target
        deficit = normalize_metric_deficit(MetricType.SSIM, 99.0, 98.0)
        assert deficit == pytest.approx(1.0)

    def test_normalize_psnr(self):
        """Test PSNR deficit with pre-normalized values (0–100 scale)."""
        # Below target
        deficit = normalize_metric_deficit(MetricType.PSNR, 40.0, 42.0)
        assert deficit == pytest.approx(-2.0)

        # Above target
        deficit = normalize_metric_deficit(MetricType.PSNR, 45.0, 42.0)
        assert deficit == pytest.approx(3.0)

    def test_normalize_vmaf(self):
        """Test VMAF deficit with pre-normalized values (0–100 scale)."""
        # Below target
        deficit = normalize_metric_deficit(MetricType.VMAF, 93.0, 95.0)
        assert deficit == pytest.approx(-2.0)

        # Above target
        deficit = normalize_metric_deficit(MetricType.VMAF, 97.0, 95.0)
        assert deficit == pytest.approx(2.0)

    def test_normalize_unknown_metric(self):
        """Test that normalize_metric_deficit works for all MetricType values."""
        # All valid metric types should work without error
        for metric_type in MetricType:
            result = normalize_metric_deficit(metric_type, 90.0, 85.0)
            assert result == pytest.approx(5.0)


class TestCRFHistory:
    """Tests for CRF history tracking."""

    def test_add_attempt(self):
        """Test adding encoding attempts."""
        history = CRFHistory()

        history.add_attempt(20.0, {"vmaf_min": 93.2, "vmaf_med": 96.1})
        history.add_attempt(18.0, {"vmaf_min": 95.3, "vmaf_med": 97.8})

        assert len(history.attempts) == 2
        assert history.attempts[0][0] == 20.0
        assert history.attempts[1][0] == 18.0

    def test_has_attempted(self):
        """Test checking if CRF has been attempted."""
        history = CRFHistory()
        history.add_attempt(20.0, {"vmaf_min": 93.2})
        history.add_attempt(18.0, {"vmaf_min": 95.3})

        assert history.has_attempted(20.0)
        assert history.has_attempted(18.0)
        assert history.has_attempted(20.05)  # Within tolerance
        assert not history.has_attempted(22.0)

    def test_get_bounds_no_attempts(self):
        """Test getting bounds with no attempts."""
        history = CRFHistory()
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]

        too_low, too_high = history.get_bounds(targets)
        assert too_low is None
        assert too_high is None

    def test_get_bounds_with_attempts(self):
        """Test getting bounds from attempt history."""
        history = CRFHistory()
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]

        # Failed attempt (quality too low)
        history.add_attempt(20.0, {"vmaf_min": 93.2})

        # Successful attempt (quality met)
        history.add_attempt(18.0, {"vmaf_min": 95.3})

        too_low, too_high = history.get_bounds(targets)
        assert too_low == 20.0  # Highest CRF where quality was below target
        assert too_high == 18.0  # Lowest CRF where quality was above target


class TestAdjustCRF:
    """Tests for CRF adjustment algorithm."""

    def test_adjust_crf_targets_met(self):
        """Test adjustment when targets are met tries to squeeze to higher CRF."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 96.0}
        history = CRFHistory()

        next_crf = adjust_crf(18.0, results, targets, history)
        # Should try a higher CRF (larger = smaller file) since no bounds known yet
        assert next_crf is not None
        assert next_crf > 18.0

    def test_adjust_crf_large_deficit(self):
        """Test adjustment with large quality deficit."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 80.0}  # 15 point deficit
        history = CRFHistory()
        history.add_attempt(20.0, results)

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        assert next_crf < 20.0  # Should decrease CRF (increase quality)

    def test_adjust_crf_small_deficit(self):
        """Test adjustment with small quality deficit."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 93.5}  # 1.5 point deficit
        history = CRFHistory()
        history.add_attempt(20.0, results)

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        assert next_crf < 20.0  # Should decrease CRF

    def test_adjust_crf_quality_above_target(self):
        """Test adjustment when quality significantly exceeds target."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 98.0}  # 3 points above
        history = CRFHistory()
        history.add_attempt(18.0, results)

        next_crf = adjust_crf(18.0, results, targets, history)
        # Should try a higher CRF since targets are met and no failing bound known
        assert next_crf is not None
        assert next_crf > 18.0

    def test_adjust_crf_with_bounds(self):
        """Test adjustment respects history bounds."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        history = CRFHistory()

        # Establish bounds
        history.add_attempt(20.0, {"vmaf_min": 93.0})  # Too low
        history.add_attempt(18.0, {"vmaf_min": 96.0})  # Success

        # Try to adjust from middle
        results = {"vmaf_min": 94.0}  # Still below target
        next_crf = adjust_crf(19.0, results, targets, history)

        assert next_crf is not None
        # Should use binary search between bounds
        assert 18.0 < next_crf < 20.0

    def test_adjust_crf_already_attempted(self):
        """Test adjustment avoids already attempted CRF values."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        history = CRFHistory()

        # Add several attempts
        history.add_attempt(20.0, {"vmaf_min": 93.0})
        history.add_attempt(18.0, {"vmaf_min": 94.5})
        history.add_attempt(17.0, {"vmaf_min": 95.5})  # Success

        # Current attempt
        results = {"vmaf_min": 94.5}
        next_crf = adjust_crf(18.0, results, targets, history)

        # Should not return already attempted values
        assert next_crf is not None
        assert not history.has_attempted(next_crf)

    def test_adjust_crf_multiple_targets(self):
        """Test adjustment with multiple quality targets."""
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="ssim", statistic="median", value=98.0),
        ]
        results = {
            "vmaf_min":    96.0,  # Meets target
            "ssim_median": 96.0,  # Below target (pre-normalized 0–100 scale)
        }
        history = CRFHistory()
        history.add_attempt(20.0, results)

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        assert next_crf < 20.0  # Should decrease to improve SSIM

    def test_adjust_crf_granularity(self):
        """Test CRF adjustment respects CRF_GRANULARITY granularity."""
        targets = [QualityTarget(metric="vmaf", statistic="min", value=95.0)]
        results = {"vmaf_min": 94.0}
        history = CRFHistory()
        history.add_attempt(20.0, results)

        next_crf = adjust_crf(20.0, results, targets, history)
        assert next_crf is not None
        # Should be multiple of CRF_GRANULARITY
        assert (next_crf / CRF_GRANULARITY) % 1 == 0
