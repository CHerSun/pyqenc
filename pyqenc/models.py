"""
Core data models for the quality-based encoding pipeline.

This module defines all data structures used throughout the pipeline,
including configuration, state tracking, and result objects.
All models use Pydantic BaseModel for validation and serialisation.
"""

import json
import logging
import re
import subprocess
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import (  # noqa: F401 (ConfigDict used in PipelineConfig)
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
)

from pyqenc.constants import TIMEOUT_SECONDS_SHORT

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
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", str(path),
        "-map", "0:v:0",
        "-c", "copy",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        stderr_lines = result.stderr.splitlines()
        # Parse frame count from last "frame=N" in stderr
        frame_count: int | None = None
        for line in reversed(stderr_lines):
            m = re.search(r"frame=\s*(\d+)", line)
            if m:
                frame_count = int(m.group(1))
                break
        return frame_count, stderr_lines
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


class PhaseStatus(Enum):
    """Status of a pipeline phase."""

    NOT_STARTED = "not_started"
    IN_PROGRESS  = "in_progress"
    COMPLETED    = "completed"
    FAILED       = "failed"


# ---------------------------------------------------------------------------
# Scene / phase metadata helpers
# ---------------------------------------------------------------------------

class SceneBoundary(BaseModel):
    """A single scene boundary detected by the scene detector.

    Attributes:
        frame:             Frame number of the boundary.
        timestamp_seconds: Timestamp in seconds of the boundary.
    """

    frame:             int
    timestamp_seconds: float


class PhaseMetadata(BaseModel):
    """Typed metadata carried by a PhaseState or PhaseUpdate.

    Attributes:
        crop_params:       Serialised crop string for the extraction phase.
        scene_boundaries:  List of detected scene boundaries for the chunking phase.
        optimal_strategy:  Optimal encoding strategy selected during optimization phase.
        test_chunk_ids:    Chunk IDs selected for optimization testing; persisted so
                           the same representative sample is reused across restarts.
    """

    crop_params:      str | None              = None
    scene_boundaries: list[SceneBoundary]     = Field(default_factory=list)
    optimal_strategy: str | None              = None
    test_chunk_ids:   list[str]               = Field(default_factory=list)


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
# Attempt / strategy / chunk state
# ---------------------------------------------------------------------------

class AttemptInfo(BaseModel):
    """Information about a single encoding attempt.

    Attributes:
        attempt_number: Sequential attempt number.
        crf:            CRF value used for this attempt.
        metrics:        Measured quality metrics (e.g., ``{'vmaf_min': 95.3}``).
        success:        Whether this attempt met quality targets.
        file_path:      Path to encoded file (if successful).
        file_size:      Size of encoded file in bytes (if successful).
    """

    attempt_number: int
    crf:            float
    metrics:        dict[str, float]
    success:        bool
    file_path:      Path | None = None
    file_size:      int  | None = None


class StrategyState(BaseModel):
    """State of chunk encoding with a specific strategy.

    Attributes:
        status:    Current status of this strategy.
        attempts:  List of encoding attempts.
        final_crf: Final successful CRF value (if completed).
    """

    status:    PhaseStatus
    attempts:  list[AttemptInfo] = Field(default_factory=list)
    final_crf: float | None      = None


class ChunkState(BaseModel):
    """State of a single chunk across strategies.

    Attributes:
        chunk_id:   Unique chunk identifier.
        strategies: State for each strategy applied to this chunk.
    """

    chunk_id:   str
    strategies: dict[str, StrategyState] = Field(default_factory=dict)


class PhaseState(BaseModel):
    """State of a single pipeline phase.

    Attributes:
        status:    Current phase status.
        timestamp: ISO timestamp of last update.
        metadata:  Typed phase-specific metadata.
    """

    status:    PhaseStatus
    timestamp: str | None         = None
    metadata:  PhaseMetadata | None = None


# ---------------------------------------------------------------------------
# Typed update objects
# ---------------------------------------------------------------------------

class PhaseUpdate(BaseModel):
    """Self-describing update for a pipeline phase.

    Attributes:
        phase:    Phase name.
        status:   New phase status.
        metadata: Optional typed phase metadata.
    """

    phase:    str
    status:   PhaseStatus
    metadata: PhaseMetadata | None = None


class ChunkUpdate(BaseModel):
    """Self-describing update for a chunk encoding attempt.

    Attributes:
        chunk_id: Chunk identifier.
        strategy: Strategy name.
        attempt:  Attempt information.
    """

    chunk_id: str
    strategy: str
    attempt:  AttemptInfo


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
        path:        Path to the video file.
        start_frame: Starting frame offset within the source video (0 for
                     source files; chunk offset for chunks).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    path:        Path
    start_frame: int = 0

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

        Only fills fields that are currently ``None``.

        Args:
            stderr_lines: Lines from ffmpeg stderr.
        """
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
        """Serialise including cached private fields for round-trip persistence."""
        base = self.model_dump()
        base["_duration_seconds"] = self._duration_seconds
        base["_frame_count"]      = self._frame_count
        base["_fps"]              = self._fps
        base["_resolution"]       = self._resolution
        base["_pix_fmt"]          = self._pix_fmt
        base["_file_size_bytes"]  = self._file_size_bytes
        return base

    @classmethod
    def model_validate_full(cls, data: dict) -> "VideoMetadata":
        """Restore a ``VideoMetadata`` from a ``model_dump_full()`` dict."""
        instance = cls.model_validate(data)
        instance._duration_seconds = data.get("_duration_seconds")
        instance._frame_count      = data.get("_frame_count")
        instance._fps              = data.get("_fps")
        instance._resolution       = data.get("_resolution")
        instance._pix_fmt          = data.get("_pix_fmt")
        instance._file_size_bytes  = data.get("_file_size_bytes")
        return instance


class ChunkVideoMetadata(VideoMetadata):
    """VideoMetadata for a video chunk, adding a stable frame-range identifier.

    The ``chunk_id`` is derived from the chunk file stem using the
    frame-range naming convention (e.g. ``'chunk.000000-000319'``).

    Attributes:
        chunk_id: Stable identifier derived from the frame-range file name.
    """

    chunk_id: str

    @classmethod
    def model_validate_full(cls, data: dict) -> "ChunkVideoMetadata":  # type: ignore[override]
        """Restore a ``ChunkVideoMetadata`` from a ``model_dump_full()`` dict."""
        instance = cls.model_validate(data)
        instance._duration_seconds = data.get("_duration_seconds")
        instance._frame_count      = data.get("_frame_count")
        instance._fps              = data.get("_fps")
        instance._resolution       = data.get("_resolution")
        instance._pix_fmt          = data.get("_pix_fmt")
        instance._file_size_bytes  = data.get("_file_size_bytes")
        return instance


# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------

class PipelineState(BaseModel):
    """Complete state of pipeline execution.

    Attributes:
        version:         State format version.
        source_video:    Metadata about the source video.
        current_phase:   Name of the currently active phase.
        phases:          State of each named phase.
        chunks_metadata: Metadata for each chunk keyed by chunk_id.
        chunks:          Encoding state of each chunk.
    """

    version:         str
    source_video:    VideoMetadata
    current_phase:   str
    phases:          dict[str, PhaseState]              = Field(default_factory=dict)
    chunks_metadata: dict[str, ChunkVideoMetadata]      = Field(default_factory=dict)
    chunks:          dict[str, ChunkState]              = Field(default_factory=dict)


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
        source_video:     Path to source video file.
        work_dir:         Working directory for intermediate files.
        quality_targets:  List of quality targets to meet.
        strategies:       List of encoding strategies to use.
        optimize:         Whether to search for optimal strategy.
        all_strategies:   Whether to produce output for all strategies.
        max_parallel:     Maximum concurrent encoding processes.
        subsample_factor: Frame subsampling for metric calculation.
        log_level:        Logging level (debug, info, warning, critical).
        crop_params:      Manual crop parameters (``None`` for auto-detect).
        video_filter:     Regex pattern to filter video streams.
        audio_filter:     Regex pattern to filter audio streams.
        keep_all:         Whether to keep all intermediate files.
        chunking_mode:    Chunking strategy — lossless FFV1 (default) or stream-copy remux.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_video:     Path
    work_dir:         Path
    quality_targets:  list[QualityTarget]
    strategies:       list[str]
    optimize:         bool         = False
    all_strategies:   bool         = False
    max_parallel:     int          = 2
    subsample_factor: int          = 10
    log_level:        str          = "info"
    crop_params:      CropParams | None  = None
    video_filter:     str | None        = None
    audio_filter:     str | None        = None
    keep_all:         bool              = False
    chunking_mode:    ChunkingMode      = ChunkingMode.LOSSLESS
