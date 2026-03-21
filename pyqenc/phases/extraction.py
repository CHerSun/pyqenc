"""
Extraction phase for the quality-based encoding pipeline.

This module handles extraction of streams from the source MKV file.
It also provides the MKVTrackExtractor and stream model classes for
parsing and extracting MKV tracks via ffprobe / mkvextract.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pyqenc.constants import (
    FAILURE_SYMBOL_MINOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
)
from pyqenc.models import AudioMetadata, PhaseOutcome, VideoMetadata
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.state import ArtifactState, ExtractionParams
from pyqenc.utils.ffmpeg_runner import run_ffmpeg

if TYPE_CHECKING:
    from pyqenc.models import PipelineConfig
    from pyqenc.phases.job import JobPhase, JobPhaseResult

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


def _log_stream_table(
    all_tracks:      list[StreamBase],
    selected_tracks: list[StreamBase],
    on_disk_names:   set[str],
) -> None:
    """Log a 3-column stream table: wanted, present, artifact name.

    Columns:
    - wanted:  ``✔`` if the stream passes the current include/exclude filters,
               ``✘`` otherwise.
    - present: ``✔`` if the artifact file is already on disk, ``✘`` otherwise.
               ``-`` for streams that are not wanted (not applicable).
    - name:    The would-be output filename for the stream.

    Args:
        all_tracks:    All streams discovered in the source file.
        selected_tracks: Streams that passed the include/exclude filters.
        on_disk_names: Set of filenames currently present in the extracted dir.
    """
    if not all_tracks:
        return

    selected_set = set(id(t) for t in selected_tracks)

    max_track_num   = len(all_tracks)
    max_track_id    = max((t.track_id for t in all_tracks), default=0)
    track_num_width = len(str(max_track_num))
    track_id_width  = len(str(max(max_track_id, 0)))

    logger.info("Streams:")
    for track in all_tracks:
        wanted  = id(track) in selected_set
        name    = track.display_name(track_num_width, track_id_width)
        w_sym   = SUCCESS_SYMBOL_MINOR if wanted else FAILURE_SYMBOL_MINOR
        p_sym   = (SUCCESS_SYMBOL_MINOR if name in on_disk_names else FAILURE_SYMBOL_MINOR) if wanted else "-"
        logger.info("  %s  %s  %s", w_sym, p_sym, name)


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


# ---------------------------------------------------------------------------
# Pipeline extraction helpers
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """Result of stream extraction phase.

    Attributes:
        video:   Extracted video metadata (single stream).
        audio:   List of extracted audio track metadata.
        outcome: Phase outcome (COMPLETED, REUSED, DRY_RUN, or FAILED).
        error:   Error message if extraction failed.
    """

    video:   VideoMetadata | None
    audio:   list[AudioMetadata]
    outcome: PhaseOutcome
    error:   str | None = None


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
    force:        bool = False,
    dry_run:      bool = False,
) -> ExtractionResult:
    """Extract video and audio streams from source MKV file.

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
        force:        If ``False``, reuse existing extracted files.
        dry_run:      If ``True``, only report status without performing extraction.

    Returns:
        ``ExtractionResult`` with the first video stream,
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
    existing = {f.name for f in output_dir.iterdir() if f.is_file()} if output_dir.exists() else set()
    _log_stream_table(extractor.tracks, selected_tracks, existing)

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
    expected_other_files = [output_dir / t.display_name() for t in other_tracks]
    video_metas = [
        VideoMetadata.from_stream(path=f, stream=t)
        for f, t in zip(expected_video_files, video_tracks)
    ]

    # ------------------------------------------------------------------
    # Recovery: clean up .tmp files and check existing artifacts
    # ------------------------------------------------------------------
    _extraction_yaml = output_dir.parent / "extraction.yaml"

    # Clean up leftover .tmp files
    from pyqenc.phases.recovery import _cleanup_tmp_files
    _cleanup_tmp_files(output_dir)

    # Determine recovery state: COMPLETE if all expected files exist, ABSENT otherwise
    artifact_files = [
        f for f in output_dir.iterdir()
        if f.is_file() and not f.name.endswith(TEMP_SUFFIX)
    ] if output_dir.exists() else []
    recovery_state = ArtifactState.COMPLETE if artifact_files else ArtifactState.ABSENT

    persisted_params = ExtractionParams.load(_extraction_yaml)
    if persisted_params is not None:
        if persisted_params.include != include or persisted_params.exclude != exclude:
            logger.warning(
                "Stream filter change detected — extraction needs re-execution. "
                "Persisted: include=%r, exclude=%r. Current: include=%r, exclude=%r. "
                "Re-run with --force to re-extract with the new filters.",
                persisted_params.include, persisted_params.exclude,
                include, exclude,
            )
            recovery_state = ArtifactState.ABSENT

    # ------------------------------------------------------------------
    # Reuse path — all artifacts present (COMPLETE via .tmp protocol)
    # ------------------------------------------------------------------
    all_exist = (
        not force
        and recovery_state == ArtifactState.COMPLETE
        and all(f.exists() for f in expected_video_files + expected_audio_files + expected_other_files)
    )
    if all_exist:
        primary_video = video_metas[0]
        audio_meta = [_audio_metadata_from_stream(path, track)
                      for path, track in zip(expected_audio_files, audio_tracks)]
        return ExtractionResult(video=primary_video, audio=audio_meta, outcome=PhaseOutcome.REUSED)

    # ------------------------------------------------------------------
    # Dry-run path
    # ------------------------------------------------------------------
    if dry_run:
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
    logger.debug("Extracting streams...")
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
            ExtractionParams(include=include, exclude=exclude).save(_extraction_yaml)
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
    audio_meta = [
        _audio_metadata_from_stream(path, track)
        for path, track in zip(expected_audio_files, audio_tracks)
    ]
    return ExtractionResult(
        video=primary_video, audio=audio_meta, outcome=PhaseOutcome.COMPLETED,
    )


# ---------------------------------------------------------------------------
# ExtractionPhase — Phase object (task 5)
# ---------------------------------------------------------------------------

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, cast

from pyqenc.constants import EXTRACTED_DIR, TEMP_SUFFIX, THICK_LINE, THIN_LINE
from pyqenc.models import AudioMetadata, PhaseOutcome, VideoMetadata
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.state import ArtifactState, ExtractionParams
from pyqenc.utils.log_format import emit_phase_banner, log_recovery_line

if TYPE_CHECKING:
    from pyqenc.models import PipelineConfig
    from pyqenc.phases.job import JobPhase, JobPhaseResult

_EXTRACTION_YAML_NAME = "extraction.yaml"


@dataclass
class VideoArtifact(Artifact):
    """Extraction artifact for a video stream.

    Attributes:
        meta: Video metadata; populated when ``state`` is ``COMPLETE``.
    """

    meta: VideoMetadata | None = None


@dataclass
class AudioArtifact(Artifact):
    """Extraction artifact for an audio stream.

    Attributes:
        meta: Audio metadata; populated when ``state`` is ``COMPLETE``.
    """

    meta: AudioMetadata | None = None


@dataclass
class OtherArtifact(Artifact):
    """Extraction artifact for subtitles, chapters, or attachments.

    No additional metadata beyond the base ``path`` and ``state``.
    """


# Type alias for all extraction artifacts — use this in annotations throughout
ExtractionArtifact: TypeAlias = VideoArtifact | AudioArtifact | OtherArtifact


@dataclass
class ExtractionPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying extraction-specific payload.

    Attributes:
        video:  Primary extracted video metadata; ``None`` on failure.
        audio:  List of extracted audio track metadata.
    """

    video: VideoMetadata | None = None
    audio: list[AudioMetadata] = field(default_factory=list)


class ExtractionPhase:
    """Phase object for stream extraction.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the extraction phase.  Wraps the existing ``MKVTrackExtractor`` and
    ``extract_streams`` helpers.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "extraction"

    def __init__(
        self,
        config: "PipelineConfig",
        phases: "dict[type[Phase], Phase] | None" = None,
    ) -> None:
        from pyqenc.phases.job import JobPhase as _JobPhase

        self._config = config
        self._job: "_JobPhase | None" = cast("_JobPhase", phases[_JobPhase]) if phases else None
        self.result: ExtractionPhaseResult | None = None
        self.dependencies: list[Phase] = [self._job] if self._job is not None else []

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> ExtractionPhaseResult:
        """Classify existing extraction artifacts without executing any work.

        Calls ``_ensure_dependencies()`` to scan dependencies if needed, then
        runs ``_recover()`` in read-only mode.

        Returns:
            ``ExtractionPhaseResult`` with all artifacts classified.
        """
        if self.result is not None:
            return self.result

        dep_result = self._ensure_dependencies(execute=False)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        artifacts, video_meta, audio_meta = self._recover(
            force_wipe=force_wipe, execute=False
        )
        outcome = self._outcome_from_artifacts(artifacts, did_work=False)
        self.result = ExtractionPhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = self._recovery_message(artifacts),
            video     = video_meta,
            audio     = audio_meta,
        )
        return self.result

    def run(self, dry_run: bool = False) -> ExtractionPhaseResult:
        """Recover, extract pending artifacts, persist ``extraction.yaml``.

        Sequence:
        1. Emit phase banner.
        2. Ensure dependencies have results (scan if needed).
        3. Run ``_recover()`` — handles ``force_wipe`` and filter-change detection.
        4. Log recovery result line.
        5. In dry-run mode: return ``DRY_RUN`` if any artifacts are pending.
        6. Extract ``ABSENT`` artifacts; leave ``STALE`` on disk.
        7. Persist ``extraction.yaml``.
        8. Log completion summary.

        Args:
            dry_run: When ``True``, report what would be done without writing files.

        Returns:
            ``ExtractionPhaseResult`` with all artifacts ``COMPLETE`` on success.
        """
        emit_phase_banner("EXTRACTION", logger)

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        # Key parameters
        logger.info("Source:   %s", self._config.source_video.name)
        if self._config.include:
            logger.info("Include:  %s", self._config.include)
        if self._config.exclude:
            logger.info("Exclude:  %s", self._config.exclude)

        artifacts, video_meta, audio_meta = self._recover(
            force_wipe=force_wipe, execute=True
        )

        # Log recovery result line
        complete_count = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
        pending_count  = sum(
            1 for a in artifacts
            if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY)
        )
        stale_count = sum(1 for a in artifacts if a.state == ArtifactState.STALE)
        log_recovery_line(logger, complete_count, pending_count, stale=stale_count)

        # Dry-run path
        if dry_run:
            if pending_count == 0 and stale_count == 0:
                outcome = PhaseOutcome.REUSED
            else:
                outcome = PhaseOutcome.DRY_RUN
            self.result = ExtractionPhaseResult(
                outcome   = outcome,
                artifacts = artifacts,
                message   = "dry-run",
                video     = video_meta,
                audio     = audio_meta,
            )
            return self.result

        # Nothing to do
        if pending_count == 0:
            self.result = ExtractionPhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = artifacts,
                message   = "all artifacts reused",
                video     = video_meta,
                audio     = audio_meta,
            )
            return self.result

        # Execute extraction for ABSENT artifacts
        result = self._execute_extraction(artifacts, video_meta, audio_meta)
        self.result = result
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, execute: bool) -> "ExtractionPhaseResult | None":
        """Scan dependencies if they have no cached result; fail fast if incomplete.

        Args:
            execute: When ``True``, call ``dep.run()`` instead of ``dep.scan()``
                     for dependencies without a cached result.

        Returns:
            A ``FAILED`` result if any dependency is not complete; ``None`` otherwise.
        """
        if self._job is None:
            return ExtractionPhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = [],
                message   = "JobPhase dependency not wired",
                error     = "ExtractionPhase requires JobPhase",
                video     = None,
                audio     = [],
            )

        if self._job.result is None:
            if execute:
                self._job.run()
            else:
                self._job.scan()

        if not self._job.result.is_complete:  # type: ignore[union-attr]
            err = "JobPhase did not complete successfully"
            logger.critical(err)
            return ExtractionPhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = [],
                message   = err,
                error     = err,
                video     = None,
                audio     = [],
            )
        return None

    def _recover(
        self,
        force_wipe: bool,
        execute: bool,
    ) -> tuple[list[ExtractionArtifact], VideoMetadata | None, list[AudioMetadata]]:
        """Classify extraction artifacts and handle force-wipe / filter changes.

        Steps:
        1. If ``force_wipe``: delete ``extracted/`` and ``extraction.yaml``.
        2. Clean up leftover ``.tmp`` files.
        3. Load persisted ``extraction.yaml``; compare include/exclude filters.
        4. Scan ``extracted/`` and classify each file.

        Args:
            force_wipe: When ``True``, wipe all extraction artifacts first.
            execute:    When ``True``, ``.tmp`` cleanup and wipe are performed;
                        when ``False`` (scan mode), no files are written or deleted.

        Returns:
            ``(artifacts, primary_video_meta, audio_meta_list)`` tuple.
        """
        work_dir      = self._config.work_dir
        extracted_dir = work_dir / EXTRACTED_DIR
        yaml_path     = work_dir / _EXTRACTION_YAML_NAME

        # Step 1: force-wipe
        if force_wipe and execute:
            if extracted_dir.exists():
                shutil.rmtree(extracted_dir)
                logger.debug("force_wipe: deleted %s", extracted_dir)
            if yaml_path.exists():
                yaml_path.unlink()
                logger.debug("force_wipe: deleted %s", yaml_path)

        # Step 2: clean up .tmp files (execute mode only)
        if execute and extracted_dir.exists():
            for tmp in extracted_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 3: load persisted params and detect filter changes
        persisted = ExtractionParams.load(yaml_path)
        filter_changed = (
            persisted is not None
            and (persisted.include != self._config.include or persisted.exclude != self._config.exclude)
        )

        # Step 4: scan extracted/ and classify
        if not extracted_dir.exists():
            # Nothing extracted yet — show table with all streams absent
            try:
                extractor = MKVTrackExtractor(str(self._config.source_video))
                selected  = streams_filter_plain_regex(
                    extractor.tracks,
                    include_pattern = self._config.include,
                    exclude_pattern = self._config.exclude,
                )
                _log_stream_table(extractor.tracks, selected, set())
            except Exception:
                pass
            return [], None, []

        all_files = [
            f for f in extracted_dir.iterdir()
            if f.is_file() and not f.name.endswith(TEMP_SUFFIX)
        ]
        if not all_files:
            # Dir exists but is empty — show table with all streams absent
            try:
                extractor = MKVTrackExtractor(str(self._config.source_video))
                selected  = streams_filter_plain_regex(
                    extractor.tracks,
                    include_pattern = self._config.include,
                    exclude_pattern = self._config.exclude,
                )
                _log_stream_table(extractor.tracks, selected, set())
            except Exception:
                pass
            return [], None, []

        # Build extractor to know what files are expected under current filters
        try:
            extractor = MKVTrackExtractor(str(self._config.source_video))
        except Exception as exc:
            logger.critical("Failed to analyse source video: %s", exc)
            return [], None, []

        selected_tracks = streams_filter_plain_regex(
            extractor.tracks,
            include_pattern = self._config.include,
            exclude_pattern = self._config.exclude,
        )
        expected_names = {t.display_name() for t in selected_tracks}

        artifacts: list[ExtractionArtifact] = []
        primary_video: VideoMetadata | None = None
        audio_list: list[AudioMetadata] = []

        for f in sorted(all_files):
            if filter_changed:
                state = ArtifactState.COMPLETE if f.name in expected_names else ArtifactState.STALE
            else:
                state = ArtifactState.COMPLETE

            if f.suffix == ".mkv":
                vm = VideoMetadata(path=f) if state == ArtifactState.COMPLETE else None
                if vm is not None and primary_video is None:
                    primary_video = vm
                artifacts.append(VideoArtifact(path=f, state=state, meta=vm))
            elif f.suffix == ".mka":
                am: AudioMetadata | None = None
                if state == ArtifactState.COMPLETE:
                    track = next(
                        (t for t in selected_tracks
                         if t.codec_type == "audio" and t.display_name() == f.name),
                        None,
                    )
                    if track is not None:
                        am = _audio_metadata_from_stream(f, track)  # type: ignore[arg-type]
                        audio_list.append(am)
                artifacts.append(AudioArtifact(path=f, state=state, meta=am))
            else:
                artifacts.append(OtherArtifact(path=f, state=state))

        # Files expected but not yet on disk → ABSENT
        on_disk_names = {f.name for f in all_files}
        for track in selected_tracks:
            name = track.display_name()
            if name not in on_disk_names:
                if track.codec_type == "video":
                    artifacts.append(VideoArtifact(path=extracted_dir / name, state=ArtifactState.ABSENT))
                elif track.codec_type == "audio":
                    artifacts.append(AudioArtifact(path=extracted_dir / name, state=ArtifactState.ABSENT))
                else:
                    artifacts.append(OtherArtifact(path=extracted_dir / name, state=ArtifactState.ABSENT))

        # Emit stream table with wanted + present columns
        _log_stream_table(extractor.tracks, selected_tracks, on_disk_names)

        return artifacts, primary_video, audio_list

    def _execute_extraction(
        self,
        artifacts:   list[ExtractionArtifact],
        video_meta:  VideoMetadata | None,
        audio_meta:  list[AudioMetadata],
    ) -> ExtractionPhaseResult:
        """Extract ABSENT artifacts and persist ``extraction.yaml``.

        Args:
            artifacts:  Artifact list from ``_recover()``.
            video_meta: Primary video metadata (may be ``None`` if not yet extracted).
            audio_meta: Audio metadata list (may be incomplete).

        Returns:
            ``ExtractionPhaseResult`` after extraction.
        """
        work_dir      = self._config.work_dir
        extracted_dir = work_dir / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)

        source = self._config.source_video
        if not source.exists():
            err = f"Source video not found: {source}"
            logger.critical(err)
            return ExtractionPhaseResult(
                outcome=PhaseOutcome.FAILED, artifacts=artifacts,
                message=err, error=err, video=None, audio=[],
            )

        # Re-build extractor and selected tracks for extraction
        try:
            extractor = MKVTrackExtractor(str(source))
        except Exception as exc:
            err = f"Failed to analyse source video: {exc}"
            logger.critical(err)
            return ExtractionPhaseResult(
                outcome=PhaseOutcome.FAILED, artifacts=artifacts,
                message=err, error=err, video=None, audio=[],
            )

        selected_tracks = streams_filter_plain_regex(
            extractor.tracks,
            include_pattern = self._config.include,
            exclude_pattern = self._config.exclude,
        )

        video_tracks: list[VideoStream] = [t for t in selected_tracks if t.codec_type == "video"]  # type: ignore[assignment]
        audio_tracks: list[AudioStream] = [t for t in selected_tracks if t.codec_type == "audio"]  # type: ignore[assignment]
        other_tracks: list[StreamBase]  = [t for t in selected_tracks if t.codec_type not in ("video", "audio")]

        if not video_tracks:
            err = "No video streams found matching filters"
            logger.critical(err)
            return ExtractionPhaseResult(
                outcome=PhaseOutcome.FAILED, artifacts=artifacts,
                message=err, error=err, video=None, audio=[],
            )

        # Determine which files are ABSENT (need extraction)
        absent_names = {
            a.path.name for a in artifacts if a.state == ArtifactState.ABSENT
        }

        errors: list[str] = []

        # Extract video tracks
        for track in video_tracks:
            output_file = extracted_dir / track.display_name()
            if output_file.name not in absent_names:
                continue
            cmd: list[str | PathLike] = [
                "ffmpeg", "-i", source,
                "-map", f"0:{track.track_id}",
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-y", output_file,
            ]
            logger.debug("Extracting video track %d: %s", track.track_id, output_file.name)
            res = run_ffmpeg(cmd, output_file=output_file)
            if not res.success:
                err = f"ffmpeg failed extracting video track {track.track_id}"
                logger.error(err)
                errors.append(err)

        # Extract audio tracks
        for track in audio_tracks:
            output_file = extracted_dir / track.display_name()
            if output_file.name not in absent_names:
                continue
            cmd = [
                "ffmpeg", "-i", source,
                "-map", f"0:{track.track_id}",
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-y", output_file,
            ]
            logger.debug("Extracting audio track %d: %s", track.track_id, output_file.name)
            res = run_ffmpeg(cmd, output_file=output_file)
            if not res.success:
                err = f"ffmpeg failed extracting audio track {track.track_id}"
                logger.error(err)
                errors.append(err)

        # Extract other tracks (subtitles, chapters, attachments) via mkvextract
        other_absent = [t for t in other_tracks if (extracted_dir / t.display_name()).name in absent_names]
        if other_absent:
            logger.debug("Extracting %d other track(s) via mkvextract", len(other_absent))
            extractor.extract_tracks(other_absent, extracted_dir)

        # Persist extraction.yaml
        try:
            ExtractionParams(
                include = self._config.include,
                exclude = self._config.exclude,
            ).save(work_dir / _EXTRACTION_YAML_NAME)
        except Exception as exc:
            logger.warning("Could not persist extraction.yaml: %s", exc)

        # Re-scan to build final typed artifact list and metadata
        final_artifacts: list[ExtractionArtifact] = []
        final_video: VideoMetadata | None = None
        final_audio: list[AudioMetadata] = []

        for track in video_tracks:
            f     = extracted_dir / track.display_name()
            state = ArtifactState.COMPLETE if f.exists() and f.stat().st_size > 0 else ArtifactState.ABSENT
            vm    = VideoMetadata(path=f) if state == ArtifactState.COMPLETE else None
            if vm is not None and final_video is None:
                final_video = vm
            final_artifacts.append(VideoArtifact(path=f, state=state, meta=vm))

        for track in audio_tracks:
            f     = extracted_dir / track.display_name()
            state = ArtifactState.COMPLETE if f.exists() and f.stat().st_size > 0 else ArtifactState.ABSENT
            am: AudioMetadata | None = None
            if state == ArtifactState.COMPLETE:
                am = _audio_metadata_from_stream(f, track)  # type: ignore[arg-type]
                final_audio.append(am)
            final_artifacts.append(AudioArtifact(path=f, state=state, meta=am))

        for track in other_tracks:
            f     = extracted_dir / track.display_name()
            state = ArtifactState.COMPLETE if f.exists() else ArtifactState.ABSENT
            final_artifacts.append(OtherArtifact(path=f, state=state))

        # Keep STALE artifacts from original recovery (they stay on disk)
        for a in artifacts:
            if a.state == ArtifactState.STALE:
                final_artifacts.append(a)

        if errors:
            failed_count = sum(1 for a in final_artifacts if a.state == ArtifactState.ABSENT)
            err_summary  = f"{len(errors)} extraction error(s): {'; '.join(errors)}"
            logger.error(err_summary)
            outcome = PhaseOutcome.FAILED if failed_count > 0 else PhaseOutcome.COMPLETED
            return ExtractionPhaseResult(
                outcome   = outcome,
                artifacts = final_artifacts,
                message   = err_summary,
                error     = err_summary if outcome == PhaseOutcome.FAILED else None,
                video     = final_video,
                audio     = final_audio,
            )

        complete_count = sum(1 for a in final_artifacts if a.state == ArtifactState.COMPLETE)
        logger.info(
            "%s Extraction complete: %d artifact(s) extracted",
            SUCCESS_SYMBOL_MINOR, complete_count,
        )
        logger.info(THICK_LINE)

        return ExtractionPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = final_artifacts,
            message   = f"extracted {complete_count} artifact(s)",
            video     = final_video,
            audio     = final_audio,
        )

    @staticmethod
    def _outcome_from_artifacts(
        artifacts: list[ExtractionArtifact],
        did_work:  bool,
    ) -> PhaseOutcome:
        """Derive ``PhaseOutcome`` from artifact states."""
        if any(a.state == ArtifactState.ABSENT for a in artifacts):
            return PhaseOutcome.DRY_RUN
        if all(a.state == ArtifactState.COMPLETE for a in artifacts) and artifacts:
            return PhaseOutcome.REUSED if not did_work else PhaseOutcome.COMPLETED
        return PhaseOutcome.DRY_RUN

    @staticmethod
    def _recovery_message(artifacts: list[ExtractionArtifact]) -> str:
        complete = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
        pending  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
        stale    = sum(1 for a in artifacts if a.state == ArtifactState.STALE)
        parts    = [f"{complete} complete", f"{pending} pending"]
        if stale:
            parts.append(f"{stale} stale")
        return ", ".join(parts)
