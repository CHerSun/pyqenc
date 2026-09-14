"""Filter-type registry and self-contained filter-type classes.

Filter types are an **open registry**, not a closed union. Each type is a
self-contained class owning three things:

1. its type id (``type_id``),
2. its Pydantic parameter model (``params_model``, with ``extra="forbid"`` so
   invalid params raise at config load), and
3. its realisation behaviour via the single stateless contract
   :meth:`FilterType.resolve` — including its pass count and, for two-pass
   filters, how it scrapes the measured value out of a measurement pass.

A new filter type is added by defining one ``@register_filter`` class; no edits
to ``AudioConfig``, the chain executor, or existing filter classes are required.
Registering two classes with the same ``type_id`` fails at import time.

The single contract the executor drives is::

    resolve(layout, last_output) -> FilterStep

- **Single-pass filters** (``dynaudnorm``, ``downmix``, ``encode``,
  ``passthrough``) ignore ``last_output`` and return ``needs_pass=False``.
- **Two-pass filters** (``peaknorm``, ``loudnorm``) return, on the first call
  (``last_output is None``), ``needs_pass=True`` with the analysis ``-af``; the
  executor runs that as a measurement pass and calls ``resolve`` again with the
  result, which the filter scrapes (its own regex/JSON parse lives here) to
  build the final ``-af`` fragment.

All filter-specific knowledge (loudnorm JSON regex, ``volumedetect``
``max_volume`` regex, downmix matrices) lives inside the owning filter class; the
executor knows only the ``resolve``/:class:`FilterStep` contract.

``EncodeParams`` is defined here (rather than in ``chain.py``) because both
``FilterStep.output_format`` and :class:`EncodeFilter` need it, and ``chain.py``
imports from this module (defining it there would create a circular import).
This module is its canonical home.
"""
# CHerSun 2026

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict

from pyqenc.audio.layout import ChannelLayout
from pyqenc.audio.matrices import DOWNMIX_FORMAT, DOWNMIX_MATRICES, layout_channels
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult

# ---------------------------------------------------------------------------
# Terminal output target
# ---------------------------------------------------------------------------

class EncodeParams(BaseModel):
    """The terminal output format a chain resolves to.

    Carries the codec, the per-channel bitrate (scaled by the output channel
    count at execution time), and the output file extension. Set by an
    ``encode`` filter's :class:`FilterStep`; the chain executor holds the last
    non-``None`` value as the effective output target (FLAC default otherwise).

    Attributes:
        codec:               ffmpeg audio codec (``-c:a`` value), e.g. ``aac``.
        bitrate_per_channel: Per-channel bitrate string (e.g. ``64k``); scaled
                             by the final layout's channel count at execution.
        extension:           Output file extension (without the dot), e.g.
                             ``m4a`` or ``flac``.
    """

    model_config = ConfigDict(frozen=True)

    codec:               str
    bitrate_per_channel: str
    extension:           str


# ---------------------------------------------------------------------------
# Filter contract
# ---------------------------------------------------------------------------

@dataclass
class FilterStep:
    """One filter's contribution to the chain, returned by :meth:`FilterType.resolve`.

    The executor consumes this without inspecting the filter's concrete type.

    Attributes:
        af:            The ``-af`` contribution. ``""`` for a no-op / terminal
                       filter (never ``None``); the executor drops empty
                       fragments from the comma-join.
        needs_pass:    ``True`` = run a measurement pass with ``af`` (output to
                       null), then call :meth:`FilterType.resolve` again with the
                       result. ``False`` = the fragment is final.
        out_layout:    The channel layout after this filter (``downmix`` updates
                       it; every other filter passes the incoming layout through).
        output_format: Set only by ``encode``-type filters; the executor keeps
                       the last non-``None`` value as the effective output target.
    """

    af:            str
    needs_pass:    bool
    out_layout:    ChannelLayout
    output_format: EncodeParams | None = None


class FilterType(ABC):
    """Abstract base for a registered filter type.

    A filter instance is **stateless and re-entrant**: it holds only its
    validated parameters and is reused across every (track, chain). Everything a
    later ``resolve`` call needs arrives via ``last_output`` — never stored on
    the instance.

    Subclasses declare :attr:`type_id` and :attr:`params_model` as class
    variables and implement :meth:`resolve`.
    """

    type_id:      ClassVar[str]
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, params: BaseModel) -> None:
        """Store the validated params model instance for this filter.

        Args:
            params: An instance of this type's :attr:`params_model`, already
                validated by config load.
        """
        self._params = params

    @abstractmethod
    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Return this filter's :class:`FilterStep` for the current layout.

        Args:
            layout:      The channel layout entering this filter.
            last_output: The result of this filter's most recent measurement
                         pass, or ``None`` if it has not run one yet. It is
                         **only ever this filter's own latest pass** — never
                         accumulated history — and is cleared the moment the
                         filter finishes.

        Returns:
            The :class:`FilterStep` describing this filter's contribution.
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_FILTER_REGISTRY: dict[str, type[FilterType]] = {}
"""Type id → filter-type class. Populated by :func:`register_filter` at import."""


def register_filter(cls: type[FilterType]) -> type[FilterType]:
    """Register a filter-type class under its ``type_id``.

    Used as a class decorator. Rejects a duplicate ``type_id`` at import time so
    two classes cannot claim the same id.

    Args:
        cls: The :class:`FilterType` subclass to register.

    Returns:
        The same class (so it can be used as a decorator).

    Raises:
        ValueError: If a class is already registered for ``cls.type_id``.
    """
    if cls.type_id in _FILTER_REGISTRY:
        raise ValueError(f"Duplicate filter type id: {cls.type_id!r}")
    _FILTER_REGISTRY[cls.type_id] = cls
    return cls


def get_filter_class(type_id: str) -> type[FilterType]:
    """Return the registered filter-type class for ``type_id``.

    Args:
        type_id: A filter-type id (e.g. ``peaknorm``).

    Returns:
        The registered :class:`FilterType` subclass.

    Raises:
        KeyError: When no class is registered for ``type_id``. Callers (config
            validation) surface this as a ``ValidationError`` listing
            :func:`registered_type_ids`.
    """
    return _FILTER_REGISTRY[type_id]


def registered_type_ids() -> list[str]:
    """Return the sorted list of currently registered filter-type ids."""
    return sorted(_FILTER_REGISTRY)


# ---------------------------------------------------------------------------
# Parameter models
# ---------------------------------------------------------------------------

class PeakNormParams(BaseModel):
    """Parameters for the ``peaknorm`` filter."""

    model_config = ConfigDict(extra="forbid")

    target_dbfs: float


class LoudNormParams(BaseModel):
    """Parameters for the ``loudnorm`` filter (EBU R128 targets)."""

    model_config = ConfigDict(extra="forbid")

    i:   float
    tp:  float
    lra: float


class DynAudNormParams(BaseModel):
    """Parameters for the ``dynaudnorm`` filter (dynamic normalisation).

    Field names are ffmpeg's **long** option names (not the single-letter
    aliases) for readability. To inspect the full set of options a filter
    accepts — to revise or extend these later — run::

        ffmpeg -h filter=dynaudnorm

    (general form ``ffmpeg -h filter=<name>``). The values used here map to
    ``dynaudnorm=framelen=<>:gausssize=<>:peak=<>:maxgain=<>:targetrms=<>``.
    """

    model_config = ConfigDict(extra="forbid")

    framelen:  int      # analysis window length in ms (ffmpeg ``framelen``/``f``)
    gausssize: int      # Gaussian window size in frames (ffmpeg ``gausssize``/``g``)
    peak:      float    # target peak magnitude, 0..1 (ffmpeg ``peak``/``p``)
    maxgain:   float    # maximum gain factor (ffmpeg ``maxgain``/``m``)
    targetrms: float    # target RMS, 0 = disabled (ffmpeg ``targetrms``/``r``)


class DownmixParams(BaseModel):
    """Parameters for the ``downmix`` filter."""

    model_config = ConfigDict(extra="forbid")

    to:     str
    matrix: str | None = None


class EncodeFilterParams(BaseModel):
    """Parameters for the ``encode`` filter."""

    model_config = ConfigDict(extra="forbid")

    codec:               str
    bitrate_per_channel: str
    extension:           str


class PassthroughParams(BaseModel):
    """Parameters for the ``passthrough`` filter — none accepted."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Two-pass scraping regexes (owned by the two-pass filter classes)
# ---------------------------------------------------------------------------

_LOUDNORM_JSON_RE: Final = re.compile(
    r"\[Parsed_loudnorm[^\]]*\]\s*(\{.*?\})",
    re.DOTALL,
)
"""Extracts the ``loudnorm`` JSON measurement block from ffmpeg stderr."""

_VOLUMEDETECT_RE: Final = re.compile(r"max_volume:\s*([-\d.]+)\s*dB")
"""Extracts the ``max_volume`` reading from ``volumedetect`` stderr output."""
_ASTATS_PEAK_VOLUME_RE: Final = re.compile(r"Peak level dB:\s*([-\d.]+)")
"""Extracts the ``peak_volume`` reading from ``astats`` stderr output."""
# Sample line:
# [Parsed_astats_2 @ ...] Peak level dB: 2.269108

# ---------------------------------------------------------------------------
# Registered filter types
# ---------------------------------------------------------------------------

@register_filter
class PeakNormFilter(FilterType):
    """Two-pass peak normalisation: measure peak (``volumedetect``), apply gain.

    Preserves source sample rate and bit depth — it only applies a ``volume``
    gain to shift the measured peak to ``target_dbfs``.
    """

    type_id      = "peaknorm"
    params_model = PeakNormParams

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Measure on the first call, apply the measured gain on the second."""
        if last_output is None:
            # DON'T use `volumedetect`. It uses 16-bit samples internally and cannot represent positive volume.
            # Use dbl/flt format for downmixing and `astats` for peak measurement instead.
            # Pass 1 — measure the peak via astats (must not be clipped before that! use dbl/flt format for downmixing).
            return FilterStep(
                af         = "astats",
                needs_pass = True,
                out_layout = layout,
            )
        # Pass 2 — scrape peak volume and apply the corrective gain.
        stderr_text = "\n".join(last_output.stderr_lines)
        match       = _ASTATS_PEAK_VOLUME_RE.search(stderr_text)
        if not match:
            raise RuntimeError(
                "peaknorm did not produce a parseable peak volume line "
                f"(ffmpeg exit code {last_output.returncode})"
            )
        params        = self._params
        assert isinstance(params, PeakNormParams)
        max_volume_db = float(match.group(1))
        gain_db       = params.target_dbfs - max_volume_db
        return FilterStep(
            af         = f"volume={gain_db:.4f}dB",
            needs_pass = False,
            out_layout = layout,
        )


@register_filter
class LoudNormFilter(FilterType):
    """Two-pass EBU R128 loudness normalisation (``loudnorm``).

    Pass 1 measures integrated loudness / true peak / range via
    ``loudnorm=…:print_format=json``; pass 2 linear-normalises using the
    measured values scraped from the pass-1 JSON block.
    """

    type_id      = "loudnorm"
    params_model = LoudNormParams

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Analyse on the first call, linear-normalise on the second."""
        params = self._params
        assert isinstance(params, LoudNormParams)
        targets = f"I={params.i}:TP={params.tp}:LRA={params.lra}"
        if last_output is None:
            # Pass 1 — analysis only (no output file).
            return FilterStep(
                af         = f"loudnorm={targets}:print_format=json",
                needs_pass = True,
                out_layout = layout,
            )
        # Pass 2 — scrape measured values and linear-normalise.
        measured = self._scrape_measurements(last_output)
        af = (
            f"loudnorm={targets}"
            f":linear=true"
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            f":print_format=none"
        )
        return FilterStep(af=af, needs_pass=False, out_layout=layout)

    @staticmethod
    def _scrape_measurements(last_output: FFmpegRunResult) -> dict[str, str]:
        """Extract the loudnorm measured values from a measurement pass result.

        Args:
            last_output: The pass-1 ffmpeg result carrying the JSON block in
                ``stderr_lines``.

        Returns:
            The measured fields needed for the linear-normalisation pass.

        Raises:
            RuntimeError: When no parseable loudnorm JSON block is present.
        """
        stderr_text = "\n".join(last_output.stderr_lines)
        match       = _LOUDNORM_JSON_RE.search(stderr_text)
        if not match:
            raise RuntimeError(
                "loudnorm did not produce a parseable JSON block "
                f"(ffmpeg exit code {last_output.returncode})"
            )
        try:
            measured = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse loudnorm JSON: {exc}") from exc
        return {
            "input_i":       str(measured["input_i"]),
            "input_tp":      str(measured["input_tp"]),
            "input_lra":     str(measured["input_lra"]),
            "input_thresh":  str(measured["input_thresh"]),
            "target_offset": str(measured["target_offset"]),
        }


@register_filter
class DynAudNormFilter(FilterType):
    """Single-pass dynamic audio normalisation (``dynaudnorm``).

    Emits ``dynaudnorm=framelen=<>:gausssize=<>:peak=<>:maxgain=<>:targetrms=<>``
    using ffmpeg's **long** option names. To inspect the filter's full option
    set (to revise these params later) run ``ffmpeg -h filter=dynaudnorm``
    (general form ``ffmpeg -h filter=<name>``).
    """

    type_id      = "dynaudnorm"
    params_model = DynAudNormParams

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Return the single-pass ``dynaudnorm`` fragment (ignores ``last_output``)."""
        params = self._params
        assert isinstance(params, DynAudNormParams)
        af = (
            f"dynaudnorm=framelen={params.framelen}:gausssize={params.gausssize}"
            f":peak={params.peak}:maxgain={params.maxgain}:targetrms={params.targetrms}"
        )
        return FilterStep(af=af, needs_pass=False, out_layout=layout)


@register_filter
class DownmixFilter(FilterType):
    """Downmix-only channel fold (``downmix``).

    Emits an index-addressed ``pan`` fragment from :data:`DOWNMIX_MATRICES` when
    the source has more channels than the ``to:`` target; otherwise a no-op
    (``af=""``) that leaves the channels untouched.
    """

    type_id      = "downmix"
    params_model = DownmixParams

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Return the pan fragment, or a no-op when source channels <= target."""
        params = self._params
        assert isinstance(params, DownmixParams)
        target        = ChannelLayout.parse(params.to)
        target_channels = layout_channels(target.normalized)
        if layout.channels <= target_channels:
            # Downmix-only: nothing to reduce, pass channels through untouched.
            return FilterStep(af="", needs_pass=False, out_layout=layout)

        # Pick the matrix to downmix with.
        layout_id = (layout.normalized, target.normalized, params.matrix)
        if layout_id not in DOWNMIX_MATRICES:
            raise ValueError(
                f"No downmix matrix for {layout.normalized} → {target.normalized} "
                f"with matrix={params.matrix!r}"
            )

        pan = ",".join([
            DOWNMIX_FORMAT,
            DOWNMIX_MATRICES[layout_id]
        ])
        return FilterStep(af=pan, needs_pass=False, out_layout=target)


@register_filter
class EncodeFilter(FilterType):
    """Terminal output target (``encode``): no ``-af``, sets ``output_format``."""

    type_id      = "encode"
    params_model = EncodeFilterParams

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Contribute no ``-af`` fragment; set the terminal output format."""
        params = self._params
        assert isinstance(params, EncodeFilterParams)
        return FilterStep(
            af            = "",
            needs_pass    = False,
            out_layout    = layout,
            output_format = EncodeParams(
                codec               = params.codec,
                bitrate_per_channel = params.bitrate_per_channel,
                extension           = params.extension,
            ),
        )


@register_filter
class PassthroughFilter(FilterType):
    """Stream-copy with no filtering — a forward-compatible stub.

    Config-time this is a valid filter (a chain may consist solely of it), but
    executing it is **not yet implemented**. Its :meth:`resolve` fails loudly so
    a passthrough chain can never silently produce an incorrect file.

    Future behaviour (planned in the in-memory-stream spec): a passthrough chain
    produces no file at all — the source stream object is surfaced directly into
    the phase result for the muxer to consume, avoiding on-disk extraction.
    """

    type_id      = "passthrough"
    params_model = PassthroughParams

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Raise — passthrough execution is not implemented yet (fail loud)."""
        raise NotImplementedError(
            "passthrough is not implemented yet. It is reserved for the future "
            "in-memory-stream spec, where a passthrough chain will surface the "
            "source stream object directly into the phase result and produce no "
            "file. It must never silently emit an incorrect file until then."
        )
