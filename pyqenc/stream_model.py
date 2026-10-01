"""File → Stream object model: the composition family for direct-from-source
processing (spec ``2026-09-25 file-stream-model``).

Every logical entity is instantiated exactly once per run by its owning phase
and passed by reference through phase results:

- :class:`File` — JobPhase; the source file's identity (path + size).
- :class:`VideoStream` / :class:`AudioStream` / :class:`SubtitleStream` /
  :class:`AttachmentStream` — ExtractionPhase; a typed stream composing the
  ``File`` with its fast-facet info. Chapters and timestamps are **not**
  streams — they carry no ``track_id`` and are not ``-map`` selectable; they
  persist as plain extracted paths on :class:`ExtractionSidecar`.
- :class:`ExtendedVideoStream` — ProbePhase; the slow facet (frame count,
  crop) above the base video stream. The input type of every downstream video
  phase.
- :class:`VideoStreamChunk` — ChunkingPhase; an extended video stream plus a
  ``[start, end)`` timestamp window. No file on disk.
- :class:`EncodedChunk` — EncodingPhase; an attempt as a stream (the attempt
  file's own video) composed with its source chunk, strategy and CRF.
- :class:`Chapters` / :class:`AudioOutput` / :class:`MergedVideo` — artifact
  payloads (spec ``2026-09-28 artifact-model``): the container's chapter
  edition, one processed (track, chain) audio output, and one merged output
  per strategy with its measured facts.

All fields are eager — no property access triggers a probe. Producer
guarantees are guarded by plain asserts at consumers (a violated guarantee is
a programming bug, not a user-facing validation error).

Serialization follows the unique-property rule: dumping an object serializes
only its own info slice, never a composed reference; loading re-composes from
the in-run object graph with the sidecar's source identity validated. The
only (de)serialization path is ``model_dump(exclude_none=True)`` /
``model_validate``; type conversions (``Fraction``, ``Decimal``, ``LongPath``)
are declared once on the annotated types below.
"""
# CHerSun 2026

from abc import abstractmethod
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

from pyqenc.audio.layout import ChannelLayout
from pyqenc.constants import (
    CHAIN_FILENAME_SUFFIX,
    CHUNK_NAME_PATTERN,
    ENCODED_ATTEMPT_NAME_PATTERN,
    FFMPEG_SELECTOR_PREFIX,
    RANGE_SEPARATOR,
    SELECTOR_KEY_CH,
    SELECTOR_KEY_LANG,
    SELECTOR_KEY_TITLE,
    TIME_SEPARATOR,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import CropParams, Strategy
from pyqenc.utils.ffmpeg_runner import FFmpegInput
from pyqenc.utils.long_path import LongPath
from pyqenc.utils.naming import sanitize_filesystem_text

# ---------------------------------------------------------------------------
# YAML-annotated types — conversions declared once, never per model
# ---------------------------------------------------------------------------

def _fraction_from_yaml(value: object) -> object:
    """Convert a serialized ``[numerator, denominator]`` pair into a Fraction."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return Fraction(int(value[0]), int(value[1]))
    return value


def _fraction_to_yaml(value: Fraction) -> list[int]:
    """Serialize a Fraction as ``[numerator, denominator]`` (project convention)."""
    return [value.numerator, value.denominator]


FractionYaml = Annotated[
    Fraction,
    BeforeValidator(_fraction_from_yaml),
    PlainSerializer(_fraction_to_yaml, return_type=list[int]),
]
"""A ``Fraction`` persisted as ``[numerator, denominator]`` — the existing
average-fps convention (e.g. ``[24000, 1001]``)."""

LongPathYaml = Annotated[
    LongPath,
    PlainSerializer(str, return_type=str),
]
"""A :class:`LongPath` persisted as its plain string form (no ``\\\\?`` prefix)."""

DecimalYaml = Annotated[
    Decimal,
    PlainSerializer(str, return_type=str),
]
"""A ``Decimal`` persisted as its exact string form (no float drift)."""


# ---------------------------------------------------------------------------
# Req 1 — File
# ---------------------------------------------------------------------------

class File(BaseModel):
    """A file on disk plus its basic identity metadata.

    Constructed exactly once per run by JobPhase (populated eagerly from the
    filesystem) and exposed on ``JobPhaseResult``; every downstream phase
    obtains it by reference. ``job.yaml`` persists only the :class:`File` dump,
    and sidecar source-identity checks compare the persisted path +
    ``file_size_bytes`` against live values.

    Attributes:
        path:            Link to the file on disk.
        file_size_bytes: File size in bytes, or ``None`` when unavailable
                         (e.g. the file is a pipe or the stat failed).
    """

    model_config = ConfigDict(frozen=True)

    path:            LongPathYaml
    file_size_bytes: int | None = None


# ---------------------------------------------------------------------------
# Req 2 — Stream infos (fast facet) and the generic Stream base
# ---------------------------------------------------------------------------

class StreamInfo(BaseModel):
    """Container-level properties every stream carries (the fast facet).

    The placement pair (``start_timestamp`` / ``duration_seconds``) is
    per-stream container placement — each stream sits at an offset on the
    container timeline (MKV per-track block timestamps/CodecDelay, MP4 edit
    lists, TS per-stream PTS); A/V alignment derives from the streams'
    differing start times.

    Attributes:
        track_id:          Stream index inside the container — the ``-map``
                           selector target (``0:<track_id>``).
        codec_name:        ffprobe codec name (e.g. ``hevc``, ``flac``).
        language:          ISO language tag, when the stream declares one.
        title:             The stream's title tag (free media-sourced text).
        start_timestamp:   The stream's start offset on the container timeline
                           (ffprobe ``start_time``) — critical for aligning
                           streams against each other if they are ever merged.
        duration_seconds:  The stream's own duration.
    """

    model_config = ConfigDict(frozen=True)

    track_id:         int
    codec_name:       str | None = None
    language:         str | None = None
    title:            str | None = None
    start_timestamp:  float | None = None
    duration_seconds: float | None = None

    @staticmethod
    def _tags_of(raw: dict) -> dict:
        """The stream's tags dict (possibly nested under the container's tag list)."""
        return raw.get("tags") or {}

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        """Parse an ffprobe scalar into a float, tolerating missing/bad values."""
        if value is None:
            return None
        try:
            return float(str(value))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _duration_from_tags(tags: dict) -> float | None:
        """Parse a Matroska ``DURATION`` tag (``HH:MM:SS.nnnnnnnnn``) to seconds.

        MKV streams carry no ffprobe-level ``duration`` float — the stream
        duration lives only in the per-track ``DURATION`` tag.
        """
        raw = tags.get("DURATION")
        if not isinstance(raw, str):
            return None
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        try:
            hours, minutes, seconds = (float(part) for part in parts)
        except ValueError:
            return None
        return hours * 3600 + minutes * 60 + seconds

    @classmethod
    def _base_ffprobe_fields(cls, raw: dict) -> dict:
        """The container-level fields every stream carries, from one ffprobe dict."""
        tags = cls._tags_of(raw)
        return {
            "track_id":         int(raw.get("index", -1)),
            "codec_name":       raw.get("codec_name"),
            "language":         tags.get("language"),
            "title":            tags.get("title") or tags.get("TITLE"),
            "start_timestamp":  cls._float_or_none(raw.get("start_time")),
            "duration_seconds": (
                cls._float_or_none(raw.get("duration"))
                if raw.get("duration") is not None
                else cls._duration_from_tags(tags)
            ),
        }

    @classmethod
    def from_ffprobe(cls, raw: dict) -> Self:
        """Build the info slice from one ffprobe stream dict.

        The info classes own their external-data mapping: ffprobe dicts become
        typed fields exactly once, here. Subclasses extend with their own
        fields.
        """
        return cls(**cls._base_ffprobe_fields(raw))


class VideoStreamInfo(StreamInfo):
    """Video-specific fast-facet properties.

    Attributes:
        fps:           Average frames per second as a float (display/log value).
        fps_fraction:  Exact average fps as a rational (e.g. ``24000/1001``)
                       — the value timestamp conversions compute with.
        resolution:    ``"<width>x<height>"`` (e.g. ``"1920x1080"``).
        pix_fmt:       Pixel format name (e.g. ``yuv420p10le``) — a source
                       property worth preserving.
    """

    fps:          float | None       = None
    fps_fraction: FractionYaml | None = None
    resolution:   str | None         = None
    pix_fmt:      str | None         = None

    @staticmethod
    def _parse_resolution(resolution: str) -> tuple[int, int] | None:
        """Parse a ``'WxH'`` resolution string into ``(width, height)``;
        ``None`` if parsing fails.
        """
        try:
            w, h = resolution.split("x")
            return int(w), int(h)
        except (ValueError, AttributeError):
            return None

    @property
    def total_pixels(self) -> int | None:
        """Total pixel count over the stream's duration, from the fast facets.

        Resolution area × ``fps * duration_seconds``; ``None`` when any input
        facet is missing or invalid. Feeds the disk-space estimate.
        """
        if not self.resolution:
            return None
        res = self._parse_resolution(self.resolution)
        if res is None or self.fps is None or self.duration_seconds is None or self.fps <= 0:
            return None
        return res[0] * res[1] * int(self.fps * self.duration_seconds)

    @classmethod
    def from_ffprobe(cls, raw: dict) -> Self:
        """Build from one ffprobe video stream dict (fps as an exact rational)."""
        fps_fraction: Fraction | None = None
        frame_rate = raw.get("r_frame_rate")
        if isinstance(frame_rate, str) and "/" in frame_rate:
            num_s, den_s = frame_rate.split("/", 1)
            try:
                den = int(den_s)
                if den != 0:
                    fps_fraction = Fraction(int(num_s), den)
            except ValueError:
                pass

        width, height = raw.get("width"), raw.get("height")
        return cls(
            **StreamInfo._base_ffprobe_fields(raw),
            fps          = float(fps_fraction) if fps_fraction is not None else None,
            fps_fraction = fps_fraction,
            resolution   = f"{width}x{height}" if width and height else None,
            pix_fmt      = raw.get("pix_fmt"),
        )


class AudioStreamInfo(StreamInfo):
    """Audio-specific fast-facet properties.

    Attributes:
        layout: The track's channel layout (faithful source token plus its
                canonical form and channel count).
    """

    layout: ChannelLayout | None = None

    @classmethod
    def from_ffprobe(cls, raw: dict) -> Self:
        """Build from one ffprobe audio stream dict, resolving the channel layout.

        Layout fallback: the declared ``channel_layout``, else ``<channels>.0``
        derived from the raw channel count.
        """
        layout: ChannelLayout | None = None
        if channel_layout := raw.get("channel_layout"):
            layout = ChannelLayout.parse(channel_layout)
        elif channels := raw.get("channels"):
            layout = ChannelLayout.parse(f"{channels}.0")
        return cls(**StreamInfo._base_ffprobe_fields(raw), layout=layout)


class SubtitleStreamInfo(StreamInfo):
    """Subtitle-specific properties plus the extracted-file path.

    Attributes:
        is_forced:      Whether the track is a forced-subtitle track.
        extracted_path: Path of the extracted subtitle file (relative to the
                        work dir on disk), or ``None`` before extraction.
    """

    is_forced:      bool               = False
    extracted_path: LongPathYaml | None = None

    @classmethod
    def from_ffprobe(cls, raw: dict) -> Self:
        """Build from one ffprobe subtitle stream dict (forced flag from disposition)."""
        return cls(
            **StreamInfo._base_ffprobe_fields(raw),
            is_forced=(raw.get("disposition") or {}).get("forced") == 1,
        )


class AttachmentStreamInfo(StreamInfo):
    """Attachment-specific properties plus the extracted-file path.

    Attributes:
        attachment_id:  The attachment's ID in the mkvextract/mkvmerge
                        numbering — its own 1-based positional space,
                        separate from the ffprobe stream index
                        (``track_id``).
        codec_type:     ffprobe's codec_type verbatim: ``"video"`` for
                        attached pictures, ``"attachment"`` for true
                        attachments (fonts and other attached files).
        mimetype:       The declared MIME type from the container tags —
                        external fact, preserved for downstream use.
        filename:       The attachment's original filename (from its tags).
        extracted_path: Path of the dumped attachment file (relative to the
                        work dir on disk), or ``None`` before extraction.
    """

    attachment_id:  int
    codec_type:     str
    mimetype:       str | None         = None
    filename:       str | None         = None
    extracted_path: LongPathYaml | None = None

    @classmethod
    def from_ffprobe(cls, raw: dict, attachment_id: int) -> Self:
        """Build from one ffprobe attachment dict at its positional mkv ID.

        ``attachment_id`` is the 1-based position among the source's
        attachments — the numbering mkvextract/mkvmerge key on. The
        enumerator walking the streams in file order assigns it.
        """
        tags = StreamInfo._tags_of(raw)
        return cls(
            **StreamInfo._base_ffprobe_fields(raw),
            attachment_id = attachment_id,
            codec_type    = raw.get("codec_type", ""),
            mimetype      = tags.get("mimetype"),
            filename      = tags.get("filename"),
        )


class Stream[InfoT: StreamInfo](BaseModel):
    """A stream inside a container: a :class:`File` composed with its info.

    The generic base earns its keep three ways: ``as_input()`` (the
    ``0:<track_id>`` selector) is implemented exactly once; the extraction
    inventory is written generically over ``Stream[InfoT]``; and there is one
    type bound for "anything ``-map``-selectable in this container". The base
    adds no dumpable payload of its own — dumps are the per-stream info slice,
    never the composed :class:`File`.

    Type parameter:
        InfoT: The stream-info slice this stream composes (bound to
               :class:`StreamInfo`).

    Attributes:
        file: The container file this stream lives in.
        info: The stream's fast-facet info slice.
    """

    model_config = ConfigDict(frozen=True)

    file: File
    info: InfoT

    def as_input(self) -> FFmpegInput:
        """The stream as a runner input: its file plus its ``-map`` selector.

        The single place stream location is expressed — no call site builds a
        selector by hand.

        Returns:
            ``FFmpegInput`` with the stream's file and the ``0:<track_id>``
            selector.
        """
        return FFmpegInput(
            path     = self.file.path,
            selector = f"{FFMPEG_SELECTOR_PREFIX}{self.info.track_id}",
        )

    @abstractmethod
    def display_name(self) -> str:
        """Display name — identity fields verbatim, any symbols, never on disk.

        Contract method: every concrete stream class must implement it.
        """

    def safe_name(self) -> str:
        """The display name made filesystem-safe (the two-name pattern).

        The same name with every filesystem-unsafe character replaced through
        the shared sanitize primitive — the form used wherever a stream's
        name becomes part of a filename (audio chain outputs).
        """
        return sanitize_filesystem_text(self.display_name())


class VideoStream(Stream[VideoStreamInfo]):
    """A video stream — ``info`` is statically :class:`VideoStreamInfo`."""

    def display_name(self) -> str:
        """Display name for logs and include/exclude filtering (never on disk)."""
        tags = _display_tags(self.info)
        if self.info.resolution:
            tags.append(f"res={self.info.resolution}")
        return _format_display_name("video", self.info, tags)


class AudioStream(Stream[AudioStreamInfo]):
    """An audio stream — ``info`` is statically :class:`AudioStreamInfo`."""

    def display_name(self) -> str:
        """Display name for logs and include/exclude filtering (never on disk)."""
        tags = _display_tags(self.info)
        if self.info.layout is not None:
            tags.append(f"ch={self.info.layout.original}")
        return _format_display_name("audio", self.info, tags)

    def selector_string(self) -> str:
        """The conventional, regex-friendly targeting string for ``audio.select``.

        Contains ``lang=<code>``, ``ch=<layout>``, and ``title=<text>`` tokens
        (title omitted when absent), space-separated. The ``ch=`` token uses
        the layout's faithful source token (``ChannelLayout.original``) so a
        user's select regex matches the source layout exactly (e.g.
        ``ch=5.1(side)``). Derived purely from the enumerated info fields —
        a display string, never used on disk.

        Returns:
            The conventional string (e.g. ``"lang=eng ch=5.1(side) title=Surround"``).
        """
        tokens: list[str] = [
            f"{SELECTOR_KEY_LANG}={self.info.language or ''}",
            f"{SELECTOR_KEY_CH}={self.info.layout.original if self.info.layout is not None else ''}",
        ]
        if self.info.title:
            tokens.append(f"{SELECTOR_KEY_TITLE}={self.info.title}")
        return " ".join(tokens)


class SubtitleStream(Stream[SubtitleStreamInfo]):
    """A subtitle stream — ``info`` is statically :class:`SubtitleStreamInfo`."""

    def display_name(self) -> str:
        """Display name for logs and include/exclude filtering (never on disk)."""
        tags = _display_tags(self.info)
        if self.info.is_forced:
            tags.append("forced")
        return _format_display_name("subtitle", self.info, tags)

    @property
    def file_extension(self) -> str:
        """The subtitle's file extension, derived from its codec."""
        codec = (self.info.codec_name or "").lower()
        if "subrip"      in codec: return "srt"
        if "dvd"         in codec: return "sub"
        if "pgs"         in codec: return "pgs"
        # ffmpeg reports ASS/SSA text subtitles as 'ass' (modern) or 'ssa'/
        # 'substation' (older); all are ASS-family text subs written as .ass.
        if "ass"         in codec: return "ass"
        if "ssa"         in codec or "substation" in codec: return "ssa"
        raise ValueError(f"Unknown subtitle codec: {self.info.codec_name}")


class AttachmentStream(Stream[AttachmentStreamInfo]):
    """An attachment stream — ``info`` is statically :class:`AttachmentStreamInfo`."""

    def display_name(self) -> str:
        """Display name for logs and include/exclude filtering (never on disk).

        Attached pictures carry their codec name (e.g. ``mjpeg``); true
        attachments carry their MIME type's type portion (``font`` from
        ``font/ttf``); with neither, the codec slot is omitted.
        """
        tags = _display_tags(self.info)
        if self.info.filename:
            tags.append(f"filename={self.info.filename}")
        codec = self.info.codec_name or (
            self.info.mimetype.split("/", 1)[0] if self.info.mimetype else None)
        return _format_display_name("attachment", self.info, tags, codec)


def _display_tags(info: StreamInfo) -> list[str]:
    """Structured identity tags for display names: the language token.

    The free-form ``title`` is NOT included here — it is appended last by
    :func:`_format_display_name` so user-authored text can never sit in the
    middle of a name (first-occurrence parsing of ``ch=``/``res=`` tokens
    stays reliable). Display names carry identity fields as-is; the
    filesystem-safe form is :meth:`Stream.safe_name`, never a pre-sanitized
    display name.
    """
    return [f"lang={info.language}"] if info.language else []


def _format_display_name(
    stream_type: str,
    info:        StreamInfo,
    tags:        list[str],
    codec:       str | None = None,
) -> str:
    """Assemble ``#N (type-codec) tag… title…`` — display names never touch the disk.

    ``codec`` overrides the info's ``codec_name`` in the ``type-codec`` slot
    (an attachment without a codec name substitutes its MIME type's type
    portion); a missing codec token omits the slot — ``#N (type)``. The
    free-form ``title=`` token always comes last, after all structured tags.
    """
    token = info.codec_name if codec is None else codec
    head = (f"#{info.track_id} ({stream_type}-{token})" if token
                      else f"#{info.track_id} ({stream_type})")
    trailing = [f"title={info.title}"] if info.title else []
    return " ".join(filter(None, [head, *tags, *trailing]))


# ---------------------------------------------------------------------------
# Req 3 — Extended video stream (slow facet)
# ---------------------------------------------------------------------------

class ExtendedVideoStream(BaseModel):
    """A video stream carrying the slow facet: frame count and crop.

    The type every downstream video phase accepts — demanding it via the type
    system means audio-only runs never pay for the slow probe. ProbePhase is
    the sole producer/owner.

    Attributes:
        stream:      The base video stream (fast facet).
        frame_count: Total frames; ``0`` is the unknown sentinel, as today.
        crop:        Detected/configured crop — **non-optional**: an empty
                     :class:`CropParams` (:meth:`~pyqenc.models.CropParams.is_empty`)
                     means "no crop". ``None`` ("auto") exists only at
                     config/CLI level, never here.
    """

    model_config = ConfigDict(frozen=True)

    stream:      VideoStream
    frame_count: int = 0
    crop:        CropParams


# ---------------------------------------------------------------------------
# Req 4 — Chunk (timestamp window)
# ---------------------------------------------------------------------------

class VideoStreamChunk(BaseModel):
    """An extended video stream bounded by a ``[start, end)`` timestamp window.

    Positioning works uniformly for CFR and VFR — no frame-index arithmetic
    anywhere. The chunk id is a pure function of the window (naming unchanged:
    ``HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm``), and this class is the sole consumer of
    the chunk-name constants: generation (``format_chunk_id`` / ``chunk_id``)
    and parsing (``parse_chunk_id``) live here as a strict inverse pair.

    Attributes:
        stream:          The extended video stream being windowed.
        start_timestamp: Window start on the source timeline (seconds).
        end_timestamp:   Window end on the source timeline (seconds).
        frame_count:     Detector-derived frame count — the difference of
                         consecutive boundary frames, the last chunk closing
                         against the source total; ``0`` = unknown.
    """

    model_config = ConfigDict(frozen=True)

    stream:          ExtendedVideoStream
    start_timestamp: float
    end_timestamp:   float
    frame_count:     int = 0

    @staticmethod
    def format_chunk_id(start_ts: float, end_ts: float) -> str:
        """Return the canonical chunk id for a timestamp range.

        Millisecond precision — the inverse of :meth:`parse_chunk_id` for
        ms-quantized timestamps.

        Args:
            start_ts: Window start in seconds.
            end_ts:   Window end in seconds.

        Returns:
            The chunk id ``HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm``.
        """
        return VideoStreamChunk._format_window(start_ts, end_ts).replace(
            ":", TIME_SEPARATOR_SAFE,
        ).replace(
            ".", TIME_SEPARATOR_MS,
        )

    @staticmethod
    def _format_window(start_ts: float, end_ts: float) -> str:
        """The window in natural separators — the single name generator.

        ``HH:MM:SS.mmm-HH:MM:SS.mmm``; the chunk id (:meth:`safe_name`) is this
        display form with the time separators substituted, never a second
        assembly.
        """
        def _bound(ts: float) -> str:
            return TIME_SEPARATOR.join([
                f"{int(ts // 3600):02d}",
                f"{int((ts % 3600) // 60):02d}",
                f"{ts % 60:06.3f}",
            ])
        return f"{_bound(start_ts)}{RANGE_SEPARATOR}{_bound(end_ts)}"

    @property
    def duration_seconds(self) -> float:
        """The window's duration in seconds."""
        return self.end_timestamp - self.start_timestamp

    def display_name(self) -> str:
        """Display name — the window in natural separators (single generator)."""
        return self._format_window(self.start_timestamp, self.end_timestamp)

    def safe_name(self) -> str:
        """Filesystem-safe name — the chunk id (separator-substituted display)."""
        return self.format_chunk_id(self.start_timestamp, self.end_timestamp)

    @classmethod
    def parse_chunk_id(
        cls,
        chunk_id:    str,
        stream:      ExtendedVideoStream,
        frame_count: int = 0,
    ) -> VideoStreamChunk:
        """Reconstruct the chunk from its id; the caller supplies the stream.

        The parsing half of the inverse pair trusted by presence-based
        recovery: ``parse(format(x)) == x`` for ms-quantized windows.

        Args:
            chunk_id:    The chunk id to parse.
            stream:      The extended video stream the window applies to
                         (the owning phase supplies it on reconstruction).
            frame_count: Detector-derived count, when known.

        Returns:
            The reconstructed chunk.

        Raises:
            ValueError: If ``chunk_id`` does not match the chunk-name pattern.
        """
        if not CHUNK_NAME_PATTERN.match(chunk_id):
            raise ValueError(f"Not a chunk id: {chunk_id!r}")

        def _parse_bound(bound: str) -> float:
            hours_s, minutes_s, seconds_s = bound.split(TIME_SEPARATOR_SAFE)
            return (
                int(hours_s) * 3600
                + int(minutes_s) * 60
                + float(seconds_s.replace(TIME_SEPARATOR_MS, "."))
            )

        start_s, end_s = chunk_id.split(RANGE_SEPARATOR)
        return cls(
            stream          = stream,
            start_timestamp = _parse_bound(start_s),
            end_timestamp   = _parse_bound(end_s),
            frame_count     = frame_count,
        )

    def as_input(self) -> FFmpegInput:
        """The windowed stream input — the base input plus the window.

        Returns:
            ``FFmpegInput`` with the stream's file/selector and the window as
            input-side ``-ss`` / ``-t``.
        """
        return replace(
            self.stream.stream.as_input(),
            start_seconds    = self.start_timestamp,
            duration_seconds = self.duration_seconds,
        )


# ---------------------------------------------------------------------------
# Req 14 — Encoded attempt as a stream
# ---------------------------------------------------------------------------

class EncodedAttemptName(BaseModel):
    """The typed record parsed from an encoded attempt's file name.

    The name carries only part of a composed identity — recovery joins this
    record against phase results rather than pretending the name reconstructs
    the object.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id:  str
    resolution: str
    crf:       DecimalYaml


class EncodedChunk(BaseModel):
    """An encoding attempt: the attempt file's own video as a stream.

    Path, size and frame count are read through ``stream.file`` /
    ``stream.frame_count`` — never duplicated as fields. Crop is empty by
    construction (applied during the encode); the attempt's
    :class:`VideoStreamInfo` is populated eagerly, once, after the encode.

    This class owns the attempt-file name family: the name is a
    pure function of the composed identity, generation and parsing living
    here as a strict inverse pair — presence-based recovery is trustworthy
    only because ``parse(format(x)) == x`` is pinned by tests.

    Attributes:
        stream:   The attempt file's own extended video stream.
        chunk:    The source window the attempt encodes.
        strategy: The strategy the attempt was encoded with.
        crf:      The quality parameter value used for the attempt.
    """

    model_config = ConfigDict(frozen=True)

    stream:   ExtendedVideoStream
    chunk:    VideoStreamChunk
    strategy: Strategy
    crf:      DecimalYaml

    @staticmethod
    def format_file_name(chunk_id: str, resolution: str, crf: Decimal) -> str:
        """The attempt file name for an identity: ``<chunk_id>.<res>.q<crf>.mkv``."""
        return f"{chunk_id}.{resolution}.q{crf}.mkv"

    @classmethod
    def parse_file_name(cls, name: str) -> EncodedAttemptName:
        """Parse an attempt file name into its typed identity record.

        Args:
            name: The attempt file name.

        Returns:
            The parsed :class:`EncodedAttemptName`.

        Raises:
            ValueError: When ``name`` does not match the attempt-name pattern.
        """
        match = ENCODED_ATTEMPT_NAME_PATTERN.match(name)
        if not match:
            raise ValueError(f"Not an encoded attempt file name: {name!r}")
        return EncodedAttemptName(
            chunk_id   = match.group("chunk_id"),
            resolution = match.group("resolution"),
            crf        = Decimal(match.group("quality")),
        )


# ---------------------------------------------------------------------------
# Artifact payload entities (spec ``2026-09-28 artifact-model``)
# ---------------------------------------------------------------------------

class Chapters(BaseModel):
    """The container's chapter edition — an extraction payload, not a stream.

    Container-level (no ``track_id``, no ``-map`` selector). Its file name is
    the fixed constant ``chapters.xml`` — not a generated name, nothing to
    pair; the extracted location derives at the extraction phase's single
    owning site.

    Attributes:
        file: The source file the edition belongs to (identity anchor).
    """

    model_config = ConfigDict(frozen=True)

    file: File


class AudioOutput(BaseModel):
    """One processed (track, chain) output — the audio phase's payload.

    Composes the source audio stream with the producing chain's identity; the
    resolved output facts are reachable through the composition (layout via
    ``stream.info.layout``, codec via the chain resolvable from
    ``chain_name`` + config). Disk name follows the existing derivation: the
    stream's safe name plus ``" chain=<name>"``, the extension appended at the
    materialization site.

    Attributes:
        stream:     The source audio stream the output was produced from.
        chain_name: The producing chain's configured name.
        output_path: The output's materialized location (the chain-output
                     name at the audio dir — a pure function of identity).
    """

    model_config = ConfigDict(frozen=True)

    stream:      AudioStream
    chain_name:  str
    output_path: LongPathYaml

    def display_name(self) -> str:
        """Display name — the stream's display name plus the chain token."""
        return f"{self.stream.display_name()}{CHAIN_FILENAME_SUFFIX}{self.chain_name}"

    def safe_name(self) -> str:
        """Filesystem-safe name — the sanitized display form (no extension)."""
        return sanitize_filesystem_text(self.display_name())


class MergedVideo(BaseModel):
    """One merged output per strategy — the merge phase's payload.

    Composes the strategy with the source identity needed for naming plus the
    measured facts consumers need. The output name materializes from
    ``<file stem> <strategy>.mkv`` at the merge phase's single derivation site.

    Attributes:
        source_stem:  The source file's name stem (naming identity).
        strategy:     The strategy the output was merged from.
        output_path:  The output's materialized location.
        frame_count:  Measured frame count; ``None`` until measured.
        metrics:      Measured quality metrics keyed by ``"{metric}_{stat}"``.
        targets_met:  Whether quality targets were met.
        plot_path:    The quality plot PNG, when produced.
    """

    model_config = ConfigDict(frozen=True)

    source_stem:  str
    strategy:     Strategy
    output_path:  LongPathYaml
    frame_count:  int | None = None
    metrics:      dict[str, float] = {}
    targets_met:  bool             = False
    plot_path:    LongPathYaml | None = None

    def display_name(self) -> str:
        """Display name — the source stem plus the strategy, verbatim."""
        return f"{self.source_stem} {self.strategy.display_name()}"

    def safe_name(self) -> str:
        """Filesystem-safe name — the sanitized display form (no extension)."""
        return sanitize_filesystem_text(self.display_name())


# ---------------------------------------------------------------------------
# Req 5 — sidecar slices (unique-property persistence)
# ---------------------------------------------------------------------------

class SourceMismatchError(ValueError):
    """Raised when a sidecar's recorded source identity does not match the live
    :class:`File` — the owning phase re-enumerates and rewrites the sidecar."""


class _SourceSidecarBase(BaseModel):
    """Private base for sidecar models that record the source identity."""

    source: File

    def validate_source(self, live: File) -> None:
        """Validate the recorded source identity against the live file.

        Args:
            live: The in-run :class:`File` from JobPhase.

        Raises:
            SourceMismatchError: When the recorded path or size differs from
                                 the live values.
        """
        if (
            self.source.path != live.path
            or self.source.file_size_bytes != live.file_size_bytes
        ):
            raise SourceMismatchError(
                f"Sidecar source identity mismatch: recorded "
                f"{self.source.path} ({self.source.file_size_bytes} bytes), "
                f"live {live.path} ({live.file_size_bytes} bytes)."
            )


class JobSidecar(_SourceSidecarBase):
    """The ``job.yaml`` slice: the :class:`File` dump under the ``source`` key.

    The persisted path + size are the source-mismatch comparison basis.
    """


class StreamsInventory(BaseModel):
    """The per-type stream inventory of :class:`ExtractionSidecar`.

    Each entry is the stream's own info slice — the composed :class:`File` is
    never part of a dump.

    Attributes:
        video:       The (first) video stream info, or ``None`` when absent.
        audio:       Audio stream infos in track order.
        subtitles:   Subtitle stream infos in track order.
        attachments: Attachment stream infos in track order.
    """

    video:       VideoStreamInfo | None    = None
    audio:       list[AudioStreamInfo]     = []
    subtitles:   list[SubtitleStreamInfo]  = []
    attachments: list[AttachmentStreamInfo] = []


class ExtractionSidecar(_SourceSidecarBase):
    """The ``extraction.yaml`` slice: stream inventory, chapters presence,
    source identity.

    Owned by ExtractionPhase; a reuse run loads it instead of re-probing, with
    :meth:`validate_source` deciding whether the inventory is still valid.

    Attributes:
        source:   The source identity for invalidation.
        streams:  The per-type stream inventory (info slices).
        chapters: Whether the source carries a chapter edition (the extracted
                  location is the fixed ``chapters.xml`` convention — nothing
                  per-run to record).
    """

    streams:  StreamsInventory
    chapters: bool = False


class SceneRecord(BaseModel):
    """One persisted scene boundary.

    Attributes:
        timestamp_seconds: The boundary's timestamp on the source timeline.
        frame:             The detector-reported frame index — informational
                           only; no code path depends on it.
    """

    timestamp_seconds: float
    frame:             int | None = None


class ChunkingSidecar(BaseModel):
    """The ``chunking.yaml`` slice: scene boundaries (no chunking mode).

    Chunk windows are derived from these boundaries plus the stream duration
    at load time — no per-chunk records, no per-chunk sidecars.

    Attributes:
        scenes: Scene boundaries in order; the first is the stream start.
    """

    scenes: list[SceneRecord]
