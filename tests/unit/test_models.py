"""Unit tests for core data models."""

from pathlib import Path

import pytest
from pyqenc.models import (
    CodecConfig,
    CropParams,
    QualityTarget,
    StrategyConfig,
)


class TestQualityTarget:
    """Tests for QualityTarget parsing and validation."""

    def test_parse_valid_vmaf_min(self):
        """Test parsing valid VMAF minimum target."""
        target = QualityTarget.parse("vmaf-min:95")
        assert target.metric == "vmaf"
        assert target.statistic == "min"
        assert target.value == 95.0

    def test_parse_valid_ssim_median(self):
        """Test parsing valid SSIM median target."""
        target = QualityTarget.parse("ssim-med:0.98")
        assert target.metric == "ssim"
        assert target.statistic == "median"
        assert target.value == 0.98

    def test_parse_valid_psnr_max(self):
        """Test parsing valid PSNR maximum target."""
        target = QualityTarget.parse("psnr-max:45.5")
        assert target.metric == "psnr"
        assert target.statistic == "max"
        assert target.value == 45.5

    def test_parse_invalid_format(self):
        """Test parsing invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid quality target format"):
            QualityTarget.parse("invalid")

    def test_parse_invalid_metric(self):
        """Test parsing invalid metric type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid quality target format"):
            QualityTarget.parse("invalid-min:95")

    def test_parse_invalid_statistic(self):
        """Test parsing invalid statistic raises ValueError."""
        with pytest.raises(ValueError, match="Invalid quality target format"):
            QualityTarget.parse("vmaf-invalid:95")


class TestCropParams:
    """Tests for CropParams parsing and conversion."""

    def test_parse_two_values(self):
        """Test parsing crop with 2 values (top/bottom only)."""
        crop = CropParams.parse("140 140")
        assert crop.top == 140
        assert crop.bottom == 140
        assert crop.left == 0
        assert crop.right == 0

    def test_parse_four_values(self):
        """Test parsing crop with 4 values (all sides)."""
        crop = CropParams.parse("140 140 10 10")
        assert crop.top == 140
        assert crop.bottom == 140
        assert crop.left == 10
        assert crop.right == 10

    def test_parse_invalid_count(self):
        """Test parsing invalid number of values raises ValueError."""
        with pytest.raises(ValueError, match="Invalid crop format"):
            CropParams.parse("140")

        with pytest.raises(ValueError, match="Invalid crop format"):
            CropParams.parse("140 140 10")

    def test_is_empty(self):
        """Test is_empty detection."""
        assert CropParams().is_empty()
        assert CropParams(top=0, bottom=0, left=0, right=0).is_empty()
        assert not CropParams(top=140, bottom=140, left=0, right=0).is_empty()
        assert not CropParams(top=0, bottom=0, left=10, right=10).is_empty()

    def test_to_ffmpeg_filter(self):
        """Test FFmpeg filter generation."""
        crop = CropParams(top=140, bottom=140, left=10, right=10)
        filter_str = crop.to_ffmpeg_filter()
        assert filter_str == "crop=iw-20:ih-280:10:140"

    def test_str_representation(self):
        """Test string representation."""
        crop = CropParams(top=140, bottom=140, left=10, right=10)
        assert str(crop) == "140 140 10 10"


class TestStrategyConfig:
    """Tests for StrategyConfig FFmpeg argument generation."""

    def test_to_ffmpeg_args(self):
        """Test FFmpeg argument generation."""
        codec = CodecConfig(
            name="h265-10bit",
            encoder="libx265",
            pixel_format="yuv420p10le",
            default_crf=20.0,
            crf_range=(0.0, 51.0)
        )

        strategy = StrategyConfig(
            preset="slow",
            profile="h265-aq",
            codec=codec,
            profile_args=["-x265-params", "aq-mode=3:aq-strength=0.8"]
        )

        args = strategy.to_ffmpeg_args(crf=18.5)

        assert args == [
            "-c:v", "libx265",
            "-preset", "slow",
            "-crf", "18.5",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "aq-mode=3:aq-strength=0.8"
        ]
