"""
Core data models for the quality-based encoding pipeline.

This module defines all data structures used throughout the pipeline,
including configuration, state tracking, and result objects.
All models use Pydantic BaseModel for validation and serialisation.
"""
# CHerSun 2026

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import (  # noqa: F401 (ConfigDict used in PipelineConfig)
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
)

from pyqenc.constants import (
    DOWN_ARROW,
    LEFT_ARROW,
    RIGHT_ARROW,
    TIMEOUT_SECONDS_SHORT,
    UP_ARROW,
)

if TYPE_CHECKING:
    from pyqenc.phases.extraction import VideoStream

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal probe helpers (module-level, not part of public API)
# ---------------------------------------------------------------------------

def _run_ffprobe_streams(path: Path) -> dict | None:
    """Run ``ffprobe -show_streams -show_format`` and return parsed JSON.

    Returns ``None`` on any failure; caller is responsible for logging.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=duration,r_frame_rate,width,height,pix_fmt:format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT_SECONDS_SHORT,
        )
        return json.loads(result.stdout)
    except Exception:
        return None


def _run_ffmpeg_null(path: Path) -> tuple[int | None, list[str]]:
    """Run ``ffmpeg -c copy -f null`` and return ``(frame_count, stderr_lines)``.

    Returns ``(None, stderr_lines)`` on failure.
    """
    from pyqenc.utils.ffmpeg_runner import (
        run_ffmpeg,  # deferred to avoid circular import
    )

    cmd: list[str | os.PathLike] = [
        "ffmpeg",
        "-i",   path,
        "-map", "0:v:0",
        "-c",   "copy",
        "-f",   "null",
        "-",
    ]
    try:
        result = run_ffmpeg(cmd, output_file=None)
        return result.frame_count, result.stderr_lines
    except Exception:
        return None, []


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ChunkingMode(Enum):
    """Controls how chunks are split from the source video.

    LOSSLESS: Re-encode each chunk to FFV1 all-intra (``-g 1``) for
              frame-perfect boundaries.  Default.
    REMUX:    Stream-copy (``-c copy``); faster and smaller chunks but
              boundaries snap to the nearest I-frame.
    """

    LOSSLESS = "lossless"
    REMUX    = "remux"


class CleanupLevel(IntEnum):
    """Controls how aggressively intermediate files are removed.

    Attributes:
        NONE:         Keep all intermediate files (default — no ``--cleanup`` flag).
        INTERMEDIATE: Delete workspace files per artifact immediately after it is
                      marked ``COMPLETE`` (``--cleanup`` with no argument).
        ALL:          Superset of ``INTERMEDIATE``; also deletes remaining
                      intermediate directories after full pipeline success
                      (``--cleanup all``).
    """

    NONE         = 0
    INTERMEDIATE = 1
    ALL          = 2


class PhaseOutcome(Enum):
    """Outcome of a completed pipeline phase execution.

    Attributes:
        COMPLETED: Phase did real work and succeeded.
        REUSED:    All artifacts existed; no work performed (valid in both modes).
        DRY_RUN:   Dry-run mode; work would be needed; pipeline stops here.
        FAILED:    Phase failed (``error`` field populated).
    """

    COMPLETED = "completed"
    REUSED    = "reused"
    DRY_RUN   = "dry_run"
    FAILED    = "failed"


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    """Typed representation of an encoding strategy.

    Replaces bare string strategy names throughout the codebase.

    Attributes:
        name:      Display form used in logs and YAML (e.g. ``slow+h265-aq``).
        safe_name: Filesystem-safe form used for directory names
                   (e.g. ``slow_h265-aq``).
    """

    name:      str
    safe_name: str

    @staticmethod
    def from_name(name: str) -> "Strategy":
        """Construct a ``Strategy`` from a display name.

        Replaces ``+`` and ``:`` with ``_`` to produce the filesystem-safe form.

        Args:
            name: Display strategy name (e.g. ``slow+h265-aq``).

        Returns:
            ``Strategy`` instance with ``safe_name`` derived from ``name``.
        """
        safe = name.replace("+", "_").replace(":", "_")
        return Strategy(name=name, safe_name=safe)


# ---------------------------------------------------------------------------
# Scene boundary
# ---------------------------------------------------------------------------

class SceneBoundary(BaseModel):
    """A single scene boundary detected by the scene detector.

    Attributes:
        frame:             Frame number of the boundary.
        timestamp_seconds: Timestamp in seconds of the boundary.
    """

    frame:             int
    timestamp_seconds: float


# ---------------------------------------------------------------------------
# Quality / codec configuration
# ---------------------------------------------------------------------------

class QualityTarget(BaseModel):
    """Quality target specification for encoding.

    Attributes:
        metric:    Metric type (vmaf, ssim, psnr).
        statistic: Statistical measure (min, median, max).
        value:     Target value for the metric.
    """

    metric:    str
    statistic: str
    value:     float

    @staticmethod
    def parse(target_str: str) -> "QualityTarget":
        """Parse quality target from string format.

        Args:
            target_str: Target string like ``'vmaf-min:95'`` or ``'ssim-med:98'``.

        Returns:
            QualityTarget instance.

        Raises:
            ValueError: If target string format is invalid.
        """
        try:
            metric_stat, value_str = target_str.split(":")
            metric, statistic = metric_stat.split("-")
            value = float(value_str)

            valid_metrics = {"vmaf", "ssim", "psnr"}
            if metric.lower() not in valid_metrics:
                raise ValueError(f"Invalid metric '{metric}'. Must be one of: {valid_metrics}")

            valid_stats = {"min", "med", "median", "max"}
            if statistic.lower() not in valid_stats:
                raise ValueError(f"Invalid statistic '{statistic}'. Must be one of: {valid_stats}")

            if statistic.lower() == "med":
                statistic = "median"

            return QualityTarget(
                metric=metric.lower(),
                statistic=statistic.lower(),
                value=value,
            )
        except (ValueError, AttributeError) as e:
            raise ValueError(
                f"Invalid quality target format: '{target_str}'. "
                f"Expected format: 'metric-stat:value' (e.g., 'vmaf-min:95')"
            ) from e

    def __str__(self) -> str:
        return f"{self.metric}-{self.statistic}≥{self.value}"

class CodecConfig(BaseModel):
    """Configuration for a video codec.

    Attributes:
        name:          Codec identifier (e.g., ``'h264-8bit'``, ``'h265-10bit'``).
        encoder:       FFmpeg encoder name (e.g., ``'libx264'``, ``'libx265'``).
        pixel_format:  Pixel format (e.g., ``'yuv420p'``, ``'yuv420p10le'``).
        default_crf:   Default CRF value for this codec.
        crf_range:     Valid CRF range as ``(min, max)`` tuple.
        presets:       List of presets supported by this encoder.
    """

    name:         str
    encoder:      str
    pixel_format: str
    default_crf:  float
    crf_range:    tuple[float, float]
    presets:      list[str] = Field(default_factory=list)


class StrategyConfig(BaseModel):
    """Parsed encoding strategy configuration.

    Attributes:
        preset:       FFmpeg preset (e.g., ``'slow'``, ``'veryslow'``).
        profile:      Profile name (e.g., ``'h265-aq'``, ``'h264-anime'``).
        codec:        Resolved codec configuration.
        profile_args: Resolved profile extra arguments.
    """

    preset:       str
    profile:      str
    codec:        CodecConfig
    profile_args: list[str]

    def to_ffmpeg_args(self, crf: float) -> list[str]:
        """Generate FFmpeg arguments for this strategy.

        Args:
            crf: CRF value to use.

        Returns:
            List of FFmpeg command-line arguments.
        """
        return [
            "-c:v",    self.codec.encoder,
            "-preset", self.preset,
            "-crf",    str(crf),
            "-pix_fmt", self.codec.pixel_format,
            *self.profile_args,
        ]



# ---------------------------------------------------------------------------
# Video metadata — lazy-loading Pydantic model
# ---------------------------------------------------------------------------

class VideoMetadata(BaseModel):
    """Metadata about a video file with transparent lazy-loading.

    Probe-derived fields (``duration_seconds``, ``fps``, ``resolution``,
    ``frame_count``) are exposed as properties backed by ``PrivateAttr``
    fields.  On first access each property triggers the appropriate probe
    call and caches the result so subsequent accesses are free.

    Two probe strategies are used:

    * ``_probe_metadata()`` — fast ``ffprobe -show_streams -show_format``
      (~175 ms).  Populates ``duration_seconds``, ``fps``, and
      ``resolution`` in a single call.
    * ``_probe_frame_count()`` — slower ``ffmpeg -c copy -f null``
      (~2-3 s).  Populates ``frame_count`` and opportunistically fills
      ``duration_seconds`` / ``fps`` / ``resolution`` from stderr if they
      are still ``None``.

    Callers never need to know whether a value was cached or freshly
    fetched.  Pass the same instance through all phases to reuse cached
    values.

    Attributes:
        path: Path to the video file.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path: Path

    # Backing fields — populated lazily or via populate_from_* helpers.
    _duration_seconds: float | None = PrivateAttr(default=None)
    _frame_count:      int   | None = PrivateAttr(default=None)
    _fps:              float | None = PrivateAttr(default=None)
    _resolution:       str   | None = PrivateAttr(default=None)
    _pix_fmt:          str   | None = PrivateAttr(default=None)
    _file_size_bytes:  int   | None = PrivateAttr(default=None)

    # ------------------------------------------------------------------
    # Lazy properties
    # ------------------------------------------------------------------

    @property
    def duration_seconds(self) -> float | None:
        """Video duration in seconds; probed on first access."""
        if self._duration_seconds is None:
            self._probe_metadata()
        return self._duration_seconds

    @property
    def fps(self) -> float | None:
        """Frames per second; probed on first access."""
        if self._fps is None:
            self._probe_metadata()
        return self._fps

    @property
    def resolution(self) -> str | None:
        """Resolution string (e.g. ``'1920x1080'``); probed on first access."""
        if self._resolution is None:
            self._probe_metadata()
        return self._resolution

    @property
    def pix_fmt(self) -> str | None:
        """Pixel format of the first video stream (e.g. ``'yuv420p'``); probed on first access."""
        if self._pix_fmt is None:
            self._probe_metadata()
        return self._pix_fmt

    @property
    def frame_count(self) -> int | None:
        """Total frame count; probed via null-encode on first access (~2-3 s)."""
        if self._frame_count is None:
            self._probe_frame_count()
        return self._frame_count

    @property
    def file_size_bytes(self) -> int | None:
        """File size in bytes; read from filesystem on first access."""
        if self._file_size_bytes is None:
            try:
                self._file_size_bytes = self.path.stat().st_size
            except OSError:
                pass
        return self._file_size_bytes

    # ------------------------------------------------------------------
    # Internal probe methods
    # ------------------------------------------------------------------

    def _probe_metadata(self) -> None:
        """Populate duration, fps, and resolution via a fast ffprobe call.

        On failure each field stays ``None`` and a warning is logged.
        """
        data = _run_ffprobe_streams(self.path)
        if data is None:
            logger.warning("ffprobe failed for %s; duration/fps/resolution unavailable", self.path)
            return
        self.populate_from_ffprobe(data)

    def _probe_frame_count(self) -> None:
        """Populate frame_count via ``ffmpeg -c copy -f null``.

        Also opportunistically fills duration/fps/resolution from stderr
        if they are still ``None``.
        """
        frame_count, stderr_lines = _run_ffmpeg_null(self.path)
        if frame_count is None:
            logger.warning("Could not determine frame count for %s", self.path)
        else:
            self._frame_count = frame_count
        # Opportunistically fill remaining fields from stderr
        if self._duration_seconds is None or self._fps is None or self._resolution is None:
            self.populate_from_ffmpeg_output(stderr_lines)

    # ------------------------------------------------------------------
    # Opportunistic population helpers (public API)
    # ------------------------------------------------------------------


    @staticmethod
    def from_stream(path: Path, stream: "VideoStream") -> "VideoMetadata":
        # TODO: proper type hinting
        # get sub-meta
        raw = stream.raw
        tags = stream.tags
        # try get resolution
        resolution = stream.resolution
        if resolution is None:
            width = raw.get('width')
            height = raw.get('height')
            if width and height:
                resolution = f"{width}x{height}"
        # try get duration
        duration_seconds_str = tags.get('DURATION')
        if duration_seconds_str:
            # Convert from HH:MM:SS.ms format into float of seconds:
            parts = duration_seconds_str.split(":") if duration_seconds_str else []
            if len(parts) == 3:
                try:
                    hours = float(parts[0])
                    minutes = float(parts[1])
                    seconds = float(parts[2])
                    duration_seconds = hours * 3600 + minutes * 60 + seconds
                except ValueError:
                    duration_seconds = None
        # try get pix_fmt
        pix_fmt = raw.get('pix_fmt', None)
        # try to get frame count
        frame_count = tags.get("NUMBER_OF_FRAMES")
        # try get fps
        fps = raw.get('r_frame_rate', None) or raw.get('avg_frame_rate')
        if not fps and frame_count and duration_seconds:
            fps = float(frame_count) / duration_seconds
        # try get
        return VideoMetadata(
            path=path,
            duration_seconds=duration_seconds,
            fps=fps,
            resolution=resolution,
            pix_fmt = pix_fmt,
            frame_count = frame_count,
        )

    def populate_from_ffprobe(self, data: dict) -> None:
        """Fill backing fields from a pre-parsed ffprobe JSON dict.

        Does not trigger any probe call.  Only fills fields that are
        currently ``None`` so existing cached values are preserved.

        Args:
            data: Parsed JSON output from ``ffprobe -show_streams -show_format``.
        """
        streams = data.get("streams", [])
        stream  = streams[0] if streams else {}
        fmt     = data.get("format", {})

        if self._duration_seconds is None:
            raw = stream.get("duration") or fmt.get("duration")
            if raw is not None:
                try:
                    self._duration_seconds = float(raw)
                except (ValueError, TypeError):
                    pass

        if self._fps is None:
            fps_str = stream.get("r_frame_rate", "")
            if fps_str and "/" in fps_str:
                try:
                    num, den = fps_str.split("/")
                    if float(den) != 0:
                        self._fps = float(num) / float(den)
                except (ValueError, ZeroDivisionError):
                    pass
            elif fps_str:
                try:
                    self._fps = float(fps_str)
                except ValueError:
                    pass

        if self._resolution is None:
            w = stream.get("width")
            h = stream.get("height")
            if w and h:
                try:
                    self._resolution = f"{int(w)}x{int(h)}"
                except (ValueError, TypeError):
                    pass

        if self._pix_fmt is None:
            pix_fmt = stream.get("pix_fmt")
            if pix_fmt:
                self._pix_fmt = str(pix_fmt)

    def populate_from_ffmpeg_output(self, stderr_lines: list[str]) -> None:
        """Fill backing fields by parsing ffmpeg stderr output.

        Parses lines such as::

            Duration: 01:30:00.04, start: 0.000000, bitrate: ...
            Stream #0:0: Video: ..., 1920x1080, 24 fps, ...
            frame=  196 fps= 66 q=-0.0 Lsize= ...

        Only fills fields that are currently ``None``.

        Args:
            stderr_lines: Lines from ffmpeg stderr.
        """
        # Parse frame count from the last "frame=N" progress line (reverse scan).
        if self._frame_count is None:
            for line in reversed(stderr_lines):
                m = re.search(r"frame=\s*(\d+)", line)
                if m:
                    try:
                        self._frame_count = int(m.group(1))
                    except (ValueError, TypeError):
                        pass
                    break

        for line in stderr_lines:
            # Duration line: "  Duration: HH:MM:SS.ss, ..."
            if self._duration_seconds is None:
                m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", line)
                if m:
                    try:
                        h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                        self._duration_seconds = h * 3600 + mn * 60 + s
                    except (ValueError, TypeError):
                        pass

            # Video stream line: "Stream #0:0: Video: ..., WxH, N fps, ..."
            if self._resolution is None or self._fps is None or self._pix_fmt is None:
                if "Video:" in line:
                    if self._resolution is None:
                        m = re.search(r"(\d{2,5})x(\d{2,5})", line)
                        if m:
                            self._resolution = f"{m.group(1)}x{m.group(2)}"
                    if self._fps is None:
                        m = re.search(r"([\d.]+)\s+fps", line)
                        if m:
                            try:
                                self._fps = float(m.group(1))
                            except ValueError:
                                pass
                    if self._pix_fmt is None:
                        # e.g. "Stream #0:0: Video: h264 (High), yuv420p(tv, bt709, progressive), ..."
                        m = re.search(r"Video:\s+\S+.*?,\s+(\w+)\(", line)
                        if not m:
                            # fallback: plain "yuv420p," without parentheses
                            m = re.search(r"Video:\s+\S+.*?,\s+(\w+),", line)
                        if m:
                            self._pix_fmt = m.group(1)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def model_dump_full(self) -> dict:
        """Serialize including cached private fields for round-trip persistence."""
        base = self.model_dump()

        # We do not serialize properties, we serialize backing private fields. This:
        #  - allows not to trigger lazy-loading properties
        #  - allows to omit None fields.
        for key, value in [
            ("duration_seconds", self._duration_seconds),
            ("frame_count",      self._frame_count),
            ("fps",              self._fps),
            ("resolution",       self._resolution),
            ("pix_fmt",          self._pix_fmt),
            ("file_size_bytes",  self._file_size_bytes),
        ]:
            if value is not None:
                base[key] = value
        return base

    @classmethod
    def model_validate_full(cls, data: dict) -> "VideoMetadata":
        """Restore a ``VideoMetadata`` from a ``model_dump_full()`` dict."""
        instance = cls.model_validate(data)
        # Manual private fields deserialization override:
        #  - allows not to trigger lazy-loading properties, while properly restoring the state.
        instance._duration_seconds = data.get("duration_seconds")
        instance._frame_count      = data.get("frame_count")
        instance._fps              = data.get("fps")
        instance._resolution       = data.get("resolution")
        instance._pix_fmt          = data.get("pix_fmt")
        instance._file_size_bytes  = data.get("file_size_bytes")
        return instance


class ChunkMetadata(VideoMetadata):
    """VideoMetadata for a video chunk, adding timestamp-based identification.

    The ``chunk_id`` is derived from the timestamp range using
    ``_chunk_name_duration(start_timestamp, end_timestamp)``.

    Attributes:
        chunk_id:        Stable identifier derived from the timestamp range.
        start_timestamp: Start timestamp of the chunk in seconds.
        end_timestamp:   End timestamp of the chunk in seconds.
    """

    chunk_id:        str
    start_timestamp: float
    end_timestamp:   float

    @classmethod
    def model_validate_full(cls, data: dict) -> "ChunkMetadata":  # type: ignore[override]
        """Restore a ``ChunkMetadata`` from a ``model_dump_full()`` dict."""
        instance = cls.model_validate(data)
        instance._duration_seconds = data.get("duration_seconds")
        instance._frame_count      = data.get("frame_count")
        instance._fps              = data.get("fps")
        instance._resolution       = data.get("resolution")
        instance._pix_fmt          = data.get("pix_fmt")
        instance._file_size_bytes  = data.get("file_size_bytes")
        return instance


# ---------------------------------------------------------------------------
# Audio / attempt metadata
# ---------------------------------------------------------------------------

class AudioMetadata(BaseModel):
    """Metadata about an extracted audio track.

    Attributes:
        path:             Path to the extracted audio file.
        codec:            Audio codec name (e.g. ``'aac'``, ``'ac3'``).
        channels:         Number of audio channels.
        language:         Language tag (e.g. ``'eng'``, ``'rus'``).
        title:            Descriptive title from track metadata (e.g. ``'Surround 5.1'``).
        duration_seconds: Duration of the audio track in seconds.
        start_timestamp:  Delay relative to video in seconds (e.g. ``0.007`` for 7 ms).
    """

    path:             Path
    codec:            str   | None = None
    channels:         int   | None = None
    language:         str   | None = None
    title:            str   | None = None
    duration_seconds: float | None = None
    start_timestamp:  float | None = None


class AttemptMetadata(BaseModel):
    """Metadata about a completed encoded chunk attempt artifact on disk.

    All fields are recoverable from the filename and filesystem alone —
    no progress tracker lookup is required.

    Attributes:
        path:            Path to the encoded attempt file.
        chunk_id:        Chunk identifier (parsed from filename stem).
        strategy:        Encoding strategy name (inferred from parent directory).
        crf:             CRF value used for this attempt.
        resolution:      Resolution string (e.g. ``'1920x800'``).
        file_size_bytes: File size in bytes.
    """

    path:            Path
    chunk_id:        str
    strategy:        str
    crf:             float
    resolution:      str
    file_size_bytes: int


# ---------------------------------------------------------------------------
# Crop parameters
# ---------------------------------------------------------------------------

class CropParams(BaseModel):
    """Black border crop parameters.

    Attributes:
        top:    Pixels to crop from top.
        bottom: Pixels to crop from bottom.
        left:   Pixels to crop from left.
        right:  Pixels to crop from right.
    """

    top:    int = 0
    bottom: int = 0
    left:   int = 0
    right:  int = 0

    def is_empty(self) -> bool:
        """Return ``True`` if no cropping is needed."""
        return not (self.top or self.bottom or self.left or self.right)

    def to_ffmpeg_filter(self) -> str:
        """Convert to ffmpeg crop filter string.

        Returns:
            FFmpeg crop filter like ``'crop=1920:800:0:140'``.
        """
        return (
            f"crop=iw-{self.left + self.right}:ih-{self.top + self.bottom}"
            f":{self.left}:{self.top}"
        )

    def __str__(self) -> str:
        """String representation for storage and display."""
        return f"{self.top} {self.bottom} {self.left} {self.right}"

    def display(self) -> str:
        """String representation for display."""
        return f"{UP_ARROW}{self.top} {DOWN_ARROW}{self.bottom} {LEFT_ARROW}{self.left} {RIGHT_ARROW}{self.right}"

    @staticmethod
    def parse(crop_str: str) -> "CropParams":
        """Parse from string format.

        Accepts 2 or 4 values:

        - 2 values: ``top bottom`` (left and right default to 0)
        - 4 values: ``top bottom left right``

        Args:
            crop_str: Crop string like ``"140 140"`` or ``"140 140 0 0"``.

        Returns:
            CropParams instance.

        Raises:
            ValueError: If format is invalid.

        Examples:
            >>> CropParams.parse("140 140")
            CropParams(top=140, bottom=140, left=0, right=0)
            >>> CropParams.parse("140 140 0 0")
            CropParams(top=140, bottom=140, left=0, right=0)
        """
        parts = crop_str.split()
        if len(parts) == 2:
            return CropParams(top=int(parts[0]), bottom=int(parts[1]), left=0, right=0)
        elif len(parts) == 4:
            return CropParams(
                top=int(parts[0]),
                bottom=int(parts[1]),
                left=int(parts[2]),
                right=int(parts[3]),
            )
        else:
            raise ValueError(
                f"Invalid crop format: '{crop_str}'. Expected 2 or 4 values "
                f"(e.g., '140 140' or '140 140 0 0')"
            )


# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------

class PipelineConfig(BaseModel):
    """Configuration for complete pipeline execution.

    Attributes:
        source_video:       Path to source video file.
        work_dir:           Working directory for intermediate files.
        quality_targets:    List of quality targets to meet.
        strategies:         List of encoding strategies to use.
        optimize:           Whether to search for optimal strategy.
        all_strategies:     Whether to produce output for all strategies.
        max_parallel:       Maximum concurrent encoding processes.
        metrics_sampling:   Frame subsampling for metric calculation.
        log_level:          Logging level (debug, info, warning, critical).
        crop_params:        Manual crop parameters (``None`` for auto-detect).
        include:            Regex pattern to include streams (applied to all stream types).
        exclude:            Regex pattern to exclude streams (applied to all stream types).
        cleanup:            Cleanup level controlling intermediate file retention.
        chunking_mode:      Chunking strategy — lossless FFV1 (default) or stream-copy remux.
        force:              When True alongside execute mode, delete all artifacts and reset state
                            when a source-file mismatch is detected, then continue with the new source.
        audio_convert:      Regex pattern selecting processed audio files to convert to the final
                            delivery format. Overrides ``audio_output.convert_filter`` from config.
        audio_codec:        Override audio codec for all conversion profiles (e.g. ``'aac'``).
        audio_base_bitrate: Base bitrate for 2.0 stereo conversion (e.g. ``'192k'``). Bitrates for
                            other channel layouts are scaled proportionally by channel count.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_video:       Path
    work_dir:           Path
    quality_targets:    list[QualityTarget]
    strategies:         list[Strategy]
    optimize:           bool              = False
    all_strategies:     bool              = False
    max_parallel:       int               = 2
    metrics_sampling:   int               = 10
    log_level:          str               = "info"
    crop_params:        CropParams | None = None
    include:            str | None        = None
    exclude:            str | None        = None
    cleanup:            CleanupLevel      = CleanupLevel.NONE
    chunking_mode:      ChunkingMode      = ChunkingMode.LOSSLESS
    force:                       bool              = False
    audio_convert:               str | None        = None
    audio_codec:                 str | None        = None
    audio_base_bitrate:          str | None        = None
    strategy_selection_tolerance: float            = 5.0
    """Tolerance percentage for strategy selection (default 5%).

    Strategies whose total encoded size is within this percentage of the best
    strategy's size are also selected as optimal.  ``0.0`` means exactly one
    strategy is selected.
    """
