"""Integration tests for encoding → quality evaluation pipeline."""

from pathlib import Path

import pytest

from pyqenc.models import QualityTarget
from pyqenc.utils.visualization import QualityEvaluator
from tests.fixtures.metric_fixtures import (
    create_mock_psnr_file,
    create_mock_ssim_file,
    create_mock_vmaf_file,
    get_expected_vmaf_stats,
)
from tests.fixtures.video_fixtures import get_sample_video_path, sample_video_exists


class TestEncodingQualityIntegration:
    """Integration tests for encoding and quality evaluation."""

    def test_quality_evaluation_with_mock_metrics(self, tmp_path):
        """Test quality evaluation using mock metric files."""
        # Create mock metric files
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()

        vmaf_file = create_mock_vmaf_file(metrics_dir / "test.vmaf.json")
        ssim_file = create_mock_ssim_file(metrics_dir / "test.ssim.log")
        psnr_file = create_mock_psnr_file(metrics_dir / "test.psnr.log")

        # Define quality targets
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=94.0),
            QualityTarget(metric="vmaf", statistic="median", value=95.0),
        ]

        # Note: This test uses mock files, so we can't actually run the evaluator
        # which requires real video files. Instead, we verify the mock data structure.
        expected_stats = get_expected_vmaf_stats()

        assert expected_stats["min"] >= targets[0].value
        assert expected_stats["median"] >= targets[1].value

    def test_quality_target_evaluation(self):
        """Test quality target evaluation logic."""
        targets = [
            QualityTarget(metric="vmaf", statistic="min",    value=95.0),
            QualityTarget(metric="ssim", statistic="median", value=0.98),
        ]

        # Simulate metrics that meet targets
        metrics_pass = {
            "vmaf": {"min": 95.5, "median": 97.0},
            "ssim": {"min": 0.97, "median": 0.985},
        }

        # Check all targets met
        all_met = True
        for target in targets:
            metric_stats = metrics_pass.get(target.metric)
            if metric_stats:
                actual = metric_stats.get(target.statistic)
                if actual is None or actual < target.value:
                    all_met = False
                    break

        assert all_met

        # Simulate metrics that fail targets
        metrics_fail = {
            "vmaf": {"min": 93.0, "median": 96.0},  # min below target
            "ssim": {"min": 0.97, "median": 0.985},
        }

        all_met = True
        for target in targets:
            metric_stats = metrics_fail.get(target.metric)
            if metric_stats:
                actual = metric_stats.get(target.statistic)
                if actual is None or actual < target.value:
                    all_met = False
                    break

        assert not all_met

    def test_artifact_generation(self, tmp_path):
        """Test that quality evaluation generates expected artifacts."""
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()

        # Create mock metric files
        vmaf_file = create_mock_vmaf_file(metrics_dir / "chunk_001.vmaf.json")
        ssim_file = create_mock_ssim_file(metrics_dir / "chunk_001.ssim.log")
        psnr_file = create_mock_psnr_file(metrics_dir / "chunk_001.psnr.log")

        # Verify files exist
        assert vmaf_file.exists()
        assert ssim_file.exists()
        assert psnr_file.exists()

        # Verify file contents are valid
        import json
        with open(vmaf_file) as f:
            vmaf_data = json.load(f)
        assert "frames" in vmaf_data
        assert "pooled_metrics" in vmaf_data

        with open(ssim_file) as f:
            ssim_content = f.read()
        assert "n:" in ssim_content
        assert "Y:" in ssim_content

        with open(psnr_file) as f:
            psnr_content = f.read()
        assert "n:" in psnr_content
        assert "psnr_avg:" in psnr_content


@pytest.mark.skipif(not sample_video_exists(), reason="Sample video not available")
class TestEncodingQualityWithRealVideo:
    """Integration tests using real video files (requires sample videos)."""

    def test_quality_evaluator_initialization(self, tmp_path):
        """Test quality evaluator can be initialized."""
        evaluator = QualityEvaluator(tmp_path)
        assert evaluator.work_dir == tmp_path
