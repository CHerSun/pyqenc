"""Unit tests for crop parameter injection in ChunkEncoder._encode_with_ffmpeg."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyqenc.models import CodecConfig, CropParams, StrategyConfig
from pyqenc.phases.encoding import ChunkEncoder, ChunkInfo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_encoder(crop_params: CropParams | None = None) -> ChunkEncoder:
    """Build a minimal ChunkEncoder with mocked dependencies."""
    return ChunkEncoder(
        config_manager=MagicMock(),
        quality_evaluator=MagicMock(),
        progress_tracker=MagicMock(),
        work_dir=Path("/tmp/work"),
        crop_params=crop_params,
    )


def _make_strategy_config() -> StrategyConfig:
    codec = CodecConfig(
        name="h265-8bit",
        encoder="libx265",
        pixel_format="yuv420p",
        default_crf=28.0,
        crf_range=(0.0, 51.0),
    )
    return StrategyConfig(preset="fast", profile="h265", codec=codec, profile_args=[])


def _make_chunk() -> ChunkInfo:
    return ChunkInfo(
        chunk_id="chunk.000000-000319",
        file_path=Path("/tmp/chunk.mkv"),
        start_frame=0,
        end_frame=319,
        frame_count=320,
        duration=10.0,
    )


# ---------------------------------------------------------------------------
# Helpers to capture the ffmpeg command
# ---------------------------------------------------------------------------

def _captured_cmd(encoder: ChunkEncoder, crop: CropParams | None) -> list[str]:
    """Run _encode_with_ffmpeg with a mocked subprocess and return the captured cmd."""
    encoder._crop_params = crop
    chunk = _make_chunk()
    strategy = _make_strategy_config()
    output = Path("/tmp/out.mkv")

    captured: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        captured.append(cmd)
        # Simulate successful encode: create the output file
        output.touch()
        result = MagicMock()
        result.returncode = 0
        return result

    with patch("subprocess.run", side_effect=fake_run):
        with patch.object(Path, "exists", return_value=True):
            encoder._encode_with_ffmpeg(chunk, strategy, 28.0, output)

    return captured[0] if captured else []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCropInjection:
    def test_vf_present_when_crop_set(self) -> None:
        """-vf crop=... must appear in the ffmpeg command when crop is non-empty."""
        crop = CropParams(top=140, bottom=140, left=0, right=0)
        encoder = _make_encoder(crop)
        cmd = _captured_cmd(encoder, crop)

        assert "-vf" in cmd
        vf_value = cmd[cmd.index("-vf") + 1]
        assert vf_value == crop.to_ffmpeg_filter()

    def test_vf_absent_when_crop_none(self) -> None:
        """-vf must NOT appear when crop_params is None."""
        encoder = _make_encoder(None)
        cmd = _captured_cmd(encoder, None)

        assert "-vf" not in cmd

    def test_vf_absent_when_crop_empty(self) -> None:
        """-vf must NOT appear when crop_params is all-zero (no-op crop)."""
        crop = CropParams(top=0, bottom=0, left=0, right=0)
        encoder = _make_encoder(crop)
        cmd = _captured_cmd(encoder, crop)

        assert "-vf" not in cmd

    def test_crop_filter_value_correct(self) -> None:
        """The crop filter string must match CropParams.to_ffmpeg_filter()."""
        crop = CropParams(top=10, bottom=20, left=5, right=5)
        encoder = _make_encoder(crop)
        cmd = _captured_cmd(encoder, crop)

        vf_value = cmd[cmd.index("-vf") + 1]
        assert vf_value == "crop=iw-10:ih-30:5:10"
