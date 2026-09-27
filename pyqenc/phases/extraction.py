"""Extraction phase — enumerates source streams and extracts container artifacts.

Direct-from-source model (spec ``2026-09-25 file-stream-model``): the phase
enumerates every stream once per run via ffprobe into the typed composition
family (:class:`~pyqenc.stream_model.Stream` objects composed with the job's
:class:`~pyqenc.stream_model.File`), persists the inventory to
``extraction.yaml``, and extracts **only** container artifacts —
``timestamps.txt`` (unconditional), chapters, subtitles and attachments
(per include/exclude filters). Video and audio tracks are never copied to
``extracted/``; downstream phases read them from the source through the
stream objects.

Recovery loads ``extraction.yaml`` instead of re-probing whenever the sidecar
is present and its source identity matches the job's live ``File``.
"""
# CHerSun 2026

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

from pyqenc.audio.layout import ChannelLayout
from pyqenc.constants import (
    EXTRACTED_DIR,
    FAILURE_SYMBOL_MINOR,
    FFMPEG_CODEC_COPY,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
    TIMESTAMPS_FILENAME,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import PhaseOutcome, VideoMetadata
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.job import JobPhase, JobPhaseResult
from pyqenc.state import ArtifactState
from pyqenc.stream_model import (
    AttachmentStream,
    AttachmentStreamInfo,
    AudioStream,
    AudioStreamInfo,
    ContainerArtifact,
    ExtractionSidecar,
    File,
    SourceMismatchError,
    StreamsInventory,
    SubtitleStream,
    SubtitleStreamInfo,
    VideoStream,
    VideoStreamInfo,
)
from pyqenc.utils.disk_space import log_disk_space_info
from pyqenc.utils.ffmpeg_runner import FFmpegInput, FFmpegRequest, run_ffmpeg
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

logger = logging.getLogger(__name__)

_EXTRACTION_YAML_FILENAME = "extraction.yaml"

_SUBTITLE_FFMPEG_FORMAT: dict[str, str] = {
    "srt": "srt",
    "ssa": "ass",
    "ass": "ass",
}
"""Text subtitle codecs that require an explicit ``-f`` muxer for their ``.tmp``
output. Bitmap subtitle codecs (pgs, sub) are self-describing and stay on the
runner's Matroska default."""

_CHAPTERS_DISPLAY_NAME = "chapters.xml"
"""Filter/display string for the chapters container artifact."""


# ---------------------------------------------------------------------------
# ffprobe enumeration → typed stream objects
# ---------------------------------------------------------------------------

def _probe_streams_json(source: Path) -> dict:
    """Run ffprobe on the source and return the parsed streams/chapters JSON.

    Args:
        source: Path to the source media file.

    Returns:
        Parsed ffprobe JSON output.

    Raises:
        RuntimeError: When ffprobe fails or its output is unparseable.
    """
    cmd: list[str | os.PathLike] = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_chapters",
        source,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)  # type: ignore[no-any-return]
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"FFprobe error: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse FFprobe output: {exc}") from exc


def _tags_of(raw: dict) -> dict:
    """The stream's tags dict (possibly nested under the container's tag list)."""
    return raw.get("tags") or {}


def _float_or_none(value: object) -> float | None:
    """Parse an ffprobe scalar into a float, tolerating missing/bad values."""
    if value is None:
        return None
    try:
        return float(str(value))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _base_info_fields(raw: dict) -> dict:
    """The container-level ``StreamInfo`` fields from one ffprobe stream dict."""
    tags = _tags_of(raw)
    return {
        "track_id":         int(raw.get("index", -1)),
        "codec_name":       raw.get("codec_name"),
        "language":         tags.get("language"),
        "title":            tags.get("title") or tags.get("TITLE"),
        "start_timestamp":  _float_or_none(raw.get("start_time")),
        "duration_seconds": _float_or_none(raw.get("duration")),
    }


def _video_info(raw: dict) -> VideoStreamInfo:
    """Build a :class:`VideoStreamInfo` from one ffprobe video stream dict."""
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
    return VideoStreamInfo(
        **_base_info_fields(raw),
        fps          = float(fps_fraction) if fps_fraction is not None else None,
        fps_fraction = fps_fraction,
        resolution   = f"{width}x{height}" if width and height else None,
        pix_fmt      = raw.get("pix_fmt"),
    )


def _audio_info(raw: dict) -> AudioStreamInfo:
    """Build an :class:`AudioStreamInfo` from one ffprobe audio stream dict."""
    layout: ChannelLayout | None = None
    if channel_layout := raw.get("channel_layout"):
        layout = ChannelLayout.parse(channel_layout)
    elif channels := raw.get("channels"):
        layout = ChannelLayout.parse(f"{channels}.0")
    return AudioStreamInfo(**_base_info_fields(raw), layout=layout)


def _subtitle_info(raw: dict) -> SubtitleStreamInfo:
    """Build a :class:`SubtitleStreamInfo` from one ffprobe subtitle stream dict."""
    return SubtitleStreamInfo(
        **_base_info_fields(raw),
        is_forced=(raw.get("disposition") or {}).get("forced") == 1,
    )


def _attachment_info(raw: dict) -> AttachmentStreamInfo:
    """Build an :class:`AttachmentStreamInfo` from one ffprobe attachment dict."""
    return AttachmentStreamInfo(
        **_base_info_fields(raw),
        filename=_tags_of(raw).get("filename"),
    )


def _is_attachment(raw: dict) -> bool:
    """Whether an ffprobe stream is an attachment (attached picture/font)."""
    if (raw.get("disposition") or {}).get("attached_pic", 0) == 1:
        return True
    return str(_tags_of(raw).get("mimetype", "")).startswith("image/")


def _enumerate_streams(
    data:    dict,
    source_file: File,
) -> tuple[list[VideoStream], list[AudioStream], list[SubtitleStream], list[AttachmentStream], bool]:
    """Build the typed stream inventory from parsed ffprobe JSON.

    Every stream is instantiated exactly once, composed with the job's
    :class:`File`. Data streams are skipped (not ``-map``-selectable payload);
    chapters are reported separately via the returned flag.

    Args:
        data:        Parsed ffprobe JSON (``streams`` + ``chapters``).
        source_file: The job's source :class:`File` to compose with.

    Returns:
        ``(video, audio, subtitles, attachments, has_chapters)``.
    """
    video:       list[VideoStream]       = []
    audio:       list[AudioStream]       = []
    subtitles:   list[SubtitleStream]    = []
    attachments: list[AttachmentStream]  = []

    for raw in data.get("streams", []):
        codec_type = raw.get("codec_type", "")
        if _is_attachment(raw):
            attachments.append(AttachmentStream(file=source_file, info=_attachment_info(raw)))
        elif codec_type == "video":
            video.append(VideoStream(file=source_file, info=_video_info(raw)))
        elif codec_type == "audio":
            audio.append(AudioStream(file=source_file, info=_audio_info(raw)))
        elif codec_type == "subtitle":
            subtitles.append(SubtitleStream(file=source_file, info=_subtitle_info(raw)))
        # Data and unknown streams are not consumable payload — skipped.

    if len(video) > 1:
        logger.warning(
            "Source contains %d video streams — scene detection and default-selector "
            "external consumers operate on the FIRST video stream (%s).",
            len(video), video[0].display_name(),
        )

    return video, audio, subtitles, attachments, bool(data.get("chapters"))


def streams_filter_plain_regex(
    tracks: list[Any],
    include_pattern: str | None = None,
    exclude_pattern: str | None = None,
    case_sensitive: bool = False,
) -> list[Any]:
    """Filter streams using include/exclude regex patterns on display names.

    Generic over the stream family: every :class:`~pyqenc.stream_model.Stream`
    provides ``display_name()``, and the matched identity fields (codec,
    language, title) sit on the info base so the filter never reaches into
    type specifics.

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


# ---------------------------------------------------------------------------
# Timestamps extraction (mkvextract first, ffprobe fallback — tmp-then-rename)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Artifacts and result
# ---------------------------------------------------------------------------

@dataclass
class SubtitleArtifact(Artifact):
    """Extraction artifact for one subtitle stream.

    Attributes:
        stream: The subtitle stream owning the artifact's name and selector.
    """

    stream: SubtitleStream | None = None


@dataclass
class AttachmentArtifact(Artifact):
    """Extraction artifact for one attachment stream.

    Attributes:
        stream: The attachment stream owning the artifact's name.
    """

    stream: AttachmentStream | None = None


@dataclass
class ChaptersArtifact(Artifact):
    """Extraction artifact for the container's chapter edition (chapters.xml)."""


@dataclass
class TimestampArtifact(Artifact):
    """Extraction artifact for the per-frame PTS timestamp file.

    Path: extracted/timestamps.txt
    States: COMPLETE (file exists and non-empty) or ABSENT only.
    Not subject to include/exclude stream filtering.

    Attributes:
        stream: The video stream whose PTS values are extracted.
    """

    stream: VideoStream | None = None


# Type alias for all extraction artifacts — use this in annotations throughout
type ExtractionArtifact = SubtitleArtifact | AttachmentArtifact | ChaptersArtifact | TimestampArtifact


@dataclass
class ExtractionPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying extraction-specific payload.

    Attributes:
        video_stream:     The (first) enumerated video stream; ``None`` when absent.
        audio_streams:    All enumerated audio streams in track order.
        subtitle_streams: Enumerated subtitle streams (extracted paths set when complete).
        attachment_streams: Enumerated attachment streams (extracted paths set when complete).
        video:            Interim legacy view for phases not yet migrated to the
                          stream model (derived from ``video_stream``).
        timestamps_path:  Path to the extracted timestamps.txt; ``None`` when absent.
        chapters_path:    Path to the extracted chapters.xml; ``None`` when absent.
    """

    video_stream:       VideoStream | None  = None
    audio_streams:      list[AudioStream]   = field(default_factory=list)
    subtitle_streams:   list[SubtitleStream] = field(default_factory=list)
    attachment_streams: list[AttachmentStream] = field(default_factory=list)
    video:              VideoMetadata | None = None
    timestamps_path:    Path | None          = None
    chapters_path:      Path | None          = None


# ---------------------------------------------------------------------------
# Interim legacy adapters (deleted with the last legacy consumer)
# ---------------------------------------------------------------------------

def _legacy_video_metadata(stream: VideoStream) -> VideoMetadata:
    """Derive the legacy ``VideoMetadata`` view from a video stream's info."""
    vm = VideoMetadata(path=stream.file.path)
    vm._fps               = stream.info.fps
    vm._resolution        = stream.info.resolution
    vm._pix_fmt           = stream.info.pix_fmt
    vm._duration_seconds  = stream.info.duration_seconds
    vm._file_size_bytes   = stream.file.file_size_bytes
    return vm


# ---------------------------------------------------------------------------
# ExtractionPhase
# ---------------------------------------------------------------------------

class ExtractionPhase(Phase):
    """Phase object for stream enumeration and container-artifact extraction.

    Owns the source's stream inventory: enumeration (or sidecar load),
    recovery, invalidation, extraction of timestamps/chapters/subtitles/
    attachments, and the disk-space estimate. The uniform run footprint is
    inherited from :class:`Phase`.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
        video_required: When ``True`` (default), the timestamps artifact is
                        wanted; audio-only registries pass ``False``.
        collector: Metrics collector for timing instrumentation.
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

        # Recovery stash — the run's stream inventory and sidecar currency.
        self._source_file: File                    = None  # type: ignore[assignment]
        self._video:       VideoStream | None      = None
        self._audio:       list[AudioStream]       = []
        self._subtitles:   list[SubtitleStream]    = []
        self._attachments: list[AttachmentStream]  = []
        self._has_chapters: bool                   = False
        self._sidecar_dirty: bool                  = False

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
        """Build the stream inventory and classify the extractable artifacts.

        Steps:

        1. If ``force_wipe``: delete ``extracted/`` and ``extraction.yaml``.
        2. Clean up leftover ``.tmp`` files.
        3. Resolve the inventory: load ``extraction.yaml`` when present and its
           source identity matches the job's live ``File`` (no re-probe);
           otherwise enumerate via ffprobe and mark the sidecar dirty.
        4. Produce one artifact per subtitle/attachment (name owned by the
           stream class), plus chapters and timestamps artifacts. ``wanted``
           comes from the current include/exclude filter and ``video_required``;
           ``state`` from the single on-disk listing — the two are orthogonal,
           and a filter change never needs a STALE state.

        Returns:
            The :class:`Recovery` single source of truth; the artifact list
            contains every extractable component (including ``wanted=False``).

        Raises:
            RecoveryError: When the source cannot be analysed at all.
        """
        job_result: JobPhaseResult = cast(JobPhaseResult, self._dep(JobPhase).result)
        work_dir      = job_result.work_dir
        extracted_dir = work_dir / EXTRACTED_DIR
        sidecar_path  = work_dir / _EXTRACTION_YAML_FILENAME
        force_wipe    = job_result.force_wipe

        # Step 1: force-wipe.
        if force_wipe:
            if extracted_dir.exists():
                shutil.rmtree(extracted_dir)
                logger.debug("force_wipe: deleted %s", extracted_dir)
            sidecar_path.unlink(missing_ok=True)

        # Step 2: clean up .tmp files.
        if extracted_dir.exists():
            for tmp in extracted_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 3: resolve the stream inventory (sidecar first, no re-probe).
        assert job_result.file is not None, "File guaranteed by JobPhase"
        self._source_file = job_result.file
        self._load_or_enumerate(job_result.file, sidecar_path)

        selected = streams_filter_plain_regex(
            [*self._subtitles, *self._attachments],
            include_pattern = job_result.config.extraction.include,
            exclude_pattern = job_result.config.extraction.exclude,
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

        for stream in self._subtitles:
            artifacts.append(self._make_stream_artifact(
                SubtitleArtifact, stream, stream.extracted_file_name(),
                on_disk_names, selected,
            ))
        for stream in self._attachments:
            artifacts.append(self._make_stream_artifact(
                AttachmentArtifact, stream, stream.extracted_file_name(),
                on_disk_names, selected,
            ))

        if self._has_chapters:
            path = extracted_dir / _CHAPTERS_DISPLAY_NAME
            artifacts.append(ChaptersArtifact(
                path   = path,
                state  = ArtifactState.COMPLETE if path.name in on_disk_names else ArtifactState.ABSENT,
                wanted = bool(streams_filter_plain_regex(
                    [_ChaptersProxy()], job_result.config.extraction.include, job_result.config.extraction.exclude,
                )),
            ))

        timestamps_file = extracted_dir / TIMESTAMPS_FILENAME
        artifacts.append(TimestampArtifact(
            path   = timestamps_file,
            state  = ArtifactState.COMPLETE if timestamps_file.name in on_disk_names else ArtifactState.ABSENT,
            wanted = self._video_required and self._video is not None,
            stream = self._video,
        ))

        self._log_inventory()
        _log_stream_table(artifacts)
        return Recovery.from_artifacts(artifacts)

    def _load_or_enumerate(self, source_file: File, sidecar_path: Path) -> None:
        """Resolve the run's stream inventory from the sidecar or ffprobe.

        Args:
            source_file: The job's live ``File`` (identity for validation).
            sidecar_path: Path to ``extraction.yaml``.

        Raises:
            RecoveryError: When ffprobe enumeration fails.
        """
        sidecar = self._load_sidecar(sidecar_path)
        if sidecar is not None:
            try:
                sidecar.validate_source(source_file)
            except SourceMismatchError as exc:
                logger.info("extraction.yaml source identity mismatch — re-enumerating: %s", exc)
            else:
                inv = sidecar.streams
                self._video = (
                    VideoStream(file=source_file, info=inv.video)
                    if inv.video is not None else None
                )
                self._audio       = [AudioStream(file=source_file, info=i) for i in inv.audio]
                self._subtitles   = [SubtitleStream(file=source_file, info=i) for i in inv.subtitles]
                self._attachments = [AttachmentStream(file=source_file, info=i) for i in inv.attachments]
                self._has_chapters = sidecar.chapters is not None
                return

        try:
            data = _probe_streams_json(source_file.path)
            video, audio, subtitles, attachments, has_chapters = _enumerate_streams(data, source_file)
        except Exception as exc:
            raise RecoveryError(f"Failed to analyse source video: {exc}") from exc
        self._video, self._audio = video[0] if video else None, audio
        self._subtitles, self._attachments, self._has_chapters = subtitles, attachments, has_chapters
        self._sidecar_dirty = True

    @staticmethod
    def _load_sidecar(path: Path) -> ExtractionSidecar | None:
        """Load ``extraction.yaml``; ``None`` when absent or unparseable."""
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return ExtractionSidecar.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — any parse failure means "re-enumerate"
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def _persist_sidecar(self, sidecar_path: Path) -> None:
        """Write the inventory to ``extraction.yaml`` (the unique info slices)."""
        sidecar = ExtractionSidecar(
            source          = self._sidecar_source(),
            streams         = StreamsInventory(
                video       = self._video.info if self._video is not None else None,
                audio       = [s.info for s in self._audio],
                subtitles   = [s.info for s in self._subtitles],
                attachments = [s.info for s in self._attachments],
            ),
            chapters = (ContainerArtifact(extracted_path=self._stream_artifact_path(_CHAPTERS_DISPLAY_NAME))
                        if self._has_chapters else None),
            timestamps_path = (self._stream_artifact_path(TIMESTAMPS_FILENAME)
                               if self._video is not None else None),
        )
        write_yaml_atomic(sidecar_path, sidecar.model_dump(exclude_none=True))
        self._sidecar_dirty = False
        logger.debug("Wrote stream inventory: %s", sidecar_path.name)

    def _sidecar_source(self) -> File:
        """The inventory's source identity — the first enumerated stream's File."""
        stream = self._video or next(iter(self._audio), None) \
            or next(iter(self._subtitles), None) or next(iter(self._attachments), None)
        assert stream is not None, "inventory has at least one stream (video required for timestamps)"
        return stream.file

    # ------------------------------------------------------------------
    # Result / finalize
    # ------------------------------------------------------------------

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[ExtractionArtifact],
        message:   str,
        error:     str | None = None,
    ) -> ExtractionPhaseResult:
        """Assemble an ``ExtractionPhaseResult`` deriving payloads from artifacts.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list.
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result.
        """
        ts = next((a for a in artifacts if isinstance(a, TimestampArtifact)), None)
        chapters = next((a for a in artifacts if isinstance(a, ChaptersArtifact)), None)

        complete_paths = {
            a.path for a in artifacts if a.state == ArtifactState.COMPLETE
        }
        subtitles = [
            s if (s.info.extracted_path and s.info.extracted_path in complete_paths)
            else s.model_copy(update={"info": s.info.model_copy(update={
                "extracted_path": a.path if a.path in complete_paths else None,
            })})
            for s, a in zip(self._subtitles, [x for x in artifacts if isinstance(x, SubtitleArtifact)])
        ]
        attachments = [
            s if (s.info.extracted_path and s.info.extracted_path in complete_paths)
            else s.model_copy(update={"info": s.info.model_copy(update={
                "extracted_path": a.path if a.path in complete_paths else None,
            })})
            for s, a in zip(self._attachments, [x for x in artifacts if isinstance(x, AttachmentArtifact)])
        ]

        return ExtractionPhaseResult(
            outcome           = outcome,
            artifacts         = artifacts,
            message           = message,
            error             = error,
            video_stream      = self._video,
            audio_streams     = list(self._audio),
            subtitle_streams  = subtitles,
            attachment_streams = attachments,
            video             = _legacy_video_metadata(self._video) if self._video is not None else None,
            timestamps_path   = ts.path if ts is not None and ts.state == ArtifactState.COMPLETE else None,
            chapters_path     = chapters.path if chapters is not None and chapters.state == ArtifactState.COMPLETE else None,
        )

    def _load_or_enumerate(self, source_file: File, sidecar_path: Path) -> None:
        """Resolve the run's stream inventory from the sidecar or ffprobe.

        Args:
            source_file: The job's live ``File`` (identity for validation).
            sidecar_path: Path to ``extraction.yaml``.

        Raises:
            RecoveryError: When ffprobe enumeration fails.
        """
        sidecar = self._load_sidecar(sidecar_path)
        if sidecar is not None:
            try:
                sidecar.validate_source(source_file)
            except SourceMismatchError as exc:
                logger.info("extraction.yaml source identity mismatch — re-enumerating: %s", exc)
            else:
                inv = sidecar.streams
                self._video = (
                    VideoStream(file=source_file, info=inv.video)
                    if inv.video is not None else None
                )
                self._audio       = [AudioStream(file=source_file, info=i) for i in inv.audio]
                self._subtitles   = [SubtitleStream(file=source_file, info=i) for i in inv.subtitles]
                self._attachments = [AttachmentStream(file=source_file, info=i) for i in inv.attachments]
                self._has_chapters = sidecar.chapters is not None
                return

        try:
            data = _probe_streams_json(source_file.path)
            video, audio, subtitles, attachments, has_chapters = _enumerate_streams(data, source_file)
        except Exception as exc:
            raise RecoveryError(f"Failed to analyse source video: {exc}") from exc
        self._video, self._audio = video[0] if video else None, audio
        self._subtitles, self._attachments, self._has_chapters = subtitles, attachments, has_chapters
        self._sidecar_dirty = True

    @staticmethod
    def _load_sidecar(path: Path) -> ExtractionSidecar | None:
        """Load ``extraction.yaml``; ``None`` when absent or unparseable."""
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return ExtractionSidecar.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — any parse failure means "re-enumerate"
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def _persist_sidecar(self, sidecar_path: Path) -> None:
        """Write the inventory to ``extraction.yaml`` (the unique info slices)."""
        sidecar = ExtractionSidecar(
            source          = self._sidecar_source(),
            streams         = StreamsInventory(
                video       = self._video.info if self._video is not None else None,
                audio       = [s.info for s in self._audio],
                subtitles   = [s.info for s in self._subtitles],
                attachments = [s.info for s in self._attachments],
            ),
            chapters = (ContainerArtifact(extracted_path=self._stream_artifact_path(_CHAPTERS_DISPLAY_NAME))
                        if self._has_chapters else None),
            timestamps_path = (self._stream_artifact_path(TIMESTAMPS_FILENAME)
                               if self._video is not None else None),
        )
        write_yaml_atomic(sidecar_path, sidecar.model_dump(exclude_none=True))
        self._sidecar_dirty = False
        logger.debug("Wrote stream inventory: %s", sidecar_path.name)

    def _sidecar_source(self) -> File:
        """The inventory's source identity — the first enumerated stream's File."""
        stream = self._video or next(iter(self._audio), None) \
            or next(iter(self._subtitles), None) or next(iter(self._attachments), None)
        assert stream is not None, "inventory has at least one stream (video required for timestamps)"
        return stream.file

    # ------------------------------------------------------------------
    # Result / finalize
    # ------------------------------------------------------------------

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[ExtractionArtifact],
        message:   str,
        error:     str | None = None,
    ) -> ExtractionPhaseResult:
        """Assemble an ``ExtractionPhaseResult`` deriving payloads from artifacts.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list.
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result.
        """
        ts = next((a for a in artifacts if isinstance(a, TimestampArtifact)), None)
        chapters = next((a for a in artifacts if isinstance(a, ChaptersArtifact)), None)

        complete_paths = {
            a.path for a in artifacts if a.state == ArtifactState.COMPLETE
        }
        subtitles = [
            s if (s.info.extracted_path and s.info.extracted_path in complete_paths)
            else s.model_copy(update={"info": s.info.model_copy(update={
                "extracted_path": a.path if a.path in complete_paths else None,
            })})
            for s, a in zip(self._subtitles, [x for x in artifacts if isinstance(x, SubtitleArtifact)])
        ]
        attachments = [
            s if (s.info.extracted_path and s.info.extracted_path in complete_paths)
            else s.model_copy(update={"info": s.info.model_copy(update={
                "extracted_path": a.path if a.path in complete_paths else None,
            })})
            for s, a in zip(self._attachments, [x for x in artifacts if isinstance(x, AttachmentArtifact)])
        ]

        return ExtractionPhaseResult(
            outcome           = outcome,
            artifacts         = artifacts,
            message           = message,
            error             = error,
            video_stream      = self._video,
            audio_streams     = list(self._audio),
            subtitle_streams  = subtitles,
            attachment_streams = attachments,
            video             = _legacy_video_metadata(self._video) if self._video is not None else None,
            timestamps_path   = ts.path if ts is not None and ts.state == ArtifactState.COMPLETE else None,
            chapters_path     = chapters.path if chapters is not None and chapters.state == ArtifactState.COMPLETE else None,
        )

    def _execute(
        self,
        wanted:  list[ExtractionArtifact],
        dry_run: bool,
    ) -> ExtractionPhaseResult:
        """Extract the ``ABSENT`` artifacts among the given wanted artifacts.

        Pure executor: ``_recover()`` already enumerated the inventory and
        decided what is wanted. Persists ``extraction.yaml`` first (the
        freshly enumerated inventory), runs the disk-space estimate on the
        real video stream data, then extracts each ``ABSENT`` artifact using
        its concrete type. ``dry_run`` is never ``True`` here (extraction is
        not a readonly-execute phase; the template previews instead).

        Args:
            wanted:  Wanted artifacts from ``_recover()`` (``wanted=True``).
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``ExtractionPhaseResult`` built directly from the updated artifacts.
        """
        artifacts = wanted
        job_result = cast(JobPhaseResult, self._dep(JobPhase).result)
        work_dir      = job_result.work_dir
        extracted_dir = work_dir / EXTRACTED_DIR
        extracted_dir.mkdir(parents=True, exist_ok=True)

        if self._sidecar_dirty:
            self._persist_sidecar(work_dir / _EXTRACTION_YAML_FILENAME)

        # Disk-space estimate on the enumerated stream data (log-only).
        if self._video is not None:
            n_strategies = len(self._config.encoding.resolved_strategies)
            log_disk_space_info(
                stream         = self._video,
                work_dir       = work_dir,
                min_strategies = 1 if (self._config.encoding.optimize or n_strategies == 0) else n_strategies,
                max_strategies = max(1, n_strategies),
            )

        source = job_result.source
        if not source.exists():
            err = f"Source video not found: {source}"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, artifacts, err, error=err)

        errors: list[str] = []

        for artifact in artifacts:
            if artifact.state != ArtifactState.ABSENT:
                continue
            if isinstance(artifact, TimestampArtifact):
                self._extract_timestamp_artifact(artifact, errors)
            elif isinstance(artifact, SubtitleArtifact):
                self._extract_subtitle_artifact(artifact, source, errors)
            elif isinstance(artifact, AttachmentArtifact):
                self._extract_attachment_artifact(artifact, source, errors)
            elif isinstance(artifact, ChaptersArtifact):
                self._extract_chapters_artifact(artifact, source, errors)

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
        errors:   list[str],
    ) -> None:
        """Extract per-frame PTS timestamps for the carried video stream.

        Updates ``artifact.state`` to ``COMPLETE`` on success.
        """
        if artifact.stream is None:
            return
        duration_s = artifact.stream.info.duration_seconds
        duration_ms = int(duration_s * 1000) if duration_s is not None else None
        try:
            _extract_timestamps(
                artifact.stream.file.path, artifact.stream.info.track_id,
                artifact.path, duration_ms=duration_ms,
            )
            if artifact.path.exists() and artifact.path.stat().st_size > 0:
                artifact.state = ArtifactState.COMPLETE
        except Exception as exc:
            err = f"Failed to extract timestamps: {exc}"
            logger.critical(err)
            errors.append(err)

    def _extract_subtitle_artifact(
        self,
        artifact: SubtitleArtifact,
        source:   Path,
        errors:   list[str],
    ) -> None:
        """Copy one subtitle stream to its artifact path via the runner."""
        if artifact.stream is None:
            return
        stream = artifact.stream
        fmt = _SUBTITLE_FFMPEG_FORMAT.get(stream.file_extension)
        request = FFmpegRequest(
            inputs       = [stream.as_input()],
            output_args  = ("-c", FFMPEG_CODEC_COPY),
            output       = artifact.path,
            output_format = fmt,
        )
        logger.debug("Extracting subtitle track %d: %s", stream.info.track_id, artifact.path.name)
        res = run_ffmpeg(request)
        if res.success and artifact.path.exists():
            artifact.state = ArtifactState.COMPLETE
        else:
            err = f"ffmpeg failed extracting subtitle track {stream.info.track_id}"
            logger.error(err)
            errors.append(err)

    def _extract_attachment_artifact(
        self,
        artifact: AttachmentArtifact,
        source:   Path,
        errors:   list[str],
    ) -> None:
        """Dump one attachment through the file-trust rule (Req 7.7).

        ``-dump_attachment`` writes directly (not a muxer output), so the
        phase wraps it: the dump targets a ``.tmp`` sibling, renamed to the
        final name only on verified success — presence at the final name
        always implies a complete write.
        """
        if artifact.stream is None:
            return
        stream  = artifact.stream
        final   = artifact.path
        tmp     = final.parent / f"{final.stem}{TEMP_SUFFIX}"
        request = FFmpegRequest(
            inputs = [FFmpegInput(path=source)],
            output_args = (
                f"-dump_attachment:{stream.info.track_id}", tmp,
                "-t", "0",
            ),
        )
        logger.debug("Extracting attachment track %d: %s", stream.info.track_id, final.name)
        res = run_ffmpeg(request)
        if res.success and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(final)
            artifact.state = ArtifactState.COMPLETE
        else:
            tmp.unlink(missing_ok=True)
            err = f"ffmpeg failed extracting attachment track {stream.info.track_id}"
            logger.error(err)
            errors.append(err)

    def _extract_chapters_artifact(
        self,
        artifact: ChaptersArtifact,
        source:   Path,
        errors:   list[str],
    ) -> None:
        """Extract chapters (mkvextract first, ffprobe-xml fallback).

        Both paths already follow the ``.tmp``-then-rename protocol.
        """
        output_file = artifact.path
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

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for the extraction phase.

        ``extracted/`` keeps its surviving small content (timestamps,
        chapters, subtitles, attachments) — deep cleanup no longer deletes it
        (Req 11.4); reproducibility is guaranteed by the atomic write
        protocol, and the artifacts are cheap to keep.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        return


    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_stream_artifact(
        self,
        cls:        type,
        stream:     SubtitleStream | AttachmentStream,
        file_name:  str,
        on_disk:    set[str],
        selected:   list[Any],
    ) -> ExtractionArtifact:
        """Build one stream artifact with orthogonal want/present facts."""
        present = file_name in on_disk
        return cls(  # type: ignore[call-arg]
            path    = self._stream_artifact_path(file_name),
            state   = ArtifactState.COMPLETE if present else ArtifactState.ABSENT,
            wanted  = stream in selected,
            stream  = stream,
        )

    def _stream_artifact_path(self, file_name: str) -> Path:
        """The artifact path inside ``extracted/``."""
        work_dir = cast(JobPhaseResult, self._dep(JobPhase).result).work_dir
        return work_dir / EXTRACTED_DIR / file_name

    def _log_inventory(self) -> None:
        """Log the enumerated video/audio streams (not extraction artifacts)."""
        if self._video is not None:
            logger.info("Video stream: %s", self._video.display_name())
        else:
            logger.info("Video stream: none")
        for stream in self._audio:
            logger.info("Audio stream: %s", stream.display_name())


class _ChaptersProxy:
    """Filter stand-in exposing the chapters artifact's display name."""

    def display_name(self) -> str:
        return _CHAPTERS_DISPLAY_NAME


def _log_stream_table(
    artifacts: list[ExtractionArtifact],
) -> None:
    """Log a 3-column artifact table: wanted, present, artifact name.

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
        logger.warning("NO extractable artifacts found.")
        return

    for artifact in artifacts:
        w_sym = SUCCESS_SYMBOL_MINOR if artifact.wanted else FAILURE_SYMBOL_MINOR
        p_sym = (
            SUCCESS_SYMBOL_MINOR
            if artifact.state == ArtifactState.COMPLETE
            else FAILURE_SYMBOL_MINOR
        )
        logger.info("   %s  %s  \"%s\"", w_sym, p_sym, artifact.path.name)
