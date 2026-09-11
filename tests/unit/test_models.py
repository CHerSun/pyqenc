"""Unit tests for core data models."""

from fractions import Fraction
from pathlib import Path

import pytest
from pyqenc.models import (
    CodecConfig,
    CropParams,
    ExtendedVideoMetadata,
    QualityTarget,
    Strategy,
    VideoMetadata,
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


class TestExtendedVideoMetadataFromBase:
    """Tests for ExtendedVideoMetadata.from_base()."""

    def _make_populated_meta(self) -> VideoMetadata:
        """Return a VideoMetadata with all private fields populated."""
        meta = VideoMetadata(path=Path("/fake/source.mkv"))
        meta._duration_seconds = 5400.0
        meta._fps              = 24.0
        meta._fps_fraction     = Fraction(24, 1)
        meta._resolution       = "1920x1080"
        meta._pix_fmt          = "yuv420p10le"
        meta._file_size_bytes  = 12_345_678
        return meta

    def test_returns_extended_video_metadata_type(self):
        """from_base() must return an ExtendedVideoMetadata instance, not a plain VideoMetadata.

        Bug: if from_base() returned the wrong type, downstream frame_count accesses would fail.
        """
        meta = self._make_populated_meta()
        extended = ExtendedVideoMetadata.from_base(meta, frame_count=1440)
        assert isinstance(extended, ExtendedVideoMetadata)

    def test_frame_count_set_correctly(self):
        """from_base() must set frame_count to the supplied value.

        Bug: frame_count could be silently zeroed or omitted if from_base()
        didn't pass it through model_validate_full correctly.
        """
        meta = self._make_populated_meta()
        extended = ExtendedVideoMetadata.from_base(meta, frame_count=1440)
        assert extended.frame_count == 1440

    def test_frame_count_zero_sentinel(self):
        """from_base() with frame_count=0 must store 0 as the 'unknown' sentinel.

        Bug: a 0 sentinel could be overwritten by a default value or guard clause.
        """
        meta = self._make_populated_meta()
        extended = ExtendedVideoMetadata.from_base(meta, frame_count=0)
        assert extended.frame_count == 0

    def test_all_cached_private_fields_copied(self):
        """from_base() must copy all cached private fields from the base VideoMetadata.

        Bug: if from_base() didn't transfer private attrs, properties would
        re-trigger lazy probes and potentially lose data.
        """
        meta = self._make_populated_meta()
        extended = ExtendedVideoMetadata.from_base(meta, frame_count=1440)

        assert extended._duration_seconds == pytest.approx(5400.0)
        assert extended._fps              == pytest.approx(24.0)
        assert extended._fps_fraction     == Fraction(24, 1)
        assert extended._resolution       == "1920x1080"
        assert extended._pix_fmt          == "yuv420p10le"
        assert extended._file_size_bytes  == 12_345_678

    def test_path_preserved(self):
        """from_base() must preserve the source path.

        Bug: path could be lost during model_dump_full/model_validate_full round-trip.
        """
        meta = self._make_populated_meta()
        extended = ExtendedVideoMetadata.from_base(meta, frame_count=100)
        assert extended.path == Path("/fake/source.mkv")

    def test_no_probe_triggered_on_property_access(self):
        """Properties on the returned ExtendedVideoMetadata must not re-probe.

        Bug: if private fields weren't transferred, any property access would
        trigger a real ffprobe subprocess call.
        """
        from unittest.mock import patch

        meta = self._make_populated_meta()
        extended = ExtendedVideoMetadata.from_base(meta, frame_count=1440)

        with patch.object(extended, "_probe_metadata") as mock_probe:
            _ = extended.duration_seconds
            _ = extended.fps
            _ = extended.resolution
            assert mock_probe.call_count == 0

    def test_uncached_base_fields_remain_none(self):
        """from_base() with a bare VideoMetadata transfers only what was cached.

        Bug: uncached fields should stay None; they shouldn't be fabricated.
        """
        meta = VideoMetadata(path=Path("/fake/source.mkv"))
        # Don't populate anything
        extended = ExtendedVideoMetadata.from_base(meta, frame_count=42)

        assert extended._duration_seconds is None
        assert extended._fps              is None
        assert extended._resolution       is None
        assert extended.frame_count       == 42


class TestStrategy:
    """Tests for Strategy FFmpeg argument generation."""

    def test_to_ffmpeg_args(self):
        """Test FFmpeg argument generation via encoder_args template."""
        codec = CodecConfig(
            name            = "h265-10bit",
            default_quality = 20.0,
            default_preset  = "slow",
            quality_range   = (0.0, 51.0),
            encoder_args    = [
                "-i", "{input}",
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
        args = strategy.to_ffmpeg_args(18.5)
        assert args == [
            "-i", "{input}",
            "-c:v", "libx265",
            "-preset", "slow",
            "-crf", "18.5",
            "-pix_fmt", "yuv420p10le",
            "-x265-params", "aq-mode=3:aq-strength=0.8",
        ]

        # With crop — -vf kept, {vf} replaced with filter
        args_crop = strategy.to_ffmpeg_args(18.5, vf_filter="crop=1920:800:0:140")
        assert args_crop == [
            "-i", "{input}",
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
            default_quality = 28.0,
            default_preset  = "p7",
            quality_range   = (1.0, 51.0),
            quality_label   = "CQ",
            encoder_args    = [
                "-hwaccel", "cuda",
                "-i", "{input}",
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
        args = strategy.to_ffmpeg_args(28.0)
        vf_idx = args.index("-vf")
        assert args[vf_idx + 1] == "scale_cuda=format=p010le:"

        # With crop — filter appended after the colon
        args_crop = strategy.to_ffmpeg_args(28.0, vf_filter="crop=1920:800:0:140")
        vf_idx = args_crop.index("-vf")
        assert args_crop[vf_idx + 1] == "scale_cuda=format=p010le:crop=1920:800:0:140"
