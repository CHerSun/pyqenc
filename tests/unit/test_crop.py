"""Unit tests for pyqenc/utils/crop.py — cropdetect request composition and parsing.

The golden argv test pins the composed command for the cropdetect call site
against the pre-request hand-built command (same input, same seek window, same
filter), with the runner-owned additions (progress flags, ``-y``,
``-map_chapters -1``, null output) applied.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pyqenc.models import CropParams, VideoMetadata
from pyqenc.utils.crop import detect_crop_parameters
from pyqenc.utils.ffmpeg_runner import (
    _PROGRESS_FLAGS,
    FFmpegRequest,
    FFmpegRunResult,
    compose_command,
)


def _video() -> VideoMetadata:
    """A video with 100 s duration, 24 fps, 1920x1080 — deterministic sampling."""
    vm = VideoMetadata(path=Path("/src/source.mkv"))
    vm._duration_seconds = 100.0
    vm._fps              = 24.0
    vm._resolution       = "1920x1080"
    return vm


class TestCropDetectCommand:
    def test_golden_argv(self) -> None:
        """Bug prevented: the cropdetect conversion drifting from the original
        hand-built command (seek target, sampling filter, vframes cap)."""
        captured: list[FFmpegRequest] = []

        def fake_run(request: FFmpegRequest, **_kwargs: object) -> FFmpegRunResult:
            captured.append(request)
            return FFmpegRunResult(returncode=0, success=True, stderr_lines=[])

        with patch("pyqenc.utils.crop.run_ffmpeg", side_effect=fake_run):
            detect_crop_parameters(_video(), sample_count=50)

        assert len(captured) == 1
        argv = [str(a) for a in compose_command(captured[0])]
        assert argv == [
            "ffmpeg", *_PROGRESS_FLAGS, "-y",
            "-ss", "10.0",
            "-i", str(Path("/src/source.mkv")),
            "-vf", "select='not(mod(n\\,39))',cropdetect=24:2:0",
            "-vframes", "50",
            "-map_chapters", "-1",
            "-f", "null", "-",
        ]

    def test_no_duration_returns_empty_crop_without_running(self) -> None:
        """A video without duration info skips crop detection entirely."""
        vm = VideoMetadata(path=Path("/src/source.mkv"))
        vm._duration_seconds = None

        with patch("pyqenc.utils.crop.run_ffmpeg") as mock_run:
            crop = detect_crop_parameters(vm)

        assert crop == CropParams()
        mock_run.assert_not_called()


class TestCropDetectParsing:
    def test_most_conservative_crop_from_samples(self) -> None:
        """Detection reports per-frame crop rectangles in stderr; the result is
        the most conservative crop across all samples — the largest detected
        content area (max w/h) with the smallest offsets (min x/y) — so no
        sample's content is ever cut off."""
        lines = [
            "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 y1:10 y2:1069 w:1920 h:1060 x:0 y:10 crop=1920:1060:0:10",
            "[Parsed_cropdetect_0 @ 0x1] x1:0 x2:1919 y1:20 y2:1059 w:1920 h:1040 x:0 y:20 crop=1920:1040:0:20",
        ]
        result = FFmpegRunResult(returncode=0, success=True, stderr_lines=lines)

        with patch("pyqenc.utils.crop.run_ffmpeg", return_value=result):
            crop = detect_crop_parameters(_video())

        # Conservative content height max(1060, 1040) = 1060; top = min y = 10
        assert crop == CropParams(top=10, bottom=1080 - 1060 - 10, left=0, right=0)

    def test_no_detections_returns_empty_crop(self) -> None:
        """No cropdetect output lines → all-zero crop (no cropping needed)."""
        result = FFmpegRunResult(returncode=0, success=True, stderr_lines=[])
        with patch("pyqenc.utils.crop.run_ffmpeg", return_value=result):
            crop = detect_crop_parameters(_video())

        assert crop == CropParams()
