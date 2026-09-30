"""Unit tests for crop parameter injection in ChunkEncoder._encode_with_ffmpeg."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from pyqenc.models import CodecConfig, CropParams, Strategy
from pyqenc.phases.encoding import ChunkEncoder
from pyqenc.stream_model import (
    CropParams as _CP,
)
from pyqenc.stream_model import (
    ExtendedVideoStream,
    File,
    VideoStream,
    VideoStreamChunk,
    VideoStreamInfo,
)
from pyqenc.utils.ffmpeg_runner import (
    _PROGRESS_FLAGS,
    FFmpegRequest,
    FFmpegRunResult,
    compose_command,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_encoder(crop_params: CropParams | None = None) -> ChunkEncoder:
    """Build a minimal ChunkEncoder with mocked dependencies."""
    from pyqenc.metrics import NoOpMetricsCollector
    return ChunkEncoder(
        quality_evaluator=MagicMock(),
        work_dir=Path("/tmp/work"),
        collector=NoOpMetricsCollector(),
        crop_params=crop_params,
    )


def _make_strategy() -> Strategy:
    codec = CodecConfig(
        name            = "h265-8bit",
        default_quality = 28.0,
        default_preset  = "fast",
        quality_range   = (0.0, 51.0),
        pre_input_args  = [],
        encoder_args    = [
            "-c:v", "libx265",
            "-preset", "{preset}",
            "-crf", "{quality}",
            "-vf", "{vf}",
            "-pix_fmt", "yuv420p",
            "{profile_args}",
        ],
        presets         = ["fast"],
    )
    return Strategy(preset="fast", profile="h265", codec=codec, profile_args=[])


def _make_chunk() -> VideoStreamChunk:
    stream = ExtendedVideoStream(
        stream = VideoStream(
            file = File(path="/tmp/source.mkv", file_size_bytes=64),
            info = VideoStreamInfo(
                track_id=0, codec_name="hevc", fps=25.0,
                fps_fraction=__import__("fractions").Fraction(25, 1),
                resolution="1920x1080", duration_seconds=10.0,
            ),
        ),
        frame_count = 250,
        crop        = _CP(),
    )
    return VideoStreamChunk(
        stream=stream, start_timestamp=0.0, end_timestamp=10.0, frame_count=250,
    )


# ---------------------------------------------------------------------------
# Helpers to capture the ffmpeg command
# ---------------------------------------------------------------------------

def _captured_request(encoder: ChunkEncoder, crop: CropParams | None) -> FFmpegRequest:
    """Run _encode_with_ffmpeg with a mocked runner and return the captured request."""
    encoder._crop_params = crop
    chunk = _make_chunk()
    strategy = _make_strategy()
    output = Path("/tmp/out.mkv")

    captured: list[FFmpegRequest] = []

    def fake_run_ffmpeg(request: FFmpegRequest, **_kwargs: object) -> FFmpegRunResult:
        captured.append(request)
        result = MagicMock(spec=FFmpegRunResult)
        result.success = True
        result.returncode = 0
        return result

    with patch("pyqenc.phases.encoding.run_ffmpeg", side_effect=fake_run_ffmpeg):
        encoder._encode_with_ffmpeg(chunk, strategy, Decimal("28.0"), output)

    return captured[0]


def _captured_cmd(encoder: ChunkEncoder, crop: CropParams | None) -> list[str]:
    """Composed launch argv of the request captured by ``_captured_request``."""
    return [str(a) for a in compose_command(_captured_request(encoder, crop))[0]]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEncodeCommandGolden:
    def test_golden_argv(self) -> None:
        """Bug prevented: the direct-from-source encode drifting from the
        intended windowed command — source + selector + input-side window,
        strategy output stage, ``-f matroska`` tmp output, chapter guard."""
        crop = CropParams(top=140, bottom=140, left=0, right=0)
        encoder = _make_encoder(crop)
        cmd = _captured_cmd(encoder, crop)

        assert cmd == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-ss", "0.0",
            "-t", "10.0",
            "-i", str(Path("/tmp/source.mkv")),
            "-map", "0:0",
            "-c:v", "libx265",
            "-preset", "fast",
            "-crf", "28.0",
            "-vf", "crop=iw-0:ih-280:0:140",
            "-pix_fmt", "yuv420p",
            "-map_chapters", "-1",
            "-f", "matroska", str(Path("/tmp/out.tmp")),
        ]


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
