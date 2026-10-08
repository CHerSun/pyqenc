"""Unit tests for pyqenc/stream_model.py — the File → Stream composition family.

Covers the spec's model-family guarantees: static info concretization (no
casts at use sites), unique-slice dumps (no composed references leak),
``dump → load → dump`` byte-identity, identity keys, the
``as_input()`` adapters, chunk-id naming round-trips, and the shared
filesystem-name primitives.
"""

from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import ValidationError
from yaml import safe_dump, safe_load

from pyqenc.audio.layout import ChannelLayout
from pyqenc.models import CodecConfig, CropParams, Fingerprint, Strategy
from pyqenc.phases.chunking import ChunkingSidecar, SceneRecord
from pyqenc.phases.extraction import ExtractionSidecar
from pyqenc.phases.job import JobSidecar, JobSourceRecord
from pyqenc.stream_model import (
    AttachmentStreamInfo,
    AudioStream,
    AudioStreamInfo,
    EncodedChunk,
    ExtendedVideoStream,
    File,
    MergedVideo,
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

_STUB_FINGERPRINT = Fingerprint(token="a" * 32, size=8_123_498_745)
"""A source identity key stub — the same shape every phase sidecar persists."""


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
            AudioStream(file=_file(), info=VideoStreamInfo(track_id=1))  # ty: ignore[invalid-argument-type] — rejection is the test's subject

    def test_info_slice_outside_bound_is_rejected(self) -> None:
        """The TypeVar bound holds structurally: an info that is not a
        StreamInfo cannot compose into any stream."""
        with pytest.raises(ValidationError):
            VideoStream(file=_file(), info={"not": "an info"})

    def test_audio_layout_round_trip(self) -> None:
        stream = AudioStream(
            file = _file(),
            info = AudioStreamInfo(track_id=1, codec_name="flac", layout=ChannelLayout.parse("5.1(side)")),
        )
        loaded = AudioStream.model_validate(stream.model_dump(exclude_none=True))
        assert loaded == stream
        assert loaded.info.layout is not None
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
            ExtendedVideoStream(stream=_video_stream(), frame_count=10)  # ty: ignore[missing-argument] — rejection is the test's subject

    def test_empty_crop_means_no_crop(self) -> None:
        ext = _extended()
        assert ext.crop.is_empty()
        assert ext.frame_count == 142_932


class TestEncodedChunk:
    def test_composes_attempt_stream_chunk_strategy(self) -> None:
        """The payload is identity-only: stream + chunk + strategy. The
        winning quality is NOT a field (Req 4) — it lives on the winner
        sidecar as a fact for processing-path consumers."""
        codec = CodecConfig(
            name            = "h265-10bit",
            default_quality = Decimal(28),
            default_preset  = "fast",
            quality_range   = (Decimal(0), Decimal(51)),
            presets         = ["fast"],
        )
        strategy = Strategy(preset="fast", profile="h265", codec=codec, profile_args=[])
        chunk = VideoStreamChunk(stream=_extended(), start_timestamp=0.0, end_timestamp=10.0)
        attempt = EncodedChunk(stream=_extended(), chunk=chunk, strategy=strategy)
        # The attempt's path/size are read through its composed stream chain —
        # never duplicated as fields (Req 14.1).
        assert attempt.stream.stream.file.path == _file().path

    def test_name_families_are_compose_only(self) -> None:
        """Both encoding name families are pure functions of identity — the
        attempt keyed by quality (the search's cache address), the winner by
        chunk alone; sidecars are stem swaps (Req 1/9)."""
        chunk_id = "00꞉00꞉00․000-00꞉00꞉13․330"
        assert EncodedChunk.format_attempt_file_name(chunk_id, Decimal("20.5")) == (
            f"{chunk_id}.q20.5.mkv"
        )
        assert EncodedChunk.format_attempt_sidecar_name(chunk_id, Decimal("20.5")) == (
            f"{chunk_id}.q20.5.yaml"
        )
        assert EncodedChunk.format_winner_file_name(chunk_id) == f"{chunk_id}.mkv"
        assert EncodedChunk.format_winner_sidecar_name(chunk_id) == f"{chunk_id}.yaml"


class TestMergedVideoNames:
    def test_output_name_carries_per_strategy_pinned_suffix(self) -> None:
        """Req 6 / M-2b: the suffix is per-strategy whenever its range is a
        single point — uniformity across the run is never a gate."""
        collapsed = Strategy(
            preset="fast", profile="h265",
            codec=CodecConfig(
                name="h265", default_quality=Decimal("18.5"), default_preset="fast",
                quality_range=(Decimal("18.5"), Decimal("18.5")), presets=["fast"],
            ),
            profile_args=[],
        )
        ranged = Strategy(
            preset="fast", profile="h265",
            codec=CodecConfig(
                name="h265", default_quality=Decimal("18.5"), default_preset="fast",
                quality_range=(Decimal(0), Decimal(51)), presets=["fast"],
            ),
            profile_args=[],
        )
        assert MergedVideo.output_file_name("movie", collapsed) == "movie h265+fast CRF=18.5.mkv"
        assert MergedVideo.output_file_name("movie", ranged) == "movie h265+fast.mkv"
        # Two different collapsed values never share a name.
        other = collapsed.model_copy(update={
            "codec": collapsed.codec.model_copy(update={
                "quality_range": (Decimal("20.0"), Decimal("20.0")),
            }),
        })
        assert MergedVideo.output_file_name("movie", other) != MergedVideo.output_file_name("movie", collapsed)


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
        assert chunk.safe_name() == name

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
        source = _STUB_FINGERPRINT,
        streams = {
            "video": _video_stream().info,
            "audio": [AudioStreamInfo(
                track_id=1, codec_name="flac", language="eng", title="Surround 5.1",
                layout=ChannelLayout.parse("5.1(side)"), duration_seconds=5964.5,
            )],
            "subtitles": [SubtitleStreamInfo(
                track_id=3, codec_name="subrip", language="eng", title="Full",
                extracted_path=LongPath("extracted/#3 (subtitle-subrip) lang=eng.srt"),
            )],
            "attachments": [AttachmentStreamInfo(
                track_id=4, attachment_id=1, codec_type="video",
                codec_name="mjpeg", mimetype="image/jpeg", filename="cover.jpg",
                extracted_path=LongPath("extracted/#4 (attachment-mjpeg) filename=cover.jpg"),
            )],
        },
        chapters = True,
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
        sidecar = JobSidecar(source=JobSourceRecord(
            path=_file().path, fingerprint=_STUB_FINGERPRINT,
        ))
        assert _yaml(_reload(sidecar)) == _yaml(sidecar)

    def test_extraction_sidecar(self) -> None:
        sidecar = _extraction_sidecar()
        assert _yaml(_reload(sidecar)) == _yaml(sidecar)

    def test_chunking_sidecar(self) -> None:
        sidecar = ChunkingSidecar(
            scenes = [
                SceneRecord(timestamp_seconds=0.0, frame=0),
                SceneRecord(timestamp_seconds=584.917, frame=14012),
            ],
            source           = _STUB_FINGERPRINT,
            scene_threshold  = 0.27,
            min_scene_length = 15,
        )
        assert _yaml(_reload(sidecar)) == _yaml(sidecar)


# ---------------------------------------------------------------------------
# Identity keys on sidecars (Req 31/32 — the fingerprint, no path)
# ---------------------------------------------------------------------------

class TestSidecarIdentityKeys:
    def test_extraction_sidecar_persists_fingerprint_only(self) -> None:
        """Bug prevented: a sidecar key carrying the source PATH — the path is
        a runtime locator (a move must not invalidate anything); the key is
        the fingerprint pair alone (Req 31)."""
        dumped = _extraction_sidecar().model_dump(exclude_none=True)
        assert set(dumped["source"]) == {"token", "size"}
        assert "path" not in dumped["source"]


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


class TestStreamTwoNames:
    """The display/safe name pair on streams (Req 15.2).

    ``display_name`` carries identity fields verbatim; ``safe_name`` is the
    SAME name made filesystem-safe through the shared primitive.
    """

    def _audio(self) -> AudioStream:
        from pyqenc.audio.layout import ChannelLayout
        from pyqenc.stream_model import AudioStream, AudioStreamInfo, File

        return AudioStream(
            file = File(path=_file().path),
            info = AudioStreamInfo(
                track_id=1, codec_name="ac3", language="rus",
                title='Дубляж: "часть 1/2"?', layout=ChannelLayout.parse("stereo"),
            ),
        )

    def test_display_name_is_verbatim(self) -> None:
        """Bug guarded: display names pre-sanitized their titles, hiding the real
        title (slashes, quotes) from logs and include/exclude matching."""
        assert self._audio().display_name() == \
            '#1 (audio-ac3) lang=rus ch=stereo title=Дубляж: "часть 1/2"?'

    def test_safe_name_is_sanitized_display_name(self) -> None:
        """The safe name is the display name with forbidden chars replaced —
        same identity, disk-consumable form (audio chain outputs)."""
        assert self._audio().safe_name() == \
            "#1 (audio-ac3) lang=rus ch=stereo title=Дубляж_ _часть 1_2__"

    def test_subtitle_disk_form_is_safe_name_plus_extension(self) -> None:
        """Bug guarded: the subtitle disk name was a SECOND generator with its
        own token rules (``#3 (ass)`` on disk vs ``#3 (subtitle-ass)`` in the
        table). The stream exposes only the pair — the disk form is composed
        at the materialization site as safe name + codec extension, nothing
        else changed (type token included, so include/exclude patterns match
        one family)."""
        from pyqenc.stream_model import SubtitleStream, SubtitleStreamInfo

        sub = SubtitleStream(
            file = File(path=_file().path),
            info = SubtitleStreamInfo(track_id=3, codec_name="ass", language="rus",
                                      title='Часть "1"?'),
        )
        assert sub.display_name() == '#3 (subtitle-ass) lang=rus title=Часть "1"?'
        assert sub.safe_name() == "#3 (subtitle-ass) lang=rus title=Часть _1__"
        assert f"{sub.safe_name()}.{sub.file_extension}" == \
            "#3 (subtitle-ass) lang=rus title=Часть _1__.ass"

    def test_attachment_disk_form_is_safe_name_verbatim(self) -> None:
        """Bug guarded: same second-generator defect — the attachment stream
        exposes only the pair; its safe name (display already carries the
        attachment's own filename) IS the disk form, no extension appended."""
        from pyqenc.stream_model import AttachmentStream, AttachmentStreamInfo

        att = AttachmentStream(
            file = File(path=_file().path),
            info = AttachmentStreamInfo(
                track_id=4, attachment_id=1, codec_type="video",
                codec_name="mjpeg", filename="font.ttf",
            ),
        )
        assert att.display_name() == "#4 (attachment-mjpeg) filename=font.ttf"
        assert att.safe_name() == "#4 (attachment-mjpeg) filename=font.ttf"

    def test_true_attachment_display_uses_mimetype_type(self) -> None:
        """A true attachment (no codec name) shows its MIME type's type
        portion in the codec slot — never "attachment-None" — and the safe
        name keeps it a single direct name (no separators)."""
        from pyqenc.stream_model import AttachmentStream, AttachmentStreamInfo

        font = AttachmentStream(
            file = File(path=_file().path),
            info = AttachmentStreamInfo(
                track_id=5, attachment_id=1, codec_type="attachment",
                mimetype="font/ttf", filename="some font.ttf",
            ),
        )
        assert font.display_name() == "#5 (attachment-font) filename=some font.ttf"
        assert font.safe_name() == "#5 (attachment-font) filename=some font.ttf"

        neither = AttachmentStream(
            file = File(path=_file().path),
            info = AttachmentStreamInfo(
                track_id=6, attachment_id=2, codec_type="attachment",
                filename="dir/file.bin",
            ),
        )
        assert neither.display_name() == "#6 (attachment) filename=dir/file.bin"
        # The sanitize primitive collapses separators — a safe name is always
        # a single direct name, never a subdirectory path.
        assert neither.safe_name() == "#6 (attachment) filename=dir_file.bin"


class TestChunkTwoNames:
    """The display/safe pair on chunks (Req 15.10) — one generator, one transform."""

    def _chunk(self):
        from pyqenc.models import CropParams
        from pyqenc.stream_model import (
            ExtendedVideoStream,
            VideoStream,
            VideoStreamChunk,
            VideoStreamInfo,
        )

        stream = ExtendedVideoStream(
            stream = VideoStream(file=_file(), info=VideoStreamInfo(track_id=0)),
            frame_count = 25,
            crop = CropParams(),
        )
        return VideoStreamChunk(stream=stream, start_timestamp=0.0,
                                end_timestamp=1.043, frame_count=25)

    def test_display_name_uses_natural_separators(self) -> None:
        """Display form carries the natural ``:``/``.`` separators — the single
        generated form everything else derives from."""
        assert self._chunk().display_name() == "00:00:00.000-00:00:01.043"

    def test_safe_name_is_the_chunk_id_form(self) -> None:
        """The safe form (display with separators substituted) is the chunk id —
        the on-disk naming is unchanged, and parse() still round-trips it."""
        from pyqenc.stream_model import VideoStreamChunk

        chunk = self._chunk()
        assert chunk.safe_name() == "00꞉00꞉00․000-00꞉00꞉01․043"
        parsed = VideoStreamChunk.parse_chunk_id(chunk.safe_name(), chunk.stream)
        assert (parsed.start_timestamp, parsed.end_timestamp) == \
            (chunk.start_timestamp, chunk.end_timestamp)


class TestStrategyTwoNames:
    """The uniform pair on Strategy — passthroughs (safe by construction)."""

    def test_pair_passes_through_unchanged(self) -> None:
        """Bug guarded: per-type decisions about which name form to use — a
        safe-by-construction name still exposes the pair so consumers call
        ``safe_name()``/``display_name()`` uniformly."""
        from decimal import Decimal

        from pyqenc.models import CodecConfig, Strategy

        strategy = Strategy(
            preset="slow", profile="h265-aq",
            codec=CodecConfig(
                name="h265-10bit", default_quality=Decimal(20),
                default_preset="slow",
                quality_range=(Decimal(0), Decimal(51)), presets=["slow"],
            ),
            profile_args=[],
        )
        assert strategy.display_name() == strategy.safe_name() == "h265-aq+slow"
        assert not hasattr(strategy, "name"), "no third accessor — exactly the pair"


# ---------------------------------------------------------------------------
# Artifact payload entities (spec 2026-09-28 artifact-model, Req 2)
# ---------------------------------------------------------------------------

def _strategy() -> Strategy:
    return Strategy(
        preset="slow", profile="h265-aq",
        codec=CodecConfig(
            name="h265-10bit", default_quality=Decimal(20),
            default_preset="slow",
            quality_range=(Decimal(0), Decimal(51)), presets=["slow"],
        ),
        profile_args=[],
    )


def _audio_stream() -> AudioStream:
    return AudioStream(
        file = _file(),
        info = AudioStreamInfo(
            track_id        = 2,
            codec_name      = "flac",
            language        = "eng",
            start_timestamp = 0.0,
            duration_seconds = 5964.48,
            layout          = ChannelLayout.parse("5.1(side)"),
        ),
    )


class TestChaptersPayload:
    def test_composes_the_source_file_and_round_trips(self) -> None:
        """Bug guarded: the chapters edition is a container-level payload
        anchored on the source File — not a path record, not a stream."""
        from pyqenc.stream_model import Chapters

        chapters = Chapters(file=_file())
        assert Chapters.model_validate(chapters.model_dump(exclude_none=True)) == chapters

    def test_no_generated_name_pair(self) -> None:
        """Bug guarded: a fixed-constant name must not grow a generated-name
        pair — Chapters exposes neither ``display_name`` nor ``safe_name``."""
        from pyqenc.stream_model import Chapters

        assert not hasattr(Chapters(file=_file()), "display_name")


class TestAudioOutputPayload:
    def test_composition_round_trip(self) -> None:
        """Bug guarded: the (stream, chain) output is an eager frozen model
        whose dump re-validates (the payload-family serialization contract)."""
        from pyqenc.stream_model import AudioOutput

        out = AudioOutput(
            stream=_audio_stream(), chain_name="nightlong",
            output_path=LongPath("D:/w/audio/#2 (audio-flac) lang=eng chain=nightlong.flac"),
        )
        assert AudioOutput.model_validate(out.model_dump(exclude_none=True)) == out

    def test_safe_name_matches_the_chain_output_site(self) -> None:
        """Bug guarded: AudioOutput's safe name drifting from the chain-output
        materialization site (stream safe name + ``chain=<name>``) — recovery
        would classify existing outputs as absent forever."""
        from pyqenc.audio.chain import chain_output_path
        from pyqenc.constants import CHAIN_FILENAME_SUFFIX
        from pyqenc.stream_model import AudioOutput

        stream = _audio_stream()
        out = AudioOutput(stream=stream, chain_name="night", output_path=LongPath("D:/w/x.flac"))
        assert out.display_name() == f"{stream.display_name()}{CHAIN_FILENAME_SUFFIX}night"
        assert out.safe_name() == f"{stream.safe_name()}{CHAIN_FILENAME_SUFFIX}night"
        site = chain_output_path(stream, "night", "flac", LongPath("D:/w/audio"))
        assert site.stem == out.safe_name(), "materialization site must derive the same stem"


class TestMergedVideoPayload:
    def test_composition_round_trip_with_measured_facts(self) -> None:
        """Bug guarded: the merged output carries its measured facts (frame
        count, metrics, targets-met, plot) — a dump must not lose them."""
        from pyqenc.stream_model import MergedVideo

        mv = MergedVideo(
            source_stem="source", strategy=_strategy(),
            output_path=LongPath("D:/w/merged/source h265-aq+slow.mkv"),
            frame_count=142_932,
            metrics={"vmaf_min": 95.9},
            targets_met=True,
            plot_path=LongPath("D:/w/merged/source h265-aq+slow.png"),
        )
        assert MergedVideo.model_validate(mv.model_dump(exclude_none=True)) == mv

    def test_two_names_derive_from_stem_and_strategy(self) -> None:
        """Bug guarded: the merged output's name pair must stay the single
        derivation (``<file stem> <strategy>``) — display verbatim, safe the
        sanitized form, extension appended at the materialization site."""
        from pyqenc.stream_model import MergedVideo

        mv = MergedVideo(
            source_stem="source", strategy=_strategy(),
            output_path=LongPath("D:/w/merged/source h265-aq+slow.mkv"),
        )
        assert mv.display_name() == "source h265-aq+slow"
        assert mv.safe_name() == "source h265-aq+slow"
        assert mv.output_path.stem == mv.safe_name()


class TestFixedConstantNames:
    def test_extraction_fixed_constants_pinned(self) -> None:
        """Bug guarded: the fixed-constant artifact filenames are load-bearing
        recovery conventions — an accidental rename would orphan every
        existing workdir's extracted components."""
        from pyqenc.constants import CHAPTERS_FILENAME, TIMESTAMPS_FILENAME

        assert TIMESTAMPS_FILENAME == "timestamps.txt"
        assert CHAPTERS_FILENAME == "chapters.xml"
