"""Unit tests for pyqenc/stream_model.py — the File → Stream composition family.

Covers the spec's model-family guarantees: static info concretization (no
casts at use sites), unique-slice dumps (no composed references leak),
``dump → load → dump`` byte-identity, source-identity validation, the
``as_input()`` adapters, chunk-id naming round-trips, and the shared
filesystem-name primitives.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import ValidationError
from yaml import safe_dump, safe_load

from pyqenc.audio.layout import ChannelLayout
from pyqenc.models import CodecConfig, CropParams, Strategy
from pyqenc.stream_model import (
    AttachmentStreamInfo,
    AudioStream,
    AudioStreamInfo,
    ChunkingSidecar,
    EncodedChunk,
    ExtendedVideoStream,
    ExtractionSidecar,
    File,
    JobSidecar,
    SceneRecord,
    SourceMismatchError,
    SubtitleStreamInfo,
    VideoStream,
    VideoStreamChunk,
    VideoStreamInfo,
)
from pyqenc.utils.ffmpeg_runner import FFmpegInput
from pyqenc.utils.long_path import LongPath

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _file() -> File:
    return File(path=LongPath("D:/media/source.mkv"), file_size_bytes=8_123_498_745)


def _video_stream() -> VideoStream:
    return VideoStream(
        file = _file(),
        info = VideoStreamInfo(
            track_id        = 0,
            codec_name      = "hevc",
            start_timestamp = 0.0,
            duration_seconds = 5964.48,
            fps             = 23.976024,
            fps_fraction    = Fraction(24000, 1001),
            resolution      = "1920x1080",
            pix_fmt         = "yuv420p10le",
        ),
    )


def _extended() -> ExtendedVideoStream:
    return ExtendedVideoStream(
        stream      = _video_stream(),
        frame_count = 142_932,
        crop        = CropParams(top=0, bottom=0, left=0, right=0),
    )


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------

class TestFile:
    def test_plain_path_string_is_coerced_to_long_path(self) -> None:
        """A str/pathlib input validates into a LongPath (long-path-safe I/O)."""
        f = File.model_validate({"path": "D:/media/source.mkv", "file_size_bytes": 1})
        assert isinstance(f.path, LongPath)

    def test_size_is_optional(self) -> None:
        f = File(path=LongPath("D:/media/source.mkv"))
        assert f.model_dump(exclude_none=True) == {"path": "D:\\media\\source.mkv"}

    def test_dump_uses_plain_string_path(self) -> None:
        """The YAML slice carries the plain path — never a ``\\\\?`` prefix."""
        f = _file()
        dumped = f.model_dump(exclude_none=True)
        assert dumped["path"] == str(LongPath("D:/media/source.mkv"))
        assert "\\\\?\\" not in dumped["path"]


# ---------------------------------------------------------------------------
# Generic Stream — static concretization
# ---------------------------------------------------------------------------

class TestStaticConcretization:
    def test_video_stream_info_is_concrete(self) -> None:
        """``.info`` on a named subclass is the concrete info type — video
        fields read directly, with no cast or runtime inspection."""
        stream = _video_stream()
        assert isinstance(stream.info, VideoStreamInfo)
        assert stream.info.fps == pytest.approx(23.976024)
        assert stream.info.fps_fraction == Fraction(24000, 1001)

    def test_wrong_info_type_is_rejected(self) -> None:
        """Bug prevented: a subclass silently accepting a foreign info slice
        (which would force casts at every consumer)."""
        with pytest.raises(ValidationError):
            AudioStream(file=_file(), info=VideoStreamInfo(track_id=1))

    def test_info_slice_outside_bound_is_rejected(self) -> None:
        """The TypeVar bound holds structurally: an info that is not a
        StreamInfo cannot compose into any stream."""
        with pytest.raises(ValidationError):
            VideoStream(file=_file(), info={"not": "an info"})  # type: ignore[arg-type]

    def test_audio_layout_round_trip(self) -> None:
        stream = AudioStream(
            file = _file(),
            info = AudioStreamInfo(track_id=1, codec_name="flac", layout=ChannelLayout.parse("5.1(side)")),
        )
        loaded = AudioStream.model_validate(stream.model_dump(exclude_none=True))
        assert loaded == stream
        assert loaded.info.layout.normalized == "5.1"


# ---------------------------------------------------------------------------
# as_input adapters
# ---------------------------------------------------------------------------

class TestAsInput:
    def test_stream_base_builds_selector_from_track_id(self) -> None:
        """The single place stream location is expressed: file + 0:<track_id>."""
        stream = AudioStream(
            file = _file(),
            info = AudioStreamInfo(track_id=2, codec_name="flac"),
        )
        assert stream.as_input() == FFmpegInput(path=_file().path, selector="0:2")

    def test_chunk_override_adds_the_window(self) -> None:
        """The chunk input is the base input with input-side -ss/-t applied."""
        chunk = VideoStreamChunk(
            stream          = _extended(),
            start_timestamp = 584.917,
            end_timestamp   = 1169.833,
            frame_count     = 14_012,
        )
        inp = chunk.as_input()
        assert inp.path == _file().path
        assert inp.selector == "0:0"
        assert inp.start_seconds == 584.917
        assert inp.duration_seconds == pytest.approx(584.916)


# ---------------------------------------------------------------------------
# ExtendedVideoStream / EncodedChunk contracts
# ---------------------------------------------------------------------------

class TestExtendedVideoStream:
    def test_crop_is_required_non_optional(self) -> None:
        """Bug prevented: a ``None`` crop sneaking past ProbePhase — the model
        itself must refuse it ("auto" exists only at config/CLI level)."""
        with pytest.raises(ValidationError):
            ExtendedVideoStream(stream=_video_stream(), frame_count=10)

    def test_empty_crop_means_no_crop(self) -> None:
        ext = _extended()
        assert ext.crop.is_empty()
        assert ext.frame_count == 142_932


class TestEncodedChunk:
    def test_composes_attempt_stream_chunk_strategy_crf(self) -> None:
        codec = CodecConfig(
            name            = "h265-10bit",
            default_quality = Decimal(28),
            default_preset  = "fast",
            quality_range   = (Decimal(0), Decimal(51)),
            presets         = ["fast"],
        )
        strategy = Strategy(preset="fast", profile="h265", codec=codec, profile_args=[])
        chunk = VideoStreamChunk(stream=_extended(), start_timestamp=0.0, end_timestamp=10.0)
        attempt = EncodedChunk(stream=_extended(), chunk=chunk, strategy=strategy, crf=Decimal("22.5"))
        assert attempt.crf == Decimal("22.5")
        # The attempt's path/size are read through its composed stream chain —
        # never duplicated as fields (Req 14.1).
        assert attempt.stream.stream.file.path == _file().path


# ---------------------------------------------------------------------------
# Chunk id — naming round-trips (Req 15.9)
# ---------------------------------------------------------------------------

_MS_QUANTIZED_WINDOWS = [
    (0.0, 13.33),
    (584.917, 1169.833),
    (3600.125, 7325.0),
    (86_399.999, 86_400.0),
]


class TestChunkIdRoundTrips:
    @pytest.mark.parametrize(("start", "end"), _MS_QUANTIZED_WINDOWS)
    def test_parse_format_inverse(self, start: float, end: float) -> None:
        """parse(format(x)) == x: the id alone reconstructs the window exactly
        for ms-quantized timestamps — the basis of trustworthy recovery."""
        chunk = VideoStreamChunk.parse_chunk_id(
            VideoStreamChunk.format_chunk_id(start, end), _extended(),
        )
        assert chunk.start_timestamp == start
        assert chunk.end_timestamp == end

    @pytest.mark.parametrize(("start", "end"), _MS_QUANTIZED_WINDOWS)
    def test_format_parse_inverse(self, start: float, end: float) -> None:
        """format(parse(s)) == s: formatting a parsed window reproduces the id
        byte-for-byte."""
        name = VideoStreamChunk.format_chunk_id(start, end)
        chunk = VideoStreamChunk.parse_chunk_id(name, _extended())
        assert chunk.chunk_id == name

    def test_parse_rejects_non_chunk_names(self) -> None:
        with pytest.raises(ValueError, match="Not a chunk id"):
            VideoStreamChunk.parse_chunk_id("chunk_01.mkv", _extended())

    def test_parse_carries_frame_count(self) -> None:
        chunk = VideoStreamChunk.parse_chunk_id(
            VideoStreamChunk.format_chunk_id(0.0, 10.0), _extended(), frame_count=240,
        )
        assert chunk.frame_count == 240


# ---------------------------------------------------------------------------
# Unique-slice dumps + byte-identity (Req 5)
# ---------------------------------------------------------------------------

def _yaml(model) -> str:
    """Serialize a sidecar slice exactly as it is written to disk."""
    return safe_dump(model.model_dump(exclude_none=True), allow_unicode=True, sort_keys=False)


def _reload(model):
    """Round-trip a sidecar through the on-disk YAML form and back."""
    return type(model).model_validate(safe_load(_yaml(model)))


def _extraction_sidecar() -> ExtractionSidecar:
    return ExtractionSidecar(
        source = _file(),
        streams = {
            "video": _video_stream().info,
            "audio": [AudioStreamInfo(
                track_id=1, codec_name="flac", language="eng", title="Surround 5.1",
                layout=ChannelLayout.parse("5.1(side)"), duration_seconds=5964.5,
            )],
            "subtitles": [SubtitleStreamInfo(
                track_id=3, codec_name="subrip", language="eng", title="Full",
                extracted_path=LongPath("extracted/#3 (subrip) lang=eng.srt"),
            )],
            "attachments": [AttachmentStreamInfo(
                track_id=4, filename="font.ttf",
                extracted_path=LongPath("extracted/#4 (attachment) font.ttf"),
            )],
        },
        chapters = {"extracted_path": LongPath("extracted/chapters.xml")},
        timestamps_path = LongPath("extracted/timestamps.txt"),
    )


class TestUniqueSliceDumps:
    def test_stream_dump_is_the_info_slice_only(self) -> None:
        """The composed File never leaks into a stream dump (Req 5.1)."""
        dumped = _video_stream().info.model_dump(exclude_none=True)
        assert "file" not in dumped
        assert set(dumped) == {
            "track_id", "codec_name", "start_timestamp", "duration_seconds",
            "fps", "fps_fraction", "resolution", "pix_fmt",
        }

    def test_fraction_serializes_as_num_den_pair(self) -> None:
        dumped = _video_stream().info.model_dump(exclude_none=True)
        assert dumped["fps_fraction"] == [24000, 1001]

    def test_extended_adds_only_the_slow_facet(self) -> None:
        """Beyond the composed reference, the object owns exactly the slow
        facet — frame_count and crop; the probe sidecar persists this slice
        and re-composes the base stream at load."""
        dumped = _extended().model_dump(exclude_none=True)
        assert set(dumped) - {"stream"} == {"frame_count", "crop"}


class TestSidecarByteIdentity:
    """dump → load → dump is byte-identical for every new sidecar (Req 5.3)."""

    def test_job_sidecar(self) -> None:
        sidecar = JobSidecar(source=_file())
        assert _yaml(_reload(sidecar)) == _yaml(sidecar)

    def test_extraction_sidecar(self) -> None:
        sidecar = _extraction_sidecar()
        assert _yaml(_reload(sidecar)) == _yaml(sidecar)

    def test_chunking_sidecar(self) -> None:
        sidecar = ChunkingSidecar(scenes=[
            SceneRecord(timestamp_seconds=0.0, frame=0),
            SceneRecord(timestamp_seconds=584.917, frame=14012),
        ])
        assert _yaml(_reload(sidecar)) == _yaml(sidecar)


# ---------------------------------------------------------------------------
# Source-identity validation (Req 5.2)
# ---------------------------------------------------------------------------

class TestSourceIdentityValidation:
    def test_matching_identity_passes(self) -> None:
        _extraction_sidecar().validate_source(_file())

    def test_size_mismatch_raises(self) -> None:
        """Bug prevented: a sidecar from a different (re-created) source file
        being trusted as a valid inventory."""
        live = File(path=_file().path, file_size_bytes=999)
        with pytest.raises(SourceMismatchError, match="identity mismatch"):
            _extraction_sidecar().validate_source(live)

    def test_path_mismatch_raises(self) -> None:
        live = File(path=LongPath("D:/media/other.mkv"), file_size_bytes=_file().file_size_bytes)
        with pytest.raises(SourceMismatchError):
            _extraction_sidecar().validate_source(live)

    def test_job_sidecar_validates_too(self) -> None:
        JobSidecar(source=_file()).validate_source(_file())
        with pytest.raises(SourceMismatchError):
            JobSidecar(source=_file()).validate_source(
                File(path=_file().path, file_size_bytes=None),
            )


# ---------------------------------------------------------------------------
# Filesystem-name primitives (Req 15.2)
# ---------------------------------------------------------------------------

class TestSanitizeFilesystemText:
    def test_forbidden_chars_replaced(self) -> None:
        """A stream title with Windows-forbidden characters is consumable —
        replacement, never rejection."""
        from pyqenc.utils.naming import sanitize_filesystem_text
        assert sanitize_filesystem_text('Title: "Part <1>?') == "Title_ _Part _1__"

    def test_control_chars_replaced(self) -> None:
        from pyqenc.utils.naming import sanitize_filesystem_text
        assert sanitize_filesystem_text("bad\n\x00name") == "bad__name"

    def test_clean_text_unchanged(self) -> None:
        from pyqenc.utils.naming import sanitize_filesystem_text
        assert sanitize_filesystem_text("Surround 5.1(side)") == "Surround 5.1(side)"

    def test_unicode_outside_forbidden_set_preserved(self) -> None:
        from pyqenc.utils.naming import sanitize_filesystem_text
        assert sanitize_filesystem_text("Русские субтитры") == "Русские субтитры"
