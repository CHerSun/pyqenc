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

from pyqenc.models import CropParams, VideoMetadata

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
        video_files: List of extracted video file paths
        audio_files: List of extracted audio file paths
        crop_params: Detected or manual crop parameters (None if no cropping)
        reused: True if existing files were reused
        needs_work: True if extraction would be performed (dry-run mode)
        success: True if extraction succeeded or files already exist
        error: Error message if extraction failed
    """

    video_files: list[VideoMetadata]
    audio_files: list[Path]
    crop_params: CropParams | None
    reused: bool
    needs_work: bool
    success: bool
    error: str | None = None


def _detect_crop_parameters(
    video_file: VideoMetadata,
    sample_count: int = 50,
) -> CropParams | None:
    """Detect black borders using ffmpeg cropdetect filter.

    Samples multiple frames across the video to find conservative crop parameters
    that remove all black borders while preserving maximum content area.

    Args:
        video_file: Path to video file to analyze
        sample_count: Number of frames to sample across video
        skip_seconds: Seconds to skip at start (avoid intro/credits)

    Returns:
        CropParams if borders detected, None if no cropping needed
    """
    logger.debug(f"Detecting black borders in {video_file.path}...")

    try:
        duration = video_file.duration_seconds

        # Handle missing duration
        if not duration:
            logger.warning("Duration is not available, skipping crop detection")
            return None

        # Calculate sample positions (skip first skip_seconds, then distribute evenly)
        start_time = duration * 0.1
        step = duration*0.8 / (sample_count - 1) if sample_count > 1 else 0
        step_frames = int(video_file.frame_count*0.8 / sample_count) if video_file.frame_count else int(step * video_file.fps) if video_file.fps else 0
        step_frames = min(step_frames, 500)  # Cap step_frames to avoid excessively long processing on long videos with missing frame count/fps
        step_frames = max(step_frames, 30)   # Cap minimum value too.

        # Collect crop detections from all samples
        crop_detections = []
        # Run cropdetect at this position
        cmd: list[str|PathLike] = [
            "ffmpeg",
            "-ss", str(start_time),
            "-i", video_file.path,
            "-vf", f"select='not(mod(n\\,{step_frames}))',cropdetect=24:16:0",  # select every step_frames' frame, detect with threshold=24, round=16, reset=0
            "-vframes", str(sample_count),              # analyze 5 frames to get a few outputs
            "-f", "null",
            "-"
        ]

# ffmpeg -i "D:\\_current\\pyqenc2\\extracted\\#0 ID=0 (video-h264) res=1920x1080.mkv" -vf "select='not(mod(n\,500))',cropdetect=24:16:0" -vframes 100 -f null -
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False  # cropdetect writes to stderr, not an error
        )

        # Parse cropdetect output from stderr
        # Looking for lines like: [Parsed_cropdetect_0 @ ...] x1:0 x2:1919 y1:140 y2:939
        for line in result.stderr.split('\n'):
            if 'cropdetect' in line and 'x1:' in line:
                # Extract crop values
                match = re.search(r'w:(\d+)\s+h:(\d+)\s+x:(\d+)\s+y:(\d+)', line)
                if match:
                    w, h, x, y = map(int, match.groups())
                    crop_detections.append((w, h, x, y))
        logger.debug(f"Got {len(crop_detections)} samples.")

        if not crop_detections:
            logger.debug("No black borders detected")
            return None

        # Find most conservative crop (largest area that removes all borders)
        # This means: max x1, min x2, max y1, min y2
        max_w = max(d[0] for d in crop_detections)
        max_h = max(d[1] for d in crop_detections)
        min_x = min(d[2] for d in crop_detections)
        min_y = min(d[3] for d in crop_detections)

        # Convert to top/bottom/left/right format
        left = min_x
        right = 0
        top = min_y
        bottom = 0

        # Get video dimensions to calculate right and bottom
        width, height = video_file.resolution.split('x', 1) if video_file.resolution else (0, 0)
        right = int(width) - max_w - left
        bottom = int(height) - max_h - top

        # Check if crop is significant (at least 2 pixels on any side)
        if max(left, right, top, bottom) < 2:
            logger.debug("Detected borders too small, no cropping needed")
            return CropParams(top=0, bottom=0, left=0, right=0)

        crop = CropParams(top=top, bottom=bottom, left=left, right=right)
        logger.info(
            f"Detected black borders: {top} top, {bottom} bottom, "
            f"{left} left, {right} right "
            f"(width: {max_w}, height: {max_h})"
        )

        return crop

    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to detect crop parameters: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error during crop detection: {e}")
        return None


def extract_streams(
    source_video: Path,
    output_dir: Path,
    video_filter: str | None = None,
    audio_filter: str | None = None,
    detect_crop: bool = True,
    manual_crop: str | None = None,
    force: bool = False,
    dry_run: bool = False
) -> ExtractionResult:
    """Extract video and audio streams from source MKV file.

    Integrates with pymkvextract for stream extraction and implements
    automatic black border detection for cropping.

    Args:
        source_video: Path to source MKV file
        output_dir: Directory for extracted streams
        video_filter: Regex pattern to include video streams (e.g., ".*eng.*")
        audio_filter: Regex pattern to include audio streams (e.g., ".*eng.*")
        detect_crop: If True, automatically detect black borders
        manual_crop: Manual crop parameters (format: "top bottom [left right]")
        force: If False, reuse existing extracted files
        dry_run: If True, only report status without performing extraction

    Returns:
        ExtractionResult with paths to extracted files and crop parameters

    Note:
        Filters are applied to stream metadata (language, title, codec).
        Crop format: "top bottom" or "top bottom left right"
    """
    logger.info(f"Extraction phase: {source_video.name}")

    # Validate source video exists
    if not source_video.exists():
        error_msg = f"Source video not found: {source_video}"
        logger.critical(error_msg)
        return ExtractionResult(
            video_files=[],
            audio_files=[],
            crop_params=None,
            reused=False,
            needs_work=False,
            success=False,
            error=error_msg
        )

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize extractor
    try:
        extractor = MKVTrackExtractor(str(source_video))
    except Exception as e:
        error_msg = f"Failed to analyze source video: {e}"
        logger.critical(error_msg)
        return ExtractionResult(
            video_files=[],
            audio_files=[],
            crop_params=None,
            reused=False,
            needs_work=False,
            success=False,
            error=error_msg
        )

    # Apply filters to select tracks
    selected_tracks = extractor.tracks

    # Filter video streams
    if video_filter:
        video_tracks = [t for t in selected_tracks if t.codec_type == 'video']
        filtered_video = streams_filter_plain_regex(
            video_tracks,
            include_pattern=video_filter,
            exclude_pattern=None
        )
        # Keep non-video tracks and filtered video tracks
        selected_tracks = [t for t in selected_tracks if t.codec_type != 'video'] + filtered_video

    # Filter audio streams
    if audio_filter:
        audio_tracks = [t for t in selected_tracks if t.codec_type == 'audio']
        filtered_audio = streams_filter_plain_regex(
            audio_tracks,
            include_pattern=audio_filter,
            exclude_pattern=None
        )
        # Keep non-audio tracks and filtered audio tracks
        selected_tracks = [t for t in selected_tracks if t.codec_type != 'audio'] + filtered_audio

    # Separate video and audio tracks
    video_tracks = [t for t in selected_tracks if t.codec_type == 'video']
    audio_tracks = [t for t in selected_tracks if t.codec_type == 'audio']

    if not video_tracks:
        error_msg = "No video streams found matching filters"
        logger.critical(error_msg)
        return ExtractionResult(
            video_files=[],
            audio_files=[],
            crop_params=None,
            reused=False,
            needs_work=False,
            success=False,
            error=error_msg
        )

    logger.info(f"Selected {len(video_tracks)} video stream(s) and {len(audio_tracks)} audio stream(s)")

    # Check for existing extracted files
    expected_video_files = [output_dir / t.display_name() for t in video_tracks]
    expected_audio_files = [output_dir / t.display_name() for t in audio_tracks]
    video_metas = [VideoMetadata.from_stream(path=f, stream=t)
                   for f, t in zip(expected_video_files, video_tracks)]

    all_exist = all(f.exists() for f in expected_video_files + expected_audio_files)

    if all_exist and not force:
        logger.info("Extracted files already exist, reusing")

        # Still need to handle crop detection/parsing
        crop_params = None
        if manual_crop:
            try:
                crop_params = CropParams.parse(manual_crop)
                logger.info(f"Using manual crop: {crop_params}")
            except ValueError as e:
                logger.warning(f"Invalid manual crop format: {e}, skipping crop")
        elif detect_crop and expected_video_files:
            # Detect crop on first video file
            crop_params = _detect_crop_parameters(video_metas[0])

        return ExtractionResult(
            video_files=expected_video_files,
            audio_files=expected_audio_files,
            crop_params=crop_params,
            reused=True,
            needs_work=False,
            success=True,
            error=None
        )

    # Dry-run mode: report what would be done
    if dry_run:
        logger.info("[DRY-RUN] Would extract:")
        for track in video_tracks:
            logger.info(f"[DRY-RUN]   Video: {track.display_name()}")
        for track in audio_tracks:
            logger.info(f"[DRY-RUN]   Audio: {track.display_name()}")

        crop_params = None
        if manual_crop:
            try:
                crop_params = CropParams.parse(manual_crop)
                logger.info(f"[DRY-RUN] Would use manual crop: {crop_params}")
            except ValueError as e:
                logger.warning(f"[DRY-RUN] Invalid manual crop format: {e}")
        elif detect_crop:
            logger.info("[DRY-RUN] Would detect black borders automatically")

        return ExtractionResult(
            video_files=expected_video_files,
            audio_files=expected_audio_files,
            crop_params=crop_params,
            reused=False,
            needs_work=True,
            success=True,
            error=None
        )

    # Perform extraction using ffmpeg instead of mkvextract for proper timestamps
    logger.info("Extracting streams...")
    try:
        import subprocess

        # Extract video streams
        for i, track in enumerate(video_tracks):
            output_file = output_dir / track.display_name()

            cmd: list[str|PathLike] = [
                'ffmpeg',
                '-i', source_video,
                '-map', f'0:{track.track_id}',
                '-c', 'copy',
                '-fflags', '+genpts',
                '-avoid_negative_ts', 'make_zero',
                '-y',
                str(output_file)
            ]

            logger.debug(f"Extracting video track {track.track_id}: {output_file.name}")
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)

        # Extract audio streams
        for i, track in enumerate(audio_tracks):
            output_file = output_dir / track.display_name()

            cmd: list[str|PathLike] = [
                'ffmpeg',
                '-i', source_video,
                '-map', f'0:{track.track_id}',
                '-c', 'copy',
                '-fflags', '+genpts',
                '-avoid_negative_ts', 'make_zero',
                '-y',
                output_file
            ]

            logger.debug(f"Extracting audio track {track.track_id}: {output_file.name}")
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)

        logger.info("Stream extraction completed")

    except subprocess.CalledProcessError as e:
        error_msg = f"Failed to extract streams: {e.stderr}"
        logger.critical(error_msg)
        return ExtractionResult(
            video_files=[],
            audio_files=[],
            crop_params=None,
            reused=False,
            needs_work=False,
            success=False,
            error=error_msg
        )
    except Exception as e:
        error_msg = f"Failed to extract streams: {e}"
        logger.critical(error_msg)
        return ExtractionResult(
            video_files=[],
            audio_files=[],
            crop_params=None,
            reused=False,
            needs_work=False,
            success=False,
            error=error_msg
        )

    # Handle crop detection/parsing
    crop_params = None
    if manual_crop:
        try:
            crop_params = CropParams.parse(manual_crop)
            logger.info(f"Using manual crop: {crop_params}")
        except ValueError as e:
            logger.warning(f"Invalid manual crop format: {e}, skipping crop")
    elif detect_crop and expected_video_files:
        # Detect crop on first extracted video file
        crop_params = _detect_crop_parameters(video_metas[0])

    return ExtractionResult(
        video_files=video_metas,
        audio_files=expected_audio_files,
        crop_params=crop_params,
        reused=False,
        needs_work=False,
        success=True,
        error=None
    )
