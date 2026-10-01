"""
Core data models for the quality-based encoding pipeline.

This module defines all data structures used throughout the pipeline,
including configuration, state tracking, and result objects.
All models use Pydantic BaseModel for validation and serialisation.
"""
# CHerSun 2026

import logging
from decimal import Decimal
from enum import Enum, IntEnum
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pyqenc.constants import (
    DOWN_ARROW,
    LEFT_ARROW,
    RIGHT_ARROW,
    TIME_SEPARATOR_MS,
    UP_ARROW,
)
from pyqenc.utils.naming import sanitize_filesystem_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

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
    """Work-state of a pipeline phase — never the run mode.

    This enum describes only *what state the phase's work is in*; it never
    encodes whether the run was a preview (dry-run) or a real execution. Run
    mode is owned by the runner via the ``dry_run`` flag it threads.

    Attributes:
        COMPLETED: Phase did real work and succeeded.
        REUSED:    All artifacts existed; no work performed (valid in both modes).
        PENDING:   Wanted work remains (one or more artifacts are ``ABSENT`` or
                   ``PARTIAL``), independent of run mode.
        FAILED:    Phase failed (``error`` field populated).
    """

    COMPLETED = "completed"
    REUSED    = "reused"
    PENDING   = "pending"
    FAILED    = "failed"


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class Strategy(BaseModel):
    """Resolved encoding strategy — single owner of identity, codec, and ffmpeg args.

    Carries everything needed to identify the strategy — the uniform name pair
    (:meth:`display_name` for logs/sidecars, :meth:`safe_name` for filesystem
    paths; both passthroughs since profile/preset names are validated safe at
    config load) — and to encode with it: the codec's two argument stages and
    the ffmpeg arg generation.

    Attributes:
        preset:       FFmpeg preset (e.g. ``'slow'``, ``'veryslow'``).
        profile:      Profile name (e.g. ``'h265-aq'``, ``'h264-anime'``).
        codec:        Resolved codec configuration.
        profile_args: Resolved profile extra ffmpeg arguments (output stage).
    """

    model_config = ConfigDict(frozen=True)

    preset:       str
    profile:      str
    codec:        "CodecConfig"
    profile_args: list[str]

    @field_validator("preset", "profile", mode="before")
    @classmethod
    def _sanitize_dots(cls, v: str) -> str:
        """Replace ASCII dots with ``TIME_SEPARATOR_MS`` so the strategy name is dot-free."""
        return v.replace(".", TIME_SEPARATOR_MS)

    def display_name(self) -> str:
        """Display name — the composed identity, verbatim (``'h265-aq+slow'``).

        The single generator; the safe form is :meth:`safe_name`.
        """
        return f"{self.profile}+{self.preset}"

    def safe_name(self) -> str:
        """Filesystem-safe name — passthrough sanitize (validated safe at load).

        Uniform pair with :meth:`display_name` (methods, like every named
        element): consumers touching the filesystem always take the safe
        name, no per-type thinking.
        """
        return sanitize_filesystem_text(self.display_name())

    def to_output_args(self, quality: Decimal, vf_filter: str | None = None) -> list[str]:
        """Expand the codec's ``encoder_args`` template into the output-stage args.

        Substitution rules applied to every element of ``codec.encoder_args``:

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
            The expanded output-stage argument list (the runner owns ``-i``,
            ``-y`` and the ``.tmp`` muxer stage).

        Raises:
            ValueError: If ``codec.encoder_args`` still contains a legacy
                        ``'{input}'`` sentinel.
        """

        def _expand(args: list[str]) -> list[str]:
            quality_str = str(quality)
            result: list[str] = []
            for arg in args:
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
            return result

        result = _expand(self.codec.encoder_args)
        # repeat templating for profile args (they may carry sentinels too)
        result = _expand(result)

        if "{input}" in result:
            raise ValueError(
                f"Codec '{self.codec.name}' encoder_args contains a legacy "
                f"'{{input}}' sentinel — the runner owns '-i <path>'; move any "
                f"pre-input tokens to the codec's pre_input_args."
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


def targets_as_strings(targets: list[QualityTarget]) -> list[str]:
    """Serialise quality targets to ``"metric-statistic:value"`` strings.

    The canonical persisted form of a target set (``optimization.yaml``,
    ``merge.yaml``): sorted so the list is order-independent — two runs with
    the same targets in different config order serialise identically.

    Args:
        targets: Quality targets from :class:`~pyqenc.app_config.AppConfig`.

    Returns:
        Sorted list of strings like ``["vmaf-min:93.0"]``.
    """
    return sorted(f"{t.metric}-{t.statistic}:{t.value}" for t in targets)


def _coerce_decimal_pair(v: tuple | list) -> tuple[Decimal, Decimal]:
    """Coerce a two-element quality range to ``(Decimal, Decimal)``.

    The shared ``quality_range`` before-validator body: each element passes
    through ``str`` first so floats and YAML strings both land on the exact
    same ``Decimal`` value.  Config order is preserved — the caller's
    convention (first = better end) decides meaning, not the helper.

    Args:
        v: The two-element range (ints, floats, or strings).

    Returns:
        The pair as ``Decimal`` values.
    """
    return Decimal(str(v[0])), Decimal(str(v[1]))


class CodecConfig(BaseModel):
    """Configuration for a video codec.

    Attributes:
        name:            Codec identifier (e.g., ``'h264-8bit'``, ``'h265-10bit'``).
        default_quality: Default quality parameter value for this codec.
        default_preset:  Default preset used when a strategy pattern omits the preset part
                         (e.g. ``'h265*'`` expands using each codec's ``default_preset``).
                         Must be one of the values in ``presets``.
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
        pre_input_args:      Input-stage tokens emitted before the input's
                             window flags and ``-i`` (e.g. ``-hwaccel`` /
                             ``-init_hw_device`` vulkan setup). The single
                             source of truth for the default is here in the
                             bundled config.
        encoder_args:        The output-stage ffmpeg argument template (the
                         ``"-i", "{input}"`` pair is gone — the runner owns
                         ``-i <path>``).  Sentinels:

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
    default_preset:      str
    quality_range:       tuple[Decimal, Decimal]
    quality_label:       str            = "CRF"
    quality_granularity: Decimal        = Decimal("0.5")
    quality_max_step:    Decimal|None   = None
    pre_input_args:      list[str]      = Field(default_factory=list)
    encoder_args:        list[str]      = Field(default_factory=list)
    presets:             list[str]      = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_default_preset(self) -> "CodecConfig":
        """Ensure ``default_preset`` is a member of ``presets``.

        Raises:
            ValueError: If ``default_preset`` is not in the ``presets`` list.
        """
        if self.default_preset not in self.presets:
            raise ValueError(
                f"Codec '{self.name}': default_preset '{self.default_preset}' "
                f"is not in the presets list {self.presets}."
            )
        return self

    @property
    def quality_better(self) -> Decimal:
        """The quality value representing the *better* end of the range (``quality_range[0]``)."""
        return self.quality_range[0]

    @property
    def quality_worse(self) -> Decimal:
        """The quality value representing the *worse* end of the range (``quality_range[1]``)."""
        return self.quality_range[1]

    @field_validator("quality_range", mode="before")
    @classmethod
    def _normalise_quality_range(
        cls, v: tuple[Decimal | float | str, Decimal | float | str] | list,
    ) -> tuple[Decimal, Decimal]:
        """Convert ``quality_range`` elements to ``Decimal``, preserving config order.

        ``quality_range[0]`` is always the *better* end as specified in config.
        """
        return _coerce_decimal_pair(v)

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
    def _to_decimal(cls, v: Decimal | float | str | None) -> Decimal | None:
        """Coerce numeric config values to ``Decimal`` for exact arithmetic."""
        if v is None:
            return None
        return Decimal(str(v))




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
        return f"{self.top},{self.bottom},{self.left},{self.right}"

    def display(self) -> str:
        """String representation for display."""
        return f"{UP_ARROW}{self.top} {DOWN_ARROW}{self.bottom} {LEFT_ARROW}{self.left} {RIGHT_ARROW}{self.right}"

    @staticmethod
    def parse(crop_str: str) -> "CropParams":
        """Parse from comma-separated string format.

        Accepts 2 or 4 comma-separated values:

        - 2 values: ``top,bottom`` (left and right default to 0)
        - 4 values: ``top,bottom,left,right``

        Args:
            crop_str: Crop string like ``"140,140"`` or ``"140,140,0,0"``.

        Returns:
            CropParams instance.

        Raises:
            ValueError: If format is invalid.

        Examples:
            >>> CropParams.parse("140,140")
            CropParams(top=140, bottom=140, left=0, right=0)
            >>> CropParams.parse("140,140,0,0")
            CropParams(top=140, bottom=140, left=0, right=0)
        """
        parts = crop_str.split(",")
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
                f"Invalid crop format: '{crop_str}'. Expected 2 or 4 comma-separated values "
                f"(e.g., '140,140' or '140,140,0,0')"
            )
