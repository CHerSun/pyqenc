"""
Extraction phase for the quality-based encoding pipeline.

This module handles extraction of video and audio streams from source MKV files,
including automatic black border detection and cropping. It also provides the
MKVTrackExtractor and stream model classes (migrated from the legacy pymkvextract
module) for parsing and extracting MKV tracks via ffprobe / mkvextract.
"""

import json
import logging
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, cast

from pyqenc.constants import FAILURE_SYMBOL_MINOR, SUCCESS_SYMBOL_MINOR
from pyqenc.models import AudioMetadata, CropParams, PhaseOutcome, VideoMetadata
from pyqenc.phases.recovery import ExtractionRecovery, recover_extraction
from pyqenc.state import ArtifactState, JobState
from pyqenc.utils.ffmpeg_runner import run_ffmpeg

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
        return ' '.join([
            self._track_ids_string(track_num_width, track_id_width),
            f"({self.codec_type}-{self.codec_name})",
            f"{' '.join(self._tags_formatted)}.{self.file_extension}",
        ])

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
    def channels_layout(self) -> str:
        """Audio channel layout as specified in the file, or empty string."""
        if channel_layout := self.raw.get('channel_layout'):
            return channel_layout
        if channels := self.raw.get('channels'):
            return f"{channels}.0"
        return ''

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
        if 'substation'  in codec_lower: return 'ssa'
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


class TagsStream(StreamBase):
    """Represents the global tags entity as a stream for uniform filtering."""

    extract_type = 'tags'

    def __init__(self, stream: dict, index: int) -> None:  # type: ignore[override]
        self.index = index
        self.raw = stream
        self.tags: dict = {}
        self.disposition: dict = {}

    @property
    def codec_type(self) -> str:
        return 'tags'

    @property
    def file_extension(self) -> str:
        return 'xml'

    def display_name(self, track_num_width: int = 2, track_id_width: int = 2) -> str:
        return f"tags.{self.file_extension}"

    def mkvextract_parts(self, output_path: Path, attachment_index: int | None = None) -> list[str]:
        return [str(output_path / self.display_name())]


class HeadersStream(StreamBase):
    """Represents file-level header information as a stream."""

    extract_type = ''  # no mkvextract command part; written directly to JSON

    def __init__(self, stream: dict, index: int) -> None:  # type: ignore[override]
        self.index = index
        self.raw = stream
        self.tags: dict = {}
        self.disposition: dict = {}

    @property
    def codec_type(self) -> str:
        return 'headers'

    @property
    def file_extension(self) -> str:
        return 'json'

    def display_name(self, track_num_width: int = 2, track_id_width: int = 2) -> str:
        return f"headers.{self.file_extension}"

    def mkvextract_parts(self, output_path: Path, attachment_index: int | None = None) -> list[str]:
        """Dump file info from ffprobe to a JSON file; returns no mkvextract fragments."""
        with (output_path / self.display_name()).open('wt') as f:
            json.dump(self.raw, f, indent=4)
        return []


class StreamFactory:
    """Factory that creates the appropriate *Stream* subclass from an ffprobe stream dict."""

    @staticmethod
    def create(stream: dict, index: int) -> StreamBase:
        """Create a typed Stream object for a single ffprobe stream dict."""
        s = dict(stream)  # shallow copy to avoid mutating caller's dict

        # Treat attached_pic or image/ mimetype as attachment
        if s.get('disposition', {}).get('attached_pic', 0) == 1:
            s['codec_type'] = 'attachment'
        elif (s.get('tags', {}) or {}).get('mimetype', '').startswith('image/'):
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


def _print_stream_table(
    all_tracks:      list[StreamBase],
    selected_tracks: list[StreamBase],
) -> None:
    """Print a compact aligned table of all streams with include/exclude symbols.

    Each row shows ``✔`` for included streams and ``✗`` for excluded streams,
    followed by the would-be output filename.  Columns are vertically aligned.

    Args:
        all_tracks:      All streams discovered in the source file.
        selected_tracks: Streams that passed the include/exclude filters.
    """
    if not all_tracks:
        return

    selected_set = set(id(t) for t in selected_tracks)

    max_track_num = len(all_tracks)
    max_track_id  = max((t.track_id for t in all_tracks), default=0)
    track_num_width = len(str(max_track_num))
    track_id_width  = len(str(max(max_track_id, 0)))

    lines: list[str] = []
    for track in all_tracks:
        symbol   = SUCCESS_SYMBOL_MINOR if id(track) in selected_set else FAILURE_SYMBOL_MINOR
        filename = track.display_name(track_num_width, track_id_width)
        lines.append(f"  {symbol}  {filename}")

    logger.info("Streams:\n%s", "\n".join(lines))


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
    TAGS_INDEX:        int = -3
    FILE_INFO_INDEX:   int = -4

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
                '-show_format',
                str(self.input_file),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)

            for i, stream in enumerate(data.get('streams', [])):
                self.tracks.append(StreamFactory.create(stream, i))

            if chapters_data := data.get('chapters'):
                self.has_chapters = True
                self.tracks.append(ChaptersStream(chapters_data, self.CHAPTERS_INDEX))

            if file_info_data := data.get('format', {}).get('tags'):
                self.has_file_info = True
                self.tracks.append(HeadersStream(file_info_data, self.FILE_INFO_INDEX))

            # Tags stream is always appended; no reliable way to detect absence via ffprobe.
            self.tracks.append(TagsStream({}, self.TAGS_INDEX))

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


# ---------------------------------------------------------------------------
# Pipeline extraction helpers
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Result of stream extraction phase.

    Attributes:
        video:   Extracted video metadata (single stream); ``crop_params`` is
                 always populated after extraction (all-zero if no borders found).
        audio:   List of extracted audio track metadata.
        outcome: Phase outcome (COMPLETED, REUSED, DRY_RUN, or FAILED).
        error:   Error message if extraction failed.
    """

    video:   VideoMetadata | None
    audio:   list[AudioMetadata]
    outcome: PhaseOutcome
    error:   str | None = None


def _detect_crop_parameters(
    video_file: VideoMetadata,
    sample_count: int = 50,
) -> CropParams:
    """Detect black borders using ffmpeg cropdetect filter.

    Samples multiple frames across the video to find conservative crop parameters
    that remove all black borders while preserving maximum content area.

    Always returns a ``CropParams`` instance — all-zero if no borders are found
    or if detection fails for any reason.

    Args:
        video_file:   Video metadata for the file to analyse.
        sample_count: Number of frames to sample across the video.

    Returns:
        CropParams with detected border offsets; all-zero if no cropping needed.
    """
    logger.debug("Detecting black borders in %s...", video_file.path)

    try:
        duration = video_file.duration_seconds

        if not duration:
            logger.warning("Duration is not available, skipping crop detection")
            return CropParams()

        # Distribute samples across the middle 80 % of the video
        start_time  = duration * 0.1
        step        = duration * 0.8 / (sample_count - 1) if sample_count > 1 else 0
        step_frames = (
            int(video_file.frame_count * 0.8 / sample_count) if video_file.frame_count
            else int(step * video_file.fps)                   if video_file.fps
            else 0
        )
        step_frames = max(min(step_frames, 500), 30)

        cmd: list[str | PathLike] = [
            "ffmpeg",
            "-ss", str(start_time),
            "-i", video_file.path,
            "-vf", f"select='not(mod(n\\,{step_frames}))',cropdetect=24:16:0",
            "-vframes", str(sample_count),
            "-f", "null",
            "-",
        ]

        result = run_ffmpeg(cmd, output_file=None)

        # Parse lines like: [Parsed_cropdetect_0 @ ...] w:1920 h:800 x:0 y:140
        crop_detections: list[tuple[int, int, int, int]] = []
        for line in result.stderr_lines:
            if "cropdetect" in line and "x1:" in line:
                match = re.search(r"w:(\d+)\s+h:(\d+)\s+x:(\d+)\s+y:(\d+)", line)
                if match:
                    w, h, x, y = map(int, match.groups())
                    crop_detections.append((w, h, x, y))

        logger.debug("Got %d crop samples.", len(crop_detections))

        if not crop_detections:
            logger.debug("No black borders detected")
            return CropParams()

        # Most conservative crop: smallest detected content area
        max_w = max(d[0] for d in crop_detections)
        max_h = max(d[1] for d in crop_detections)
        min_x = min(d[2] for d in crop_detections)
        min_y = min(d[3] for d in crop_detections)

        left = min_x
        top  = min_y

        width, height = video_file.resolution.split("x", 1) if video_file.resolution else ("0", "0")
        right  = int(width)  - max_w - left
        bottom = int(height) - max_h - top

        if max(left, right, top, bottom) < 2:
            logger.debug("Detected borders too small, no cropping needed")
            return CropParams()

        crop = CropParams(top=top, bottom=bottom, left=left, right=right)
        logger.info(
            "Detected black borders: %d top, %d bottom, %d left, %d right (content %dx%d)",
            top, bottom, left, right, max_w, max_h,
        )
        return crop

    except Exception as e:
        logger.warning("Failed to detect crop parameters: %s", e)
        return CropParams()


def _audio_metadata_from_stream(path: Path, track: "AudioStream") -> AudioMetadata:
    """Build an ``AudioMetadata`` instance from an extracted audio file and its stream info.

    Args:
        path:  Path to the extracted audio file.
        track: ``AudioStream`` parsed from ffprobe output.

    Returns:
        Populated ``AudioMetadata`` instance.
    """
    channels: int | None = None
    raw_channels = track.raw.get("channels")
    if raw_channels is not None:
        try:
            channels = int(raw_channels)
        except (ValueError, TypeError):
            pass

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
        channels        = channels,
        language        = track.language or None,
        title           = track.title or None,
        start_timestamp = start_ts,
    )


def extract_streams(
    source_video: Path,
    output_dir:   Path,
    include:      str | None = None,
    exclude:      str | None = None,
    detect_crop:  bool = True,
    manual_crop:  str | None = None,
    force:        bool = False,
    dry_run:      bool = False,
    job:          JobState | None = None,
) -> ExtractionResult:
    """Extract video and audio streams from source MKV file.

    Integrates with pymkvextract for stream extraction and implements
    automatic black border detection for cropping.  The returned
    ``ExtractionResult.video.crop_params`` is always a ``CropParams``
    instance after this call — all-zero if no borders were found.

    Performs extraction phase recovery first: cleans up leftover ``.tmp``
    files and checks whether extracted artifacts already exist.  If all
    artifacts are present (``ArtifactState.COMPLETE``), extraction is skipped
    and existing files are reused.

    On subsequent runs the persisted ``include``/``exclude`` values from
    ``extraction.yaml`` are compared against the current values.  If they
    differ, a warning is logged and the phase is marked as needing re-execution.

    Args:
        source_video: Path to source MKV file.
        output_dir:   Directory for extracted streams.
        include:      Regex pattern applied to ALL stream types; only streams
                      whose would-be output filename matches are extracted.
                      ``None`` means include all.
        exclude:      Regex pattern applied to ALL stream types; streams whose
                      would-be output filename matches are skipped even if they
                      also match ``include``.  ``None`` means exclude none.
        detect_crop:  If ``True``, automatically detect black borders.
        manual_crop:  Manual crop parameters (format: ``"top bottom [left right]"``).
        force:        If ``False``, reuse existing extracted files.
        dry_run:      If ``True``, only report status without performing extraction.
        job:          Current job state for recovery context (optional).

    Returns:
        ``ExtractionResult`` with the first video stream (``crop_params`` populated),
        a list of ``AudioMetadata`` objects, and a ``PhaseOutcome``.
    """
    logger.info("Extraction phase: %s", source_video.name)

    if not source_video.exists():
        error_msg = f"Source video not found: {source_video}"
        logger.critical(error_msg)
        return ExtractionResult(
            video=None, audio=[], outcome=PhaseOutcome.FAILED, error=error_msg,
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        extractor = MKVTrackExtractor(str(source_video))
    except Exception as e:
        error_msg = f"Failed to analyse source video: {e}"
        logger.critical(error_msg)
        return ExtractionResult(
            video=None, audio=[], outcome=PhaseOutcome.FAILED, error=error_msg,
        )

    # Apply unified include/exclude filter to ALL stream types in one call (Req 10.1–10.4)
    selected_tracks = streams_filter_plain_regex(
        extractor.tracks,
        include_pattern=include,
        exclude_pattern=exclude,
    )

    # Compact stream display — shown in both dry-run and execute modes (Req 10.5, 10.6)
    _print_stream_table(extractor.tracks, selected_tracks)

    video_tracks: list[VideoStream] = [t for t in selected_tracks if t.codec_type == "video"]  # type: ignore[assignment]
    audio_tracks: list[AudioStream] = [t for t in selected_tracks if t.codec_type == "audio"]  # type: ignore[assignment]
    other_tracks: list[StreamBase]  = [t for t in selected_tracks if t.codec_type not in ("video", "audio")]

    if not video_tracks:
        error_msg = "No video streams found matching filters"
        logger.critical(error_msg)
        return ExtractionResult(
            video=None, audio=[], outcome=PhaseOutcome.FAILED, error=error_msg,
        )

    expected_video_files = [output_dir / t.display_name() for t in video_tracks]
    expected_audio_files = [output_dir / t.display_name() for t in audio_tracks]
    expected_other_files = [output_dir / t.display_name() for t in other_tracks
                            if t.extract_type]  # skip HeadersStream (extract_type="")
    video_metas = [
        VideoMetadata.from_stream(path=f, stream=t)
        for f, t in zip(expected_video_files, video_tracks)
    ]

    # ------------------------------------------------------------------
    # Helper: resolve crop params (manual takes priority over detection)
    # ------------------------------------------------------------------
    def _resolve_crop(video_meta: VideoMetadata) -> CropParams:
        if manual_crop:
            try:
                crop = CropParams.parse(manual_crop)
                logger.info("Using manual crop: %s", crop)
                return crop
            except ValueError as e:
                logger.warning("Invalid manual crop format: %s — skipping crop", e)
                return CropParams()
        if detect_crop:
            return _detect_crop_parameters(video_meta)
        return CropParams()

    # ------------------------------------------------------------------
    # Recovery: clean up .tmp files and check existing artifacts (Req 3.2, 4.1, 7.7)
    # ------------------------------------------------------------------
    _job = job or JobState(source=VideoMetadata(path=source_video))
    recovery = recover_extraction(work_dir=output_dir.parent, job=_job)

    # ------------------------------------------------------------------
    # Filter change detection via extraction.yaml sidecar (Req 10.11, 10.12)
    # ------------------------------------------------------------------
    from pyqenc.state import ExtractionParams, JobStateManager
    _state_manager = JobStateManager(work_dir=output_dir.parent, source_video=source_video)
    persisted_params = _state_manager.load_extraction()
    if persisted_params is not None:
        if persisted_params.include != include or persisted_params.exclude != exclude:
            logger.warning(
                "Stream filter change detected — extraction needs re-execution. "
                "Persisted: include=%r, exclude=%r. Current: include=%r, exclude=%r. "
                "Re-run with --force to re-extract with the new filters.",
                persisted_params.include, persisted_params.exclude,
                include, exclude,
            )
            # Mark phase as needing re-execution by treating artifacts as absent
            recovery = type(recovery)(video_files=recovery.video_files, state=ArtifactState.ABSENT)

    # ------------------------------------------------------------------
    # Reuse path — all artifacts present (COMPLETE via .tmp protocol)
    # ------------------------------------------------------------------
    all_exist = (
        not force
        and recovery.state == ArtifactState.COMPLETE
        and all(f.exists() for f in expected_video_files + expected_audio_files + expected_other_files)
    )
    if all_exist:
        primary_video = video_metas[0]
        if manual_crop:
            primary_video.crop_params = _resolve_crop(primary_video)
        elif job is not None and job.source.crop_params is not None:
            # Crop was already detected and persisted in job.yaml — reuse it
            primary_video.crop_params = job.source.crop_params
            logger.info("Reusing previously detected crop from job.yaml: %s", primary_video.crop_params)
        elif detect_crop:
            # No cached crop — run detection now
            primary_video.crop_params = _detect_crop_parameters(primary_video)
        else:
            primary_video.crop_params = CropParams()
        audio_meta = [_audio_metadata_from_stream(path, track)
                      for path, track in zip(expected_audio_files, audio_tracks)]
        return ExtractionResult(video=primary_video, audio=audio_meta, outcome=PhaseOutcome.REUSED)

    # ------------------------------------------------------------------
    # Dry-run path
    # ------------------------------------------------------------------
    if dry_run:
        if manual_crop:
            logger.info("[DRY-RUN] Would use manual crop: %s", manual_crop)
        elif detect_crop:
            logger.info("[DRY-RUN] Would detect black borders automatically")

        primary_video = video_metas[0] if video_metas else None
        audio_meta = [
            _audio_metadata_from_stream(path, track)
            for path, track in zip(expected_audio_files, audio_tracks)
        ]
        return ExtractionResult(
            video=primary_video, audio=audio_meta, outcome=PhaseOutcome.DRY_RUN,
        )

    # ------------------------------------------------------------------
    # Extraction path
    # ------------------------------------------------------------------
    logger.info("Extracting streams...")
    try:
        for track in video_tracks:
            output_file = output_dir / track.display_name()
            cmd: list[str | PathLike] = [
                "ffmpeg",
                "-i", source_video,
                "-map", f"0:{track.track_id}",
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-y",
                output_file,
            ]
            logger.debug("Extracting video track %d: %s", track.track_id, output_file.name)
            video_result = run_ffmpeg(cmd, output_file=output_file)
            if not video_result.success:
                raise RuntimeError(
                    f"ffmpeg failed (exit {video_result.returncode}) extracting video track {track.track_id}"
                )

        for track in audio_tracks:
            output_file = output_dir / track.display_name()
            cmd = [
                "ffmpeg",
                "-i", source_video,
                "-map", f"0:{track.track_id}",
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-y",
                output_file,
            ]
            logger.debug("Extracting audio track %d: %s", track.track_id, output_file.name)
            audio_result = run_ffmpeg(cmd, output_file=output_file)
            if not audio_result.success:
                raise RuntimeError(
                    f"ffmpeg failed (exit {audio_result.returncode}) extracting audio track {track.track_id}"
                )

        if other_tracks:
            logger.debug("Extracting %d other track(s) via mkvextract", len(other_tracks))
            extractor.extract_tracks(other_tracks, output_dir)

        # Persist include/exclude filters in extraction.yaml sidecar (Req 10.11)
        try:
            _state_manager.save_extraction(ExtractionParams(include=include, exclude=exclude))
        except Exception as e:
            logger.warning("Could not persist extraction.yaml: %s", e)

    except RuntimeError as e:
        error_msg = str(e)
        logger.critical(error_msg)
        return ExtractionResult(
            video=None, audio=[], outcome=PhaseOutcome.FAILED, error=error_msg,
        )
    except Exception as e:
        error_msg = f"Failed to extract streams: {e}"
        logger.critical(error_msg)
        return ExtractionResult(
            video=None, audio=[], outcome=PhaseOutcome.FAILED, error=error_msg,
        )

    primary_video = video_metas[0]
    if detect_crop and not manual_crop:
        logger.info("Detecting black borders...")
    primary_video.crop_params = _resolve_crop(primary_video)
    audio_meta = [
        _audio_metadata_from_stream(path, track)
        for path, track in zip(expected_audio_files, audio_tracks)
    ]
    return ExtractionResult(
        video=primary_video, audio=audio_meta, outcome=PhaseOutcome.COMPLETED,
    )
