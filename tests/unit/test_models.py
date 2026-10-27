"""Unit tests for core data models."""


from decimal import Decimal

import pytest

from pyqenc.models import (
    CodecConfig,
    CropParams,
    QualityTarget,
    Resolution,
    Strategy,
)
from pyqenc.quality_metrics import MetricStatKey


class TestQualityTarget:
    """Tests for QualityTarget parsing and validation."""

    def test_parse_valid_vmaf_min(self):
        """Test parsing valid VMAF minimum target."""
        target = QualityTarget.parse("vmaf-min:95")
        assert target.identity == MetricStatKey.parse("vmaf-min")
        assert target.value == 95.0

    def test_parse_valid_ssim_median(self):
        """Test parsing valid SSIM median target."""
        target = QualityTarget.parse("ssim-med:0.98")
        assert target.identity == MetricStatKey.parse("ssim-median")
        assert target.value == 0.98

    def test_parse_valid_psnr_max(self):
        """Test parsing valid PSNR maximum target."""
        target = QualityTarget.parse("psnr-max:45.5")
        assert target.identity == MetricStatKey.parse("psnr-max")
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
        """Test parsing crop with 2 comma-separated values (top/bottom only)."""
        crop = CropParams.parse("140,140")
        assert crop.top == 140
        assert crop.bottom == 140
        assert crop.left == 0
        assert crop.right == 0

    def test_parse_four_values(self):
        """Test parsing crop with 4 comma-separated values (all sides)."""
        crop = CropParams.parse("140,140,10,10")
        assert crop.top == 140
        assert crop.bottom == 140
        assert crop.left == 10
        assert crop.right == 10

    def test_parse_invalid_count(self):
        """Test parsing invalid number of values raises ValueError."""
        with pytest.raises(ValueError, match="Invalid crop format"):
            CropParams.parse("140")

        with pytest.raises(ValueError, match="Invalid crop format"):
            CropParams.parse("140,140,10")

    def test_parse_old_space_format_raises(self):
        """Old space-separated format must no longer be accepted."""
        with pytest.raises(ValueError, match="Invalid crop format"):
            CropParams.parse("140 140")

        with pytest.raises(ValueError, match="Invalid crop format"):
            CropParams.parse("140 140 10 10")

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
        """Test string representation uses comma-separated format."""
        crop = CropParams(top=140, bottom=140, left=10, right=10)
        assert str(crop) == "140,140,10,10"


class TestResolution:
    """Tests for the Resolution value type (the derivable dimensions)."""

    def test_parse_valid(self):
        """Test parsing a 'WxH' string."""
        res = Resolution.parse("1920x1080")
        assert res.width == 1920
        assert res.height == 1080

    def test_parse_rejects_missing_separator(self):
        """A string without 'x' is not a resolution."""
        with pytest.raises(ValueError, match="Not a resolution string"):
            Resolution.parse("19201080")

    def test_parse_rejects_non_integers(self):
        """Non-integer parts are not resolutions."""
        with pytest.raises(ValueError, match="Not a resolution string"):
            Resolution.parse("1920x1080.5")

    def test_parse_rejects_non_positive(self):
        """Zero/negative dimensions are not resolutions (positive by meaning)."""
        for bad in ("0x1080", "1920x0", "-10x100"):
            with pytest.raises(ValueError, match="Not a resolution string"):
                Resolution.parse(bad)

    def test_subtraction_applies_crop_margins(self):
        """source - crop subtracts margins per axis (the live-found case)."""
        source = Resolution.parse("1920x1080")
        crop   = CropParams(top=22, bottom=22, left=0, right=0)
        assert str(source - crop) == "1920x1036"

    def test_subtraction_all_sides(self):
        """All four margins subtract from their corresponding axis."""
        source = Resolution.parse("3840x2160")
        crop   = CropParams(top=140, bottom=140, left=10, right=30)
        result = source - crop
        assert (result.width, result.height) == (3800, 1880)

    def test_subtraction_empty_crop_is_identity(self):
        """An empty crop leaves the dimensions unchanged."""
        source = Resolution.parse("1280x720")
        assert str(source - CropParams()) == "1280x720"

    def test_str_form(self):
        """The string form is the stream-info 'WxH' convention."""
        assert str(Resolution(width=640, height=360)) == "640x360"

    def test_frozen(self):
        """Resolution is immutable (a value type)."""
        from pydantic import ValidationError

        res = Resolution.parse("1920x1080")
        with pytest.raises(ValidationError):
            res.width = 3840  # ty: ignore[invalid-assignment] — rejection is the test's subject


class TestStrategy:
    """Tests for Strategy FFmpeg argument generation."""

    def test_to_ffmpeg_args(self):
        """Test FFmpeg argument generation via encoder_args template."""
        codec = CodecConfig(
            name            = "h265-10bit",
            default_quality = Decimal("20"),
            default_preset  = "slow",
            quality_label   = "CRF",
            quality_granularity = Decimal("0.5"),
            quality_range   = (Decimal("0"), Decimal("51")),
            pre_input_args  = [],
            encoder_args    = [
                "-c:v", "libx265",
                "-preset", "{preset}",
                "-crf", "{quality}",
                "-vf", "{vf}",
                "-pix_fmt", "yuv420p10le",
                "{profile_args}",
            ],
            presets         = ["slow"],
        )

        strategy = Strategy(
            preset       = "slow",
            profile      = "h265-aq",
            codec        = codec,
            profile_args = ["-x265-params", "aq-mode=3:aq-strength=0.8"],
        )

        # Without crop — -vf and {vf} both dropped
        args = strategy.to_output_args(Decimal("18.5"))
        assert args == [
            "-c:v", "libx265",
            "-preset", "slow",
            "-crf", "18.5",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "aq-mode=3:aq-strength=0.8",
        ]

        # With crop — -vf kept, {vf} replaced with filter
        args_crop = strategy.to_output_args(Decimal("18.5"), vf_filter="crop=1920:800:0:140")
        assert args_crop == [
            "-c:v", "libx265",
            "-preset", "slow",
            "-crf", "18.5",
            "-vf", "crop=1920:800:0:140",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "aq-mode=3:aq-strength=0.8",
        ]

    def test_to_ffmpeg_args_embedded_vf(self):
        """Test {vf} embedded inside a larger filter chain (nvenc-style)."""
        codec = CodecConfig(
            name            = "hevc-nvenc-10bit",
            default_quality = Decimal("28"),
            default_preset  = "p7",
            quality_label   = "CQ",
            quality_granularity = Decimal("0.5"),
            quality_range   = (Decimal("1"), Decimal("51")),
            pre_input_args  = ["-hwaccel", "cuda"],
            encoder_args    = [
                "-c:v", "hevc_nvenc",
                "-cq:v", "{quality}",
                "-vf", "scale_cuda=format=p010le:{vf}",
                "-pix_fmt", "p010le",
                "{profile_args}",
            ],
            presets         = ["p7"],
        )
        strategy = Strategy(preset="p7", profile="hevc-nvenc-hq", codec=codec, profile_args=["-tune:v", "hq"])

        # Without crop — trailing : left in place (ffmpeg tolerates it)
        args = strategy.to_output_args(Decimal("28.0"))
        vf_idx = args.index("-vf")
        assert args[vf_idx + 1] == "scale_cuda=format=p010le:"

        # With crop — filter appended after the colon
        args_crop = strategy.to_output_args(Decimal("28.0"), vf_filter="crop=1920:800:0:140")
        vf_idx = args_crop.index("-vf")
        assert args_crop[vf_idx + 1] == "scale_cuda=format=p010le:crop=1920:800:0:140"
