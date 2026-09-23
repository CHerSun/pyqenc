"""
Extraction phase for the quality-based encoding pipeline.

This module handles extraction of streams from the source MKV file.
It also provides the MKVTrackExtractor and stream model classes for
parsing and extracting MKV tracks via ffprobe / mkvextract.
"""
# CHerSun 2026

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pyqenc.audio.layout import ChannelLayout
from pyqenc.constants import (
    FAILURE_SYMBOL_MINOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
    TIMESTAMPS_FILENAME,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import AudioMetadata, PhaseOutcome, VideoMetadata
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.job import JobPhase
from pyqenc.state import ArtifactState
from pyqenc.utils.ffmpeg_runner import run_ffmpeg

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stream model classes (migrated from pyqenc/legacy/pymkvextract/main.py)
# ---------------------------------------------------------------------------

def sanitize_filename(s: str) -> str:
    """Sanitize string for use as a filename; unacceptable characters replaced with underscores."""
    return re.sub(r'[\\/:"*?<>|]+', '_', s)


class StreamBase:
    """Base class to get stream metadata and assist in further processing."""

    extract_type: str = ''
    """The stream type for mkvextract."""

    def __init__(self, stream: dict, index: int) -> None:
        self.index = index  # order in ffprobe streams array; unique id in case stream id is missing
        self.raw = stream
        self.tags: dict = stream.get('tags', {}) or {}
        self.disposition: dict = stream.get('disposition', {}) or {}
        self.__display_name_cached: str = ''

    @property
    def track_id(self) -> int:
        """Track id as specified in the file, or MISSING_STREAM_ID sentinel."""
        if isinstance(self.raw, dict):
            return self.raw.get('index', MKVTrackExtractor.MISSING_STREAM_ID)
        return MKVTrackExtractor.MISSING_STREAM_ID

    @property
    def codec_type(self) -> str:
        """Codec type as specified in the file, or empty string."""
        return self.raw.get('codec_type', '')

    @property
    def codec_name(self) -> str:
        """Codec name as specified in the file, or empty string."""
        return self.raw.get('codec_name', '')

    @property
    def language(self) -> str:
        """Language as specified in the file, or empty string."""
        return self.tags.get('language', self.raw.get('tag:language', ''))

    @property
    def file_extension(self) -> str:
        """File extension for this stream type."""
        return 'mkv'

    @property
    def title(self) -> str:
        """Title as specified in the file, or empty string."""
        return self.tags.get('title', '') or self.tags.get('TITLE', '')

    @property
    def title_sanitized(self) -> str:
        """Title sanitized for use as a filename."""
        return re.sub(r'[\\/:"*?<>|]+', '_', self.title)

    @property
    def start_time(self) -> float | str:
        """Start time as specified in the file, or empty string."""
        start_str = self.raw.get('start_time', '0')
        try:
            return float(start_str)
        except ValueError:
            return start_str

    def _track_ids_string(self, track_num_width: int, track_id_width: int) -> str:
        """Return a prefix string like '#01 ID=23'."""
        return f"#{str(self.index).zfill(track_num_width)} ID={str(self.track_id).zfill(track_id_width)}"

    @property
    def _wanted_tags(self) -> dict[str, Any]:
        """Tags with values interesting for display. Override in subclasses."""
        return {
            'lang':  self.language,
            'start': self.start_time,
            'title': sanitize_filename(self.title),
        }

    @property
    def _tags_formatted(self) -> list[str]:
        """Formatted list of non-empty tags for display."""
        return [f"{key}={value}" for key, value in self._wanted_tags.items() if value]

    def __display_name(self, track_num_width: int = 2, track_id_width: int = 2) -> str:
        name = ' '.join(filter(None, [
            self._track_ids_string(track_num_width, track_id_width),
            f"({self.codec_type}-{self.codec_name})",
            ' '.join(self._tags_formatted),
        ]))
        return f"{name}.{self.file_extension}"

    def display_name(self, track_num_width: int = 2, track_id_width: int = 2) -> str:
        """Human-readable filename/display string for this stream."""
        self.__display_name_cached = (
            self.__display_name_cached or self.__display_name(track_num_width, track_id_width)
        )
        return self.__display_name_cached

    def mkvextract_parts(self, output_path: Path, attachment_index: int | None = None) -> list[str]:
        """Return the mkvextract command fragments for this stream."""
        return [f"{self.track_id}:{output_path / self.display_name()}"]

    def __hash__(self) -> int:
        return self.index

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, StreamBase):
            return NotImplemented
        return self.index == other.index


class VideoStream(StreamBase):
    """Represents a video stream."""

    extract_type = 'tracks'

    @property
    def resolution(self) -> str:
        """Image resolution (WIDTHxHEIGHT) as specified in the file, or empty string."""
        width  = self.raw.get('width')
        height = self.raw.get('height')
        return f"{width}x{height}" if width and height else ''

    @property
    def file_extension(self) -> str:
        return 'mkv'

    @property
    def _wanted_tags(self) -> dict[str, Any]:
        return {
            'lang':  self.language,
            'res':   self.resolution,
            'start': self.start_time,
            'title': sanitize_filename(self.title),
        }


class AudioStream(StreamBase):
    """Represents an audio stream."""

    extract_type = 'tracks'

    @property
    def channels_layout(self) -> ChannelLayout | None:
        """Audio channel layout as a :class:`ChannelLayout`, or ``None`` when unknown.

        Prefers the explicit ``channel_layout`` token (e.g. ``5.1(side)``); falls
        back to synthesising ``<channels>.0`` from the raw ``channels`` count.
        """
        if channel_layout := self.raw.get('channel_layout'):
            return ChannelLayout.parse(channel_layout)
        if channels := self.raw.get('channels'):
            return ChannelLayout.parse(f"{channels}.0")
        return None

    @property
    def file_extension(self) -> str:
        return 'mka'

    @property
    def _wanted_tags(self) -> dict[str, Any]:
        return {
            'lang':  self.language,
            'ch':    self.channels_layout,
            'start': self.start_time,
            'title': sanitize_filename(self.title),
        }


class SubtitleStream(StreamBase):
    """Represents a subtitle stream."""

    extract_type = 'tracks'

    @property
    def is_forced(self) -> bool:
        """Whether this subtitle track is forced."""
        return self.disposition.get('forced') == '1'

    @property
    def file_extension(self) -> str:
        codec_lower = self.codec_name.lower()
        if 'subrip'      in codec_lower: return 'srt'
        if 'dvd'         in codec_lower: return 'sub'
        if 'pgs'         in codec_lower: return 'pgs'
        # ffmpeg reports ASS/SSA text subtitles as 'ass' (modern) or 'ssa'/
        # 'substation' (older); all are ASS-family text subs written as .ass.
        if 'ass'         in codec_lower: return 'ass'
        if 'ssa'         in codec_lower or 'substation' in codec_lower: return 'ssa'
        raise ValueError(f"Unknown subtitle codec: {self.codec_name}")

    @property
    def _wanted_tags(self) -> dict[str, Any]:
        return {
            'lang':   self.language,
            'forced': self.is_forced,
            'start':  self.start_time,
            'title':  sanitize_filename(self.title),
        }


class AttachmentStream(VideoStream):
    """Represents an attachment stream (images, fonts, etc.)."""

    extract_type = 'attachments'

    @property
    def file_extension(self) -> str:
        if 'png'  in self.codec_name: return 'png'
        if 'jpeg' in self.codec_name or 'jpg' in self.codec_name: return 'jpg'
        if 'gif'  in self.codec_name: return 'gif'
        raise ValueError(f"Unknown attachment codec: {self.codec_name}")

    def mkvextract_parts(self, output_path: Path, attachment_index: int | None = None) -> list[str]:
        """Return mkvextract fragments for an attachment.

        Unlike regular tracks, mkvextract expects a 1-based index within the
        attachments group, so attachment_index must be provided externally.
        """
        if attachment_index is None:
            raise ValueError('attachment_index must be provided for attachments externally')
        return [f"{attachment_index}:{output_path / self.display_name()}"]


class ChaptersStream(StreamBase):
    """Represents the chapters entity as a stream for uniform filtering."""

    extract_type = 'chapters'

    def __init__(self, stream: dict, index: int) -> None:  # type: ignore[override]
        self.index = index
        self.raw = stream
        self.chapters: list[dict] = cast(list[dict], self.raw)
        self.tags: dict = {}
        self.disposition: dict = {}

    @property
    def codec_type(self) -> str:
        return 'chapters'

    @property
    def file_extension(self) -> str:
        return 'xml'

    def display_name(self, track_num_width: int = 2, track_id_width: int = 2) -> str:
        return f"chapters.{self.file_extension}"

    def mkvextract_parts(self, output_path: Path, attachment_index: int | None = None) -> list[str]:
        return [str(output_path / self.display_name())]


class StreamFactory:
    """Factory that creates the appropriate *Stream* subclass from an ffprobe stream dict."""

    @staticmethod
    def create(stream: dict, index: int) -> StreamBase:
        """Create a typed Stream object for a single ffprobe stream dict."""
        s = dict(stream)  # shallow copy to avoid mutating caller's dict

        # Treat attached_pic or image/ mimetype as attachment
        if s.get('disposition', {}).get('attached_pic', 0) == 1 or (s.get('tags', {}) or {}).get('mimetype', '').startswith('image/'):
            s['codec_type'] = 'attachment'

        ctype = s.get('codec_type', '')
        if ctype == 'video':      return VideoStream(s, index)
        if ctype == 'audio':      return AudioStream(s, index)
        if ctype == 'subtitle':   return SubtitleStream(s, index)
        if ctype == 'attachment': return AttachmentStream(s, index)

        codec_name = s.get('codec_name', '').lower()
        tags = s.get('tags', {})
        if (
            ctype == 'data'
            or 'header' in codec_name
            or any('header' in str(k).lower() or 'header' in str(v).lower() for k, v in tags.items())
        ):
            return ChaptersStream(s, index)

        # Fallback heuristics
        if stream.get('width') and stream.get('height'):
            return VideoStream(stream, index)
        if stream.get('channels'):
            return AudioStream(stream, index)

        return StreamBase(stream, index)


def _log_stream_table(
    artifacts: list["ExtractionArtifact"],
) -> None:
    """Log a 3-column stream table: wanted, present, artifact name.

    The artifact list is the single source of truth: it drives both the row
    enumeration order (preserving the ffprobe track order established by
    ``_recover()``) and the per-row status. ``TimestampArtifact`` rows are
    included naturally, with no special-case handling.

    Columns (orthogonal — neither influences the other):
    - Want:    ``✔`` if ``artifact.wanted`` else ``✘`` (selection only).
    - Present: ``✔`` if ``artifact.state`` is ``COMPLETE`` else ``✘``
               (completeness only; ``ABSENT`` and ``PARTIAL`` both show ``✘``).
    - Name:    The output filename for the artifact.

    Args:
        artifacts: Internal artifact list produced by ``_recover()`` (includes
                   both wanted and unwanted entries).
    """
    logger.info("Streams:")
    logger.info("Want  Present      Name")
    if not artifacts:
        logger.warning("NO streams found.")
        return

    for artifact in artifacts:
        w_sym = SUCCESS_SYMBOL_MINOR if artifact.wanted else FAILURE_SYMBOL_MINOR
        p_sym = (
            SUCCESS_SYMBOL_MINOR
            if artifact.state == ArtifactState.COMPLETE
            else FAILURE_SYMBOL_MINOR
        )
        logger.info("   %s  %s  \"%s\"", w_sym, p_sym, artifact.path.name)


def streams_filter_plain_regex(
    tracks: list[StreamBase],
    include_pattern: str | None = None,
    exclude_pattern: str | None = None,
    case_sensitive: bool = False,
) -> list[StreamBase]:
    """Filter a list of tracks using include/exclude regex patterns.

    Args:
        tracks: List of stream objects to filter.
        include_pattern: Python regex; only matching tracks are kept.
        exclude_pattern: Python regex; matching tracks are removed.
        case_sensitive: If False (default), matching is case-insensitive.

    Returns:
        Filtered list of stream objects.
    """
    filtered = list(tracks)
    flags = re.NOFLAG if case_sensitive else re.IGNORECASE
    if include_pattern:
        include_re = re.compile(include_pattern, flags)
        filtered = [t for t in filtered if include_re.search(t.display_name())]
    if exclude_pattern:
        exclude_re = re.compile(exclude_pattern, flags)
        filtered = [t for t in filtered if not exclude_re.search(t.display_name())]
    return filtered


class MKVTrackExtractor:
    """High-level extractor orchestrating ffprobe parsing and mkvextract command building.

    Responsibilities:
    - Run ffprobe on the input file.
    - Create typed Stream objects for each discovered stream.
    - Provide formatted display lists for filtering.
    - Build and run mkvextract commands for selected tracks.
    """

    MISSING_STREAM_ID: int = -1
    CHAPTERS_INDEX:    int = -2

    def __init__(self, input_file: str) -> None:
        self.input_file = Path(input_file)
        if not self.input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_file}")

        self.has_chapters: bool = False
        self.tracks: list[StreamBase] = []

        self._run_ffprobe()
        self.format_track_list()

    def _run_ffprobe(self) -> None:
        """Run ffprobe and populate Stream objects from returned JSON data."""
        try:
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_streams',
                '-show_chapters',
                str(self.input_file),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)

            for i, stream in enumerate(data.get('streams', [])):
                self.tracks.append(StreamFactory.create(stream, i))

            if chapters_data := data.get('chapters'):
                self.has_chapters = True
                self.tracks.append(ChaptersStream(chapters_data, self.CHAPTERS_INDEX))

        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"FFprobe error: {e}") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse FFprobe output: {e}") from e

    def format_track_list(self) -> list[str]:
        """Return human-readable strings describing all discovered streams."""
        if not self.tracks:
            return []

        max_track_num = len(self.tracks)
        max_track_id  = max((t.track_id for t in self.tracks), default=0)
        track_num_width = len(str(max_track_num))
        track_id_width  = len(str(max_track_id))

        return [t.display_name(track_num_width, track_id_width) for t in self.tracks]

    def extract_tracks(self, tracks: list[StreamBase], output_dir: Path) -> None:
        """Extract selected tracks to output_dir using mkvextract."""
        cmd: list[str] = ['mkvextract', str(self.input_file)]

        groups: dict[str, list[StreamBase]] = defaultdict(list)
        for track in tracks:
            groups[track.extract_type].append(track)

        for group, group_tracks in groups.items():
            if not group:
                continue
            cmd.append(group)
            for i, track in enumerate(group_tracks, start=1):
                cmd.extend(track.mkvextract_parts(output_dir, i))

        try:
            subprocess.run(cmd, capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"\n--- Error extracting tracks ---\n{e}", file=sys.stderr)
            print(f"\n\n--- Command output ---\n{e.output.decode()}", file=sys.stderr)




def _audio_metadata_from_stream(path: Path, track: "AudioStream") -> AudioMetadata:
    """Build an ``AudioMetadata`` instance from an extracted audio file and its stream info.

    Args:
        path:  Path to the extracted audio file.
        track: ``AudioStream`` parsed from ffprobe output.

    Returns:
        Populated ``AudioMetadata`` instance.
    """
    layout = track.channels_layout

    start_ts: float | None = None
    raw_start = track.start_time
    if isinstance(raw_start, float):
        start_ts = raw_start
    elif isinstance(raw_start, str):
        try:
            start_ts = float(raw_start)
        except (ValueError, TypeError):
            pass

    return AudioMetadata(
        path            = path,
        codec           = track.codec_name or None,
        layout          = layout,
        language        = track.language or None,
        title           = track.title or None,
        start_timestamp = start_ts,
    )




# ---------------------------------------------------------------------------
# ExtractionPhase — Phase object (task 5)
# ---------------------------------------------------------------------------

from typing import TYPE_CHECKING, ClassVar, TypeAlias

from pyqenc.constants import (
    EXTRACTED_DIR,
)
from pyqenc.models import AudioMetadata

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector


_SUBTITLE_FFMPEG_FORMAT: dict[str, str] = {
    "srt": "srt",
    "ssa": "ass",
    "ass": "ass",
}
"""Text subtitle codecs that require an explicit ``-f`` flag when writing to a
``.tmp`` file (ffmpeg cannot infer the format from the extension).
Bitmap subtitle codecs (pgs, sub) are self-describing and do not need ``-f``."""


def _extract_timestamps(
    source:         Path,
    video_track_id: int,
    output:         Path,
    duration_ms:    int | None = None,
) -> None:
    """Extract per-frame PTS values from source and write timestamps.txt.

    Tries ``mkvextract timecodes_v2`` first (native format, already sorted,
    includes the final duration entry). Falls back to ``ffprobe packet=pts``
    for non-MKV containers or when mkvextract is unavailable — in that case
    values are sorted ascending and ``duration_ms`` is appended as the final
    entry (approximating the mkvextract trailing timestamp).

    Both paths use the ``.tmp``-then-rename protocol for atomicity.

    Args:
        source:         Path to the source video file.
        video_track_id: The ffprobe stream index of the video track.
        output:         Destination path (``extracted/timestamps.txt``).
        duration_ms:    Source video duration in milliseconds, used as the
                        trailing entry in the ffprobe fallback path.  When
                        ``None`` the trailing entry is omitted.

    Raises:
        subprocess.CalledProcessError: If both mkvextract and ffprobe fail.
        ValueError: If ffprobe output is empty or contains unparseable lines.
        OSError: If the output file cannot be written.
    """
    tmp = output.parent / f"{output.stem}{TEMP_SUFFIX}"
    output.parent.mkdir(parents=True, exist_ok=True)

    # --- Attempt 1: mkvextract timecodes_v2 ---
    mkvextract_cmd: list[str | os.PathLike] = [
        "mkvextract", source,
        "timecodes_v2", f"0:{tmp}",
    ]
    logger.debug("Extracting timestamps via mkvextract: %s", source.name)
    try:
        subprocess.run(mkvextract_cmd, capture_output=True, check=True)
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(output)
            logger.debug("Timestamps written via mkvextract → %s", output.name)
            return
        tmp.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(1, mkvextract_cmd)
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug("mkvextract timecodes_v2 failed (%s), falling back to ffprobe", exc)
        tmp.unlink(missing_ok=True)

    # --- Attempt 2: ffprobe packet=pts fallback ---
    ffprobe_cmd: list[str | os.PathLike] = [
        "ffprobe", "-v", "error",
        "-select_streams", str(video_track_id),
        "-show_packets",
        "-show_entries", "packet=pts",
        "-of", "csv=print_section=0",
        source,
    ]
    logger.debug("Extracting timestamps via ffprobe: %s", source.name)
    try:
        result = subprocess.run(ffprobe_cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        logger.critical(
            "ffprobe failed extracting timestamps (exit %d): %s",
            exc.returncode, (exc.stderr or "").strip(),
        )
        raise

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        msg = f"ffprobe returned empty output for timestamps from {source.name}"
        logger.critical(msg)
        raise ValueError(msg)

    pts_ms: list[int] = []
    for line in lines:
        try:
            pts_ms.append(int(line))
        except (ValueError, OverflowError) as exc:
            msg = f"Unparseable PTS line from ffprobe: {line!r} — {exc}"
            logger.critical(msg)
            raise ValueError(msg) from exc

    pts_ms.sort()

    # Append duration as trailing entry (approximates mkvextract's final timestamp)
    if duration_ms is not None and (not pts_ms or duration_ms > pts_ms[-1]):
        pts_ms.append(duration_ms)

    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("# timestamp format v2\n")
        for ms in pts_ms:
            fh.write(f"{ms}\n")
    tmp.replace(output)
    logger.debug("Timestamps written via ffprobe fallback: %d entries → %s", len(pts_ms), output.name)


@dataclass
class VideoArtifact(Artifact):
    """Extraction artifact for a video stream.

    Attributes:
        meta:   Video metadata; populated when ``state`` is ``COMPLETE``.
        stream: Originating source track; carries the ``track_id`` the executor
                needs to extract this artifact without re-probing the source.
    """

    meta:   VideoMetadata | None = None
    stream: "VideoStream | None" = None


@dataclass
class AudioArtifact(Artifact):
    """Extraction artifact for an audio stream.

    Attributes:
        meta:   Audio metadata; populated when ``state`` is ``COMPLETE``.
        stream: Originating source track; carries the ``track_id`` the executor
                needs to extract this artifact without re-probing the source.
    """

    meta:   AudioMetadata | None = None
    stream: "AudioStream | None" = None


@dataclass
class OtherArtifact(Artifact):
    """Extraction artifact for subtitles, chapters, or attachments.

    Attributes:
        stream: Originating source track. The executor dispatches on its
                concrete type (``ChaptersStream`` / ``AttachmentStream`` /
                ``SubtitleStream``) and reads ``track_id`` / ``file_extension``
                to extract this artifact without re-probing the source.
    """

    stream: "StreamBase | None" = None


@dataclass
class TimestampArtifact(Artifact):
    """Extraction artifact for the per-frame PTS timestamp file.

    Path: extracted/timestamps.txt
    States: COMPLETE (file exists and non-empty) or ABSENT only.
    Not subject to include/exclude stream filtering.

    Attributes:
        stream: The video source track whose PTS values are extracted. Carries
                the ``track_id`` the executor passes to timestamp extraction.
    """

    stream: "VideoStream | None" = None


# Type alias for all extraction artifacts — use this in annotations throughout
ExtractionArtifact: TypeAlias = VideoArtifact | AudioArtifact | OtherArtifact | TimestampArtifact


@dataclass
class ExtractionPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying extraction-specific payload.

    Attributes:
        video:           Primary extracted video metadata; ``None`` on failure.
        audio:           List of extracted audio track metadata.
        timestamps_path: Path to the extracted timestamps.txt file; ``None`` when absent.
    """

    video:           VideoMetadata | None = None
    audio:           list[AudioMetadata]  = field(default_factory=list)
    timestamps_path: Path | None          = None


class ExtractionPhase(Phase):
    """Phase object for stream extraction.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the extraction phase.  Wraps the existing ``MKVTrackExtractor`` and
    ``extract_streams`` helpers. The uniform run footprint is inherited from
    :class:`Phase`.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "extraction"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase,)
    _METRIC_KEY: MetricKey = MetricKey.EXTRACTION

    def __init__(
        self,
        config:         "AppConfig",
        phases:         PhaseRegistry | None = None,
        *,
        video_required: bool              = True,
        collector:      "MetricsCollector",
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._video_required: bool = video_required

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the source path and the active include/exclude filter."""
        logger.info("Source:   %s", self._dep(JobPhase).result.source.name)  # type: ignore[union-attr]
        extraction_cfg = self._dep(JobPhase).result.config.extraction  # type: ignore[union-attr]
        if extraction_cfg.include or extraction_cfg.exclude:
            logger.info("Filter:")
            if extraction_cfg.include:
                logger.info("  Include:  %s", extraction_cfg.include)
            if extraction_cfg.exclude:
                logger.info("  Exclude:  %s", extraction_cfg.exclude)

    def _recover(self) -> Recovery:
        """Classify extraction artifacts by enumerating every source track.

        Steps:
        1. If ``force_wipe``: delete ``extracted/``.
        2. Clean up leftover ``.tmp`` files.
        3. Analyse the source and produce one artifact per ffprobe track, in
           index order. Each artifact carries two orthogonal facts:
           - ``wanted``: whether the current include/exclude filter selects the
             track (and ``False`` for video/timestamp artifacts when
             ``video_required`` is ``False``). A track dropped by a filter change
             surfaces naturally as ``wanted=False`` rather than a STALE state.
           - ``state``: ``COMPLETE`` when the component is present in the single
             on-disk listing, otherwise ``ABSENT``.
           A ``TimestampArtifact`` row is appended under the same rules.

        Returns:
            The :class:`Recovery` single source of truth; the artifact list
            contains every track (including ``wanted=False`` ones). Metadata
            stash fields (``_recovered_video`` / ``_recovered_audio``) hold only
            selected (``wanted=True``) tracks for result payloads.

        Raises:
            RecoveryError: When the source cannot be analysed at all.
        """
        work_dir      = self._dep(JobPhase).result.work_dir  # type: ignore[union-attr]
        extracted_dir = work_dir / EXTRACTED_DIR
        force_wipe    = getattr(self._dep(JobPhase).result, "force_wipe", False)  # type: ignore[union-attr]

        # Step 1: force-wipe
        if force_wipe and extracted_dir.exists():
            shutil.rmtree(extracted_dir)
            logger.debug("force_wipe: deleted %s", extracted_dir)

        # Step 2: clean up .tmp files
        if extracted_dir.exists():
            for tmp in extracted_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Filter changes are not a special case: a track excluded by the current
        # filter simply becomes ``wanted=False``; if its file is still on disk it
        # surfaces as ``wanted=False, state=COMPLETE`` (there is no STALE state).
        # Recovery derives ``wanted`` purely from the current filter selection, so
        # nothing needs to be persisted between runs.

        # Step 3: analyse the source and enumerate ALL tracks.
        try:
            extractor = MKVTrackExtractor(str(self._dep(JobPhase).result.source))  # type: ignore[union-attr]
        except Exception as exc:
            raise RecoveryError(f"Failed to analyse source video: {exc}") from exc

        selected_tracks = streams_filter_plain_regex(
            extractor.tracks,
            include_pattern = self._dep(JobPhase).result.config.extraction.include,  # type: ignore[union-attr]
            exclude_pattern = self._dep(JobPhase).result.config.extraction.exclude,  # type: ignore[union-attr]
        )

        # Single on-disk listing shared by every artifact's completeness check.
        if extracted_dir.exists():
            on_disk_names = {
                f.name for f in extracted_dir.iterdir()
                if f.is_file() and not f.name.endswith(TEMP_SUFFIX)
            }
        else:
            on_disk_names = set()

        artifacts: list[ExtractionArtifact] = []
        primary_video: VideoMetadata | None = None
        audio_list: list[AudioMetadata] = []

        # Enumerate every track in ffprobe index order; selection drives ``wanted``
        # and the single on-disk listing drives completeness — the two are orthogonal.
        for track in extractor.tracks:
            name   = track.display_name()
            path   = extracted_dir / name
            wanted = track in selected_tracks
            if not self._video_required and track.codec_type == "video":
                wanted = False

            present = name in on_disk_names
            state   = ArtifactState.COMPLETE if present else ArtifactState.ABSENT

            if track.codec_type == "video":
                vm = VideoMetadata(path=path) if present else None
                if vm is not None and wanted and primary_video is None:
                    primary_video = vm
                artifacts.append(VideoArtifact(
                    path=path, state=state, wanted=wanted, meta=vm,
                    stream=cast(VideoStream, track),
                ))
            elif track.codec_type == "audio":
                am: AudioMetadata | None = None
                if present:
                    am = _audio_metadata_from_stream(path, track)  # type: ignore[arg-type]
                    if wanted:
                        audio_list.append(am)
                artifacts.append(AudioArtifact(
                    path=path, state=state, wanted=wanted, meta=am,
                    stream=cast(AudioStream, track),
                ))
            else:
                artifacts.append(OtherArtifact(
                    path=path, state=state, wanted=wanted, stream=track,
                ))

        # TimestampArtifact — enumerated and shown in the table like any other
        # stream, but ``wanted`` only when video is required (same treatment as
        # video streams in audio-only mode: shown as unwanted, never extracted).
        # Carries the first video track, whose PTS the executor extracts.
        first_video: VideoStream | None = next(
            (cast(VideoStream, t) for t in extractor.tracks if t.codec_type == "video"),
            None,
        )
        timestamps_file = extracted_dir / TIMESTAMPS_FILENAME
        ts_present      = TIMESTAMPS_FILENAME in on_disk_names
        artifacts.append(TimestampArtifact(
            path   = timestamps_file,
            state  = ArtifactState.COMPLETE if ts_present else ArtifactState.ABSENT,
            wanted = self._video_required,
            stream = first_video,
        ))

        # Emit stream table — artifacts are the single source of truth
        _log_stream_table(artifacts)
        return Recovery.from_artifacts(artifacts)

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[ExtractionArtifact],
        message:   str,
        error:     str | None = None,
    ) -> ExtractionPhaseResult:
        """Assemble an ``ExtractionPhaseResult`` deriving payloads from artifacts.

        ``video`` / ``audio`` / ``timestamps_path`` are computed from the given
        artifacts' current metas and states — the single source of truth — so
        every path (dry-run, reused, executed, failed) derives identical
        payloads. Empty artifact lists (dependency short-circuits) naturally
        yield ``video=None, audio=[]``.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list.
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result.
        """
        video = next(
            (a.meta for a in artifacts if isinstance(a, VideoArtifact) and a.meta is not None),
            None,
        )
        audio = [a.meta for a in artifacts if isinstance(a, AudioArtifact) and a.meta is not None]
        ts    = next((a for a in artifacts if isinstance(a, TimestampArtifact)), None)
        return ExtractionPhaseResult(
            outcome         = outcome,
            artifacts       = artifacts,
            message         = message,
            error           = error,
            video           = video,
            audio           = audio,
            timestamps_path = ts.path if ts is not None and ts.state == ArtifactState.COMPLETE else None,
        )

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for the extraction phase.

        When ``ctx.deep_cleanup`` is ``True``, deletes this phase's own
        ``extracted/`` directory in full. Every extraction artifact (video,
        audio, subtitles, chapters, attachments, timestamps) is reproducible
        from the source, so none is exempt from deep cleanup. Deletion is guarded
        by an existence check and never raises: any ``OSError`` is caught and logged
        as a warning so a cleanup failure never fails the run.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        if not ctx.deep_cleanup:
            return
        job = self._dep(JobPhase)
        if job.result is None:
            return
        extracted_dir = job.result.work_dir / EXTRACTED_DIR
        if extracted_dir.exists():
            try:
                shutil.rmtree(extracted_dir)
                logger.debug("deep cleanup: deleted %s", extracted_dir)
            except OSError as exc:
                logger.warning("deep cleanup: could not delete %s: %s", extracted_dir, exc)

    def _execute(
        self,
        wanted:    list[ExtractionArtifact],
        dry_run:   bool,
    ) -> ExtractionPhaseResult:
        """Extract the ``ABSENT`` artifacts among the given wanted artifacts.

        Pure executor. It performs NO selection, filtering, re-probing, or
        ``video_required`` gating: ``_recover()`` already decided what is wanted,
        typed each artifact, and attached its originating source stream. This
        method walks the passed-in list (all ``wanted=True``), extracts each
        artifact whose ``state`` is ``ABSENT`` using the concrete artifact type
        plus its carried ``stream``, then updates that artifact's ``state`` and
        ``meta`` in place. No third disk re-scan and no re-derivation of track
        lists occur. ``dry_run`` is never ``True`` here (extraction is not a
        readonly-execute phase; the template previews instead).

        Args:
            wanted:  Wanted artifacts from ``_recover()`` (``wanted=True``),
                     each carrying its source ``stream``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``ExtractionPhaseResult`` built directly from the updated artifacts.
        """
        artifacts = wanted
        work_dir      = self._dep(JobPhase).result.work_dir  # type: ignore[union-attr]
        extracted_dir = work_dir / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)

        source = self._dep(JobPhase).result.source  # type: ignore[union-attr]
        if not source.exists():
            err = f"Source video not found: {source}"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, artifacts, err, error=err)

        errors: list[str] = []

        # Extract only artifacts recovery marked ABSENT; COMPLETE ones are kept
        # as-is. Dispatch on the concrete artifact type — the type already says
        # WHAT to extract, and the carried ``stream`` says HOW (track_id / format).
        for artifact in artifacts:
            if artifact.state != ArtifactState.ABSENT:
                continue
            if isinstance(artifact, TimestampArtifact):
                self._extract_timestamp_artifact(artifact, source, errors)
            elif isinstance(artifact, VideoArtifact):
                self._extract_video_artifact(artifact, source, errors)
            elif isinstance(artifact, AudioArtifact):
                self._extract_audio_artifact(artifact, source, errors)
            elif isinstance(artifact, OtherArtifact):
                self._extract_other_artifact(artifact, source, errors)

        # Build the result directly from the (now updated) wanted artifacts —
        # payloads derive from artifact metas inside ``_make_result``.
        if errors:
            failed_count = sum(1 for a in artifacts if a.state == ArtifactState.ABSENT)
            err_summary  = f"{len(errors)} extraction error(s): {'; '.join(errors)}"
            logger.error(err_summary)
            outcome = PhaseOutcome.FAILED if failed_count > 0 else PhaseOutcome.COMPLETED
            return self._make_result(
                outcome, artifacts, err_summary,
                error=err_summary if outcome == PhaseOutcome.FAILED else None,
            )

        complete_count = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
        logger.info(
            "%s Extraction complete: %d artifact(s) extracted",
            SUCCESS_SYMBOL_MINOR, complete_count,
        )
        logger.info(THICK_LINE)

        return self._make_result(
            PhaseOutcome.COMPLETED, artifacts,
            f"extracted {complete_count} artifact(s)",
        )

    # ------------------------------------------------------------------
    # Per-artifact extractors — pure "how to extract" helpers
    # ------------------------------------------------------------------

    def _extract_timestamp_artifact(
        self,
        artifact: TimestampArtifact,
        source:   Path,
        errors:   list[str],
    ) -> None:
        """Extract per-frame PTS timestamps for the carried video track.

        PTS are read from the source video stream, so a video track is required;
        recovery only marks the timestamp artifact wanted when video is required
        (and thus a video track exists and is carried on ``artifact.stream``).
        Updates ``artifact.state`` to ``COMPLETE`` on success.
        """
        if artifact.stream is None:
            return
        job_source = self._dep(JobPhase).result.job if self._dep(JobPhase).result is not None else None
        source_duration_s: float | None = job_source.source.duration_seconds if job_source is not None else None
        duration_ms: int | None = int(source_duration_s * 1000) if source_duration_s is not None else None
        try:
            _extract_timestamps(source, artifact.stream.track_id, artifact.path, duration_ms=duration_ms)
            if artifact.path.exists() and artifact.path.stat().st_size > 0:
                artifact.state = ArtifactState.COMPLETE
        except Exception as exc:
            err = f"Failed to extract timestamps: {exc}"
            logger.critical(err)
            errors.append(err)

    def _extract_video_artifact(
        self,
        artifact: VideoArtifact,
        source:   Path,
        errors:   list[str],
    ) -> None:
        """Copy the carried video track into the artifact path (matroska)."""
        if artifact.stream is None:
            return
        track = artifact.stream
        cmd: list[str | PathLike] = [
            "ffmpeg", "-i", source,
            "-map", f"0:{track.track_id}",
            "-c", "copy",
            "-f", "matroska",
            artifact.path,
        ]
        logger.debug("Extracting video track %d: %s", track.track_id, artifact.path.name)
        res = run_ffmpeg(cmd, output_file=artifact.path)
        if res.success and artifact.path.exists() and artifact.path.stat().st_size > 0:
            artifact.state = ArtifactState.COMPLETE
            artifact.meta  = VideoMetadata(path=artifact.path)
        else:
            err = f"ffmpeg failed extracting video track {track.track_id}"
            logger.error(err)
            errors.append(err)

    def _extract_audio_artifact(
        self,
        artifact: AudioArtifact,
        source:   Path,
        errors:   list[str],
    ) -> None:
        """Copy the carried audio track into the artifact path."""
        if artifact.stream is None:
            return
        track = artifact.stream
        cmd: list[str | PathLike] = [
            "ffmpeg", "-i", source,
            "-map", f"0:{track.track_id}",
            "-c", "copy",
            artifact.path,
        ]
        logger.debug("Extracting audio track %d: %s", track.track_id, artifact.path.name)
        res = run_ffmpeg(cmd, output_file=artifact.path)
        if res.success and artifact.path.exists() and artifact.path.stat().st_size > 0:
            artifact.state = ArtifactState.COMPLETE
            artifact.meta  = _audio_metadata_from_stream(artifact.path, track)
        else:
            err = f"ffmpeg failed extracting audio track {track.track_id}"
            logger.error(err)
            errors.append(err)

    def _extract_other_artifact(
        self,
        artifact: OtherArtifact,
        source:   Path,
        errors:   list[str],
    ) -> None:
        """Extract a subtitle, chapters, or attachment artifact.

        Dispatches on the carried stream's concrete type. Updates
        ``artifact.state`` to ``COMPLETE`` on success.
        """
        track       = artifact.stream
        output_file = artifact.path
        if track is None:
            logger.warning("OtherArtifact without a source stream: %s", output_file.name)
            return

        if isinstance(track, ChaptersStream):
            # Chapters: try mkvextract first (native MKV XML format).
            # Fall back to ffprobe -show_chapters -print_format xml for
            # non-MKV containers or when mkvextract is unavailable.
            # Both paths use .tmp-then-rename for atomicity.
            tmp = output_file.parent / f"{output_file.stem}{TEMP_SUFFIX}"
            logger.debug("Extracting chapters: %s", output_file.name)

            mkvextract_cmd: list[str | os.PathLike] = [
                "mkvextract", source, "chapters", tmp,
            ]
            try:
                subprocess.run(mkvextract_cmd, capture_output=True, check=True)
                if tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(output_file)
                    artifact.state = ArtifactState.COMPLETE
                    logger.debug("Chapters extracted via mkvextract")
                    return
                raise subprocess.CalledProcessError(1, mkvextract_cmd)
            except (subprocess.CalledProcessError, OSError) as exc:
                logger.debug(
                    "mkvextract chapters failed (%s), falling back to ffprobe", exc,
                )
                # Clean up any partial output from mkvextract
                output_file.unlink(missing_ok=True)
                tmp.unlink(missing_ok=True)

                ffprobe_cmd: list[str | os.PathLike] = [
                    "ffprobe", "-v", "error",
                    "-show_chapters",
                    "-print_format", "xml",
                    source,
                ]
                try:
                    ffprobe_result = subprocess.run(
                        ffprobe_cmd, capture_output=True, text=True, check=True,
                    )
                except subprocess.CalledProcessError as ffprobe_exc:
                    err = f"ffprobe failed extracting chapters (exit {ffprobe_exc.returncode}): {(ffprobe_exc.stderr or '').strip()}"
                    logger.error(err)
                    errors.append(err)
                    return
                comment = "<!-- Extracted by ffprobe -show_chapters -print_format xml (mkvextract not available or not applicable) -->\n"
                tmp.write_text(comment + ffprobe_result.stdout, encoding="utf-8")
                tmp.replace(output_file)
                artifact.state = ArtifactState.COMPLETE
                logger.debug("Chapters extracted via ffprobe fallback")

        elif isinstance(track, AttachmentStream):
            # Attachments: -dump_attachment writes directly — bypass muxer
            cmd: list[str | PathLike] = [
                "ffmpeg", "-i", source,
                f"-dump_attachment:{track.track_id}", output_file,
                "-t", "0", "-f", "null", "-",
            ]
            logger.debug("Extracting attachment track %d: %s", track.track_id, output_file.name)
            # output_file=None: dump_attachment writes directly, .tmp protocol does not apply
            res = run_ffmpeg(cmd, output_file=None)
            if res.success and output_file.exists():
                artifact.state = ArtifactState.COMPLETE
            else:
                err = f"ffmpeg failed extracting attachment track {track.track_id}"
                logger.error(err)
                errors.append(err)

        elif isinstance(track, SubtitleStream):
            # Subtitles: text codecs need explicit -f; bitmap codecs do not
            fmt = _SUBTITLE_FFMPEG_FORMAT.get(track.file_extension)
            cmd = [
                "ffmpeg", "-i", source,
                "-map", f"0:{track.track_id}",
                "-c", "copy",
            ]
            if fmt:
                cmd += ["-f", fmt]
            cmd.append(output_file)
            logger.debug("Extracting subtitle track %d: %s", track.track_id, output_file.name)
            res = run_ffmpeg(cmd, output_file=output_file)
            if res.success and output_file.exists():
                artifact.state = ArtifactState.COMPLETE
            else:
                err = f"ffmpeg failed extracting subtitle track {track.track_id}"
                logger.error(err)
                errors.append(err)

        else:
            logger.warning("Skipping unknown stream type %s: %s", type(track).__name__, output_file.name)

    @staticmethod
    def _outcome_from_artifacts(
        artifacts: list[ExtractionArtifact],
        did_work:  bool,
    ) -> PhaseOutcome:
        """Derive ``PhaseOutcome`` purely from artifact states (mode-free).

        Any ``ABSENT`` or ``PARTIAL`` artifact means wanted work remains, so
        the phase is ``PENDING`` regardless of run mode; the runner owns the
        dry-run vs execute distinction. When every artifact is ``COMPLETE``
        the phase is ``COMPLETED`` (did work) or ``REUSED`` (nothing to do).
        """
        if any(a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL) for a in artifacts):
            return PhaseOutcome.PENDING
        if all(a.state == ArtifactState.COMPLETE for a in artifacts) and artifacts:
            return PhaseOutcome.REUSED if not did_work else PhaseOutcome.COMPLETED
        return PhaseOutcome.PENDING
