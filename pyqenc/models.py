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
from decimal import Decimal
from enum import Enum, IntEnum
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import (  # noqa: F401 (ConfigDict used in PipelineConfig)
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
)

from pyqenc.constants import (
    DEFAULT_MAX_PARALLEL,
    DEFAULT_METRICS_SAMPLING,
    DOWN_ARROW,
    LEFT_ARROW,
    RIGHT_ARROW,
    TIME_SEPARATOR_MS,
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
        "-show_entries", "stream=duration,r_frame_rate,avg_frame_rate,width,height,pix_fmt:format=duration",
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


class Strategy(BaseModel):
    """Resolved encoding strategy — single owner of identity, codec, and ffmpeg args.

    Carries everything needed to identify the strategy (``name``/``safe_name``
    for logs, YAML keys, and filesystem paths) and to encode with it
    (codec config, profile args, ffmpeg arg generation).

    Attributes:
        preset:       FFmpeg preset (e.g. ``'slow'``, ``'veryslow'``).
        profile:      Profile name (e.g. ``'h265-aq'``, ``'h264-anime'``).
        codec:        Resolved codec configuration.
        profile_args: Resolved profile extra ffmpeg arguments.
    """

    model_config = ConfigDict(frozen=True)

    preset:       str
    profile:      str
    codec:        "CodecConfig"
    profile_args: list[str]

    @field_validator("preset", "profile", mode="before")
    @classmethod
    def _sanitize_dots(cls, v: str) -> str:
        """Replace ASCII dots with ``TIME_SEPARATOR_MS`` so ``strategy.name`` is dot-free."""
        return v.replace(".", TIME_SEPARATOR_MS)

    @property
    def name(self) -> str:
        """Display name used in logs and YAML (e.g. ``'slow+h265-aq'``)."""
        return f"{self.preset}+{self.profile}"

    @property
    def safe_name(self) -> str:
        """Filesystem-safe name for directory paths (e.g. ``'slow_h265-aq'``)."""
        return self.name.replace(":", "_")

    def to_ffmpeg_args(self, quality: Decimal, vf_filter: str | None = None) -> list[str]:
        """Expand the codec's ``encoder_args`` template into a concrete ffmpeg argument list.

        Substitution rules applied to every element of ``codec.encoder_args``:

        - ``'{input}'``       → kept as-is for the caller to replace with the
          actual input ``Path``.  The preceding ``'-i'`` flag is a separate
          element in the template.  Everything before ``'-i'`` is pre-input
          (e.g. ``-hwaccel`` options).
        - ``'{quality}'``     → replaced with ``str(quality)``.  The ``Decimal``
          value is already quantized to the codec's granularity, so ``str()``
          produces the correct representation (e.g. ``'18.5'``, ``'19'``).
          May appear multiple times (e.g. ``-cq:v {quality} -qmin {quality}``).
        - ``'{preset}'``      → replaced with ``self.preset``.
        - ``'{profile_args}'``→ expanded to ``self.profile_args`` in-place.
        - ``'{vf}'``          → replaced with the vf filter expression string
          (e.g. ``'crop=1920:800:0:140'``).  When ``{vf}`` is the entire
          element and *vf_filter* is empty/``None``, the element is silently
          dropped.  When ``{vf}`` is embedded inside a larger filter chain
          (e.g. ``'scale_cuda=format=p010le:{vf}'``), it is replaced with the
          filter string or with an empty string — a trailing ``:`` is left in
          place, which ffmpeg tolerates.

        Args:
            quality:   Quality parameter value as a ``Decimal`` already quantized
                       to the codec's granularity (CRF for x264/x265, CQ for nvenc, …).
            vf_filter: Optional ffmpeg video filter expression (e.g. ``'crop=1920:800:0:140'``).

        Returns:
            Expanded argument list with ``'{input}'`` still present as a string
            sentinel for the caller to substitute with the actual ``Path``.

        Raises:
            ValueError: If ``codec.encoder_args`` contains no ``'{input}'`` sentinel.
        """
        quality_str = str(quality)
        result: list[str] = []
        for arg in self.codec.encoder_args:
            if arg == "{profile_args}":
                result.extend(self.profile_args)
            elif arg == "{vf}":
                if vf_filter:
                    result.append(vf_filter)
                else:
                    # Standalone {vf} with no filter — also drop the preceding -vf flag
                    if result and result[-1] == "-vf":
                        result.pop()
            else:
                expanded = arg.replace("{quality}", quality_str).replace("{preset}", self.preset)
                # {vf} embedded inside a larger filter chain string
                if "{vf}" in expanded:
                    expanded = expanded.replace("{vf}", vf_filter or "")
                result.append(expanded)
        # repeat templating for profile args
        expanded_args = result
        result = []
        for arg in expanded_args:
            if arg == "{profile_args}":
                result.extend(self.profile_args)
            elif arg == "{vf}":
                if vf_filter:
                    result.append(vf_filter)
                else:
                    # Standalone {vf} with no filter — also drop the preceding -vf flag
                    if result and result[-1] == "-vf":
                        result.pop()
            else:
                expanded = arg.replace("{quality}", quality_str).replace("{preset}", self.preset)
                # {vf} embedded inside a larger filter chain string
                if "{vf}" in expanded:
                    expanded = expanded.replace("{vf}", vf_filter or "")
                result.append(expanded)

        if "{input}" not in result:
            raise ValueError(
                f"Codec '{self.codec.name}' encoder_args must contain a '{{input}}' sentinel"
            )
        return result


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
        statistic: Statistical measure (min, median, max, p05, p25, p75, p95).
        value:     Target value for the metric.
    """

    metric:    str
    statistic: str
    value:     float

    @staticmethod
    def parse(target_str: str) -> "QualityTarget":
        """Parse quality target from string format.

        Args:
            target_str: Target string like ``'vmaf-min:95'``, ``'ssim-med:98'``,
                        or ``'vmaf-p25:90'``.

        Returns:
            QualityTarget instance.

        Raises:
            ValueError: If target string format is invalid.
        """
        try:
            metric_stat, value_str = target_str.split(":")
            metric, statistic = metric_stat.split("-")
            value = float(value_str)

            from pyqenc.quality import MetricType  # deferred to avoid circular import
            valid_metrics = {m.value for m in MetricType}
            if metric.lower() not in valid_metrics:
                raise ValueError(f"Invalid metric '{metric}'. Must be one of: {sorted(valid_metrics)}")

            valid_stats = {"min", "med", "median", "max", "p05", "p25", "p75", "p95"}
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
        name:            Codec identifier (e.g., ``'h264-8bit'``, ``'h265-10bit'``).
        default_quality: Default quality parameter value for this codec.
        quality_range:   Valid quality range as ``(first, last)`` tuple stored in config order.
                         ``quality_range[0]`` is always the *better* end (lower CRF = better quality,
                         higher bitrate = better quality).  For CRF/CQ/QP codecs use ``[0, 51]``
                         (0 = lossless, 51 = worst).  For VBR bitrate codecs use ``[99, 0]``
                         (99 Mbit/s = best, 0 = worst).  Order is preserved as-is from config.
        quality_label:       Human-readable label for the quality parameter used in logs
                             and plots (e.g. ``'CRF'``, ``'CQ'``).
        quality_granularity: Step size for the quality search algorithm.  CRF/CQ codecs
                             typically use ``0.5``; QP-based codecs prefer ``1.0`` (integer
                             steps).  The search result is rounded to the nearest multiple
                             of this value.
        encoder_args:        Full ffmpeg argument template for this codec.  Sentinels:

                         - ``'-i'`` + ``'{input}'`` — two consecutive items; ``{input}``
                           is replaced with the actual input ``Path`` at runtime.
                           Args before ``'-i'`` are pre-input (e.g. ``-hwaccel``).
                         - ``'{quality}'`` — replaced with the quality value; may appear
                           multiple times (e.g. ``-cq:v {quality} -qmin {quality}``).
                         - ``'{preset}'`` — replaced with the strategy preset name.
                         - ``'{profile_args}'`` — expanded to the profile's extra args.
                         - ``'{vf}'`` — replaced with the vf filter expression when
                           active (e.g. crop), or silently dropped when standalone
                           and no filter is set.  Can be embedded inside a larger
                           filter chain: ``'scale_cuda=format=p010le:{vf}'``.
        presets:         List of presets supported by this encoder.
    """

    name:                str
    default_quality:     Decimal
    quality_range:       tuple[Decimal, Decimal]
    quality_label:       str            = "CRF"
    quality_granularity: Decimal        = Decimal("0.5")
    quality_max_step:    Decimal|None   = None
    encoder_args:        list[str]      = Field(default_factory=list)
    presets:             list[str]      = Field(default_factory=list)

    @property
    def quality_better(self) -> Decimal:
        """The quality value representing the *better* end of the range (``quality_range[0]``)."""
        return self.quality_range[0]

    @property
    def quality_worse(self) -> Decimal:
        """The quality value representing the *worse* end of the range (``quality_range[1]``)."""
        return self.quality_range[1]

    @property
    def quality_higher_is_better(self) -> bool:
        """``True`` when a higher quality value means better quality (e.g. VBR bitrate).

        Derived from ``quality_range``: when ``quality_range[0] > quality_range[1]``,
        higher values are better (e.g. ``[99, 0]`` for Mbit/s).
        When ``quality_range[0] < quality_range[1]``, lower values are better
        (e.g. ``[0, 51]`` for CRF/CQ/QP).
        """
        return self.quality_range[0] > self.quality_range[1]

    @field_validator("quality_range", mode="before")
    @classmethod
    def _normalise_quality_range(
        cls, v: tuple[Decimal | float | str, Decimal | float | str] | list,
    ) -> tuple[Decimal, Decimal]:
        """Convert ``quality_range`` elements to ``Decimal``, preserving config order.

        ``quality_range[0]`` is always the *better* end as specified in config.
        """
        a, b = Decimal(str(v[0])), Decimal(str(v[1]))
        return a, b

    @property
    def quality_log_padding(self) -> int:
        """Computed column width for quality parameter log formatting.

        Derives the correct padding width from the codec's own range and granularity
        so log columns align correctly for any codec (e.g. CRF 0–51 with gran 0.5 → 4 chars;
        VBR 0–100 with gran 0.1 → 5 chars; QP 0–63 with gran 1 → 2 chars).
        """
        max_val = max(abs(self.quality_better), abs(self.quality_worse))
        return len(str(Decimal(str(max_val)).quantize(self.quality_granularity)))

    @field_validator("default_quality", "quality_granularity", "quality_max_step", mode="before")
    @classmethod
    def _to_decimal(cls, v: Decimal | float | int | str | None) -> Decimal | None:
        """Coerce numeric config values to ``Decimal`` for exact arithmetic."""
        if v is None:
            return None
        return Decimal(str(v))




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
    _duration_seconds: float    | None = PrivateAttr(default=None)
    _frame_count:      int      | None = PrivateAttr(default=None)
    _fps:              float    | None = PrivateAttr(default=None)
    _fps_fraction:     Fraction | None = PrivateAttr(default=None)
    _resolution:       str      | None = PrivateAttr(default=None)
    _pix_fmt:          str      | None = PrivateAttr(default=None)
    _file_size_bytes:  int      | None = PrivateAttr(default=None)

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
    def fps_fraction(self) -> Fraction | None:
        """Exact rational FPS as a Fraction; probed on first access via avg_frame_rate."""
        if self._fps_fraction is None:
            self._probe_metadata()
        return self._fps_fraction

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

    async def _probe_frame_count_async(self) -> None:
        """Async variant of ``_probe_frame_count`` — safe to call from a running event loop.

        Uses ``run_ffmpeg_async`` directly to avoid the sync-in-async deadlock.
        Also opportunistically fills duration/fps/resolution from stderr.
        """
        import os as _os

        from pyqenc.utils.ffmpeg_runner import run_ffmpeg_async

        cmd: list[str | _os.PathLike] = [
            "ffmpeg",
            "-i",   self.path,
            "-map", "0:v:0",
            "-c",   "copy",
            "-f",   "null",
            "-",
        ]
        try:
            result = await run_ffmpeg_async(cmd, output_file=None)
            if result.frame_count is None:
                logger.warning("Could not determine frame count for %s", self.path)
            else:
                self._frame_count = result.frame_count
            if self._duration_seconds is None or self._fps is None or self._resolution is None:
                self.populate_from_ffmpeg_output(result.stderr_lines)
        except Exception as exc:
            logger.warning("Async frame count probe failed for %s: %s", self.path, exc)


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

        if self._fps_fraction is None:
            avg_fps_str = stream.get("avg_frame_rate", "")
            if avg_fps_str and "/" in avg_fps_str:
                try:
                    num_s, den_s = avg_fps_str.split("/")
                    num, den = int(num_s), int(den_s)
                    if den != 0:
                        self._fps_fraction = Fraction(num, den)
                except (ValueError, TypeError):
                    pass

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
        if self._fps_fraction is not None:
            base["fps_fraction"] = [self._fps_fraction.numerator, self._fps_fraction.denominator]
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
        fps_frac = data.get("fps_fraction")
        if isinstance(fps_frac, list) and len(fps_frac) == 2:
            instance._fps_fraction = Fraction(int(fps_frac[0]), int(fps_frac[1]))
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
    def model_validate_full(cls, data: dict) -> "ChunkMetadata":
        """Restore a ``ChunkMetadata`` from a ``model_dump_full()`` dict."""
        instance = cls.model_validate(data)
        instance._duration_seconds = data.get("duration_seconds")
        instance._frame_count      = data.get("frame_count")
        instance._fps              = data.get("fps")
        instance._resolution       = data.get("resolution")
        instance._pix_fmt          = data.get("pix_fmt")
        instance._file_size_bytes  = data.get("file_size_bytes")
        fps_frac = data.get("fps_fraction")
        if isinstance(fps_frac, list) and len(fps_frac) == 2:
            instance._fps_fraction = Fraction(int(fps_frac[0]), int(fps_frac[1]))
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
    crf:             Decimal
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
    """Whether to search for optimal strategy (optimization phase)."""
    max_parallel:       int               = DEFAULT_MAX_PARALLEL
    """Maximum concurrent encoding processes."""
    metrics_sampling:   int               = DEFAULT_METRICS_SAMPLING
    """Frame subsampling for metric calculation. 10 is the default - good tradeoff between speed and accuracy."""
    log_level:          str               = "info"
    crop_params:        CropParams | None = None
    include:            str | None        = None
    exclude:            str | None        = None
    cleanup:            CleanupLevel      = CleanupLevel.NONE
    chunking_mode:      ChunkingMode      = ChunkingMode.LOSSLESS
    """Which chunking mode to use - lossless FFV1 reencoding (default) or stream-copy remux (bad, but cheaper)."""
    force:                       bool              = False
    """A flag indicating whether user agrees to force actions, like forced cleanup."""
    audio_convert:               str | None        = None
    audio_codec:                 str | None        = None
    audio_base_bitrate:          str | None        = None
    strategy_selection_tolerance: float            = 5.0
    """Tolerance percentage for strategy selection (default 5%).

    Strategies whose total encoded size is within this percentage of the best
    strategy's size are also selected as optimal.  ``0.0`` means exactly one
    strategy is selected.
    """
    visual_hash:                 bool              = True
    """Whether to display extra visual cue for encoding logging."""
    no_metrics:                  bool              = False
    """When True, suppress metrics.yaml output (NoOpMetricsCollector is used)."""
