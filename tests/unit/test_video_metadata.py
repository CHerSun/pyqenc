"""Unit tests for VideoMetadata lazy-loading and ChunkVideoMetadata."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyqenc.models import ChunkVideoMetadata, VideoMetadata

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FFPROBE_DATA = {
    "streams": [
        {
            "duration": "5400.0",
            "r_frame_rate": "24/1",
            "width": 1920,
            "height": 1080,
        }
    ],
    "format": {},
}

_FFMPEG_STDERR = [
    "  Duration: 01:30:00.00, start: 0.000000, bitrate: 5000 kb/s",
    "    Stream #0:0: Video: hevc, yuv420p, 1920x1080, 24 fps, 24 tbr",
]


def _make_meta(path: str = "/fake/video.mkv") -> VideoMetadata:
    return VideoMetadata(path=Path(path))


# ---------------------------------------------------------------------------
# _probe_metadata called exactly once for duration / fps / resolution
# ---------------------------------------------------------------------------

class TestProbeMetadataCalledOnce:
    def test_single_probe_for_all_three_fields(self):
        """Accessing duration, fps, and resolution triggers _probe_metadata once."""
        meta = _make_meta()
        with patch.object(meta, "_probe_metadata", wraps=meta._probe_metadata) as mock_probe:
            # Pre-fill so the real subprocess is never called
            meta._duration_seconds = 5400.0
            meta._fps = 24.0
            meta._resolution = "1920x1080"

            _ = meta.duration_seconds
            _ = meta.fps
            _ = meta.resolution

            # Already filled — _probe_metadata should NOT be called
            assert mock_probe.call_count == 0

    def test_probe_metadata_called_once_when_fields_are_none(self):
        """_probe_metadata is called exactly once even when all three fields are accessed."""
        meta = _make_meta()

        call_count = 0

        def fake_probe() -> None:
            nonlocal call_count
            call_count += 1
            meta._duration_seconds = 5400.0
            meta._fps = 24.0
            meta._resolution = "1920x1080"

        with patch.object(meta, "_probe_metadata", side_effect=fake_probe):
            _ = meta.duration_seconds  # triggers probe
            _ = meta.fps              # already filled
            _ = meta.resolution       # already filled

        assert call_count == 1


# ---------------------------------------------------------------------------
# _probe_frame_count called exactly once
# ---------------------------------------------------------------------------

class TestProbeFrameCountCalledOnce:
    def test_frame_count_probe_called_once(self):
        """_probe_frame_count is called exactly once even on repeated access."""
        meta = _make_meta()

        call_count = 0

        def fake_probe_fc() -> None:
            nonlocal call_count
            call_count += 1
            meta._frame_count = 129600

        with patch.object(meta, "_probe_frame_count", side_effect=fake_probe_fc):
            _ = meta.frame_count
            _ = meta.frame_count
            _ = meta.frame_count

        assert call_count == 1

    def test_fps_access_does_not_trigger_frame_count_probe(self):
        """Accessing fps must NOT trigger _probe_frame_count."""
        meta = _make_meta()

        def fake_probe_meta() -> None:
            meta._fps = 24.0
            meta._duration_seconds = 5400.0
            meta._resolution = "1920x1080"

        with patch.object(meta, "_probe_metadata", side_effect=fake_probe_meta):
            with patch.object(meta, "_probe_frame_count") as mock_fc:
                _ = meta.fps
                assert mock_fc.call_count == 0


# ---------------------------------------------------------------------------
# populate_from_ffprobe fills fields without triggering a probe
# ---------------------------------------------------------------------------

class TestPopulateFromFfprobe:
    def test_fills_all_fields_without_probe(self):
        """populate_from_ffprobe fills backing fields; no probe is triggered."""
        meta = _make_meta()

        with patch.object(meta, "_probe_metadata") as mock_pm, \
             patch.object(meta, "_probe_frame_count") as mock_fc:
            meta.populate_from_ffprobe(_FFPROBE_DATA)
            assert mock_pm.call_count == 0
            assert mock_fc.call_count == 0

        assert meta._duration_seconds == pytest.approx(5400.0)
        assert meta._fps == pytest.approx(24.0)
        assert meta._resolution == "1920x1080"

    def test_does_not_overwrite_existing_values(self):
        """populate_from_ffprobe preserves already-cached values."""
        meta = _make_meta()
        meta._fps = 30.0  # pre-cached

        meta.populate_from_ffprobe(_FFPROBE_DATA)

        assert meta._fps == pytest.approx(30.0)  # unchanged

    def test_properties_return_populated_values_without_probe(self):
        """After populate_from_ffprobe, property access returns values without probing."""
        meta = _make_meta()
        meta.populate_from_ffprobe(_FFPROBE_DATA)

        with patch.object(meta, "_probe_metadata") as mock_pm:
            assert meta.duration_seconds == pytest.approx(5400.0)
            assert meta.fps == pytest.approx(24.0)
            assert meta.resolution == "1920x1080"
            assert mock_pm.call_count == 0


# ---------------------------------------------------------------------------
# populate_from_ffmpeg_output
# ---------------------------------------------------------------------------

class TestPopulateFromFfmpegOutput:
    def test_parses_duration_fps_resolution(self):
        meta = _make_meta()
        meta.populate_from_ffmpeg_output(_FFMPEG_STDERR)

        assert meta._duration_seconds == pytest.approx(5400.0)
        assert meta._fps == pytest.approx(24.0)
        assert meta._resolution == "1920x1080"

    def test_does_not_overwrite_existing_values(self):
        meta = _make_meta()
        meta._duration_seconds = 100.0

        meta.populate_from_ffmpeg_output(_FFMPEG_STDERR)

        assert meta._duration_seconds == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# model_dump_full / model_validate_full round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_round_trip_preserves_cached_fields(self):
        """model_dump_full -> model_validate_full preserves all cached private fields."""
        meta = _make_meta()
        meta._duration_seconds = 5400.0
        meta._frame_count      = 129600
        meta._fps              = 24.0
        meta._resolution       = "1920x1080"

        dumped   = meta.model_dump_full()
        restored = VideoMetadata.model_validate_full(dumped)

        assert restored._duration_seconds == pytest.approx(5400.0)
        assert restored._frame_count      == 129600
        assert restored._fps              == pytest.approx(24.0)
        assert restored._resolution       == "1920x1080"

    def test_round_trip_no_probe_on_property_access(self):
        """After model_validate_full, property access does not trigger any probe."""
        meta = _make_meta()
        meta._duration_seconds = 5400.0
        meta._frame_count      = 129600
        meta._fps              = 24.0
        meta._resolution       = "1920x1080"

        restored = VideoMetadata.model_validate_full(meta.model_dump_full())

        with patch.object(restored, "_probe_metadata") as mock_pm, \
             patch.object(restored, "_probe_frame_count") as mock_fc:
            assert restored.duration_seconds == pytest.approx(5400.0)
            assert restored.frame_count      == 129600
            assert restored.fps              == pytest.approx(24.0)
            assert restored.resolution       == "1920x1080"
            assert mock_pm.call_count == 0
            assert mock_fc.call_count == 0

    def test_chunk_video_metadata_round_trip(self):
        """ChunkVideoMetadata round-trip preserves chunk_id and cached fields."""
        chunk = ChunkVideoMetadata(
            path=Path("/fake/chunk.000000-000319.mkv"),
            chunk_id="chunk.000000-000319",
            start_frame=0,
        )
        chunk._frame_count = 320
        chunk._fps         = 24.0
        chunk._resolution  = "1920x800"

        dumped   = chunk.model_dump_full()
        restored = ChunkVideoMetadata.model_validate_full(dumped)

        assert restored.chunk_id    == "chunk.000000-000319"
        assert restored._frame_count == 320
        assert restored._fps         == pytest.approx(24.0)
        assert restored._resolution  == "1920x800"
