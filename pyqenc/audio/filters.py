"""Self-contained audio filter classes: config-loaded params plus behaviour.

Each filter is ONE Pydantic class owning its ``type`` tag, its parameters
(fields, ``extra="forbid"`` so invalid params raise at config load), and its
realisation behaviour via the single stateless contract
:meth:`_FilterInstanceBase.resolve` — including its pass count and, for
two-pass filters, how it scrapes the measured value out of a measurement
pass. A new filter type is added entirely in this module: one class plus one
:data:`FilterInstance` union member; no edits to ``AudioConfig``, the chain
executor, or existing filters are required.

The single contract the executor drives is::

    resolve(layout, last_output) -> FilterStep

- **Single-pass filters** (``dynaudnorm``, ``downmix``, ``encode``,
  ``passthrough``) ignore ``last_output`` and return ``needs_pass=False``.
- **Two-pass filters** (``peaknorm``, ``loudnorm``) return, on the first call
  (``last_output is None``), ``needs_pass=True`` with the analysis ``-af``; the
  executor runs that as a measurement pass and calls ``resolve`` again with the
  result, which the filter scrapes (its own regex/JSON parse lives here) to
  build the final ``-af`` fragment.

All filter-specific knowledge (loudnorm JSON regex, ``astats`` peak regex,
downmix matrices) lives inside the owning filter class; the executor knows
only the ``resolve``/:class:`FilterStep` contract.

``EncodeParams`` is defined here (rather than in ``chain.py``) because both
``FilterStep.output_format`` and the ``encode`` filter need it, and ``chain.py``
imports from this module (defining it there would create a circular import).
This module is its canonical home.
"""
# CHerSun 2026

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    """One filter's contribution to the chain, returned by the filter's ``resolve``.

    The executor consumes this without inspecting the filter's concrete type.

    Attributes:
        af:            The ``-af`` contribution. ``""`` for a no-op / terminal
                       filter (never ``None``); the executor drops empty
                       fragments from the comma-join.
        needs_pass:    ``True`` = run a measurement pass with ``af`` (output to
                       null), then call the filter's ``resolve`` again with the
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


class _FilterInstanceBase(BaseModel, ABC):
    """Base of every audio filter class: shared validation config + ``resolve``.

    A filter object is **stateless and re-entrant**: it holds only its
    validated fields (the config palette entry) and is reused across every
    (track, chain). Everything a later ``resolve`` call needs beyond the
    fields arrives via ``last_output`` — never stored on the instance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

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
# Two-pass scraping regexes (owned by the two-pass filters)
# ---------------------------------------------------------------------------

_LOUDNORM_JSON_RE: Final = re.compile(
    r"\[Parsed_loudnorm[^\]]*\]\s*(\{.*?\})",
    re.DOTALL,
)
"""Extracts the ``loudnorm`` JSON measurement block from ffmpeg stderr."""

_ASTATS_PEAK_VOLUME_RE: Final = re.compile(r"Peak level dB:\s*([-\d.]+)")
"""Extracts the ``peak_volume`` reading from ``astats`` stderr output."""
# Sample line:
# [Parsed_astats_2 @ ...] Peak level dB: 2.269108

# ---------------------------------------------------------------------------
# Audio filters
# ---------------------------------------------------------------------------

class PeakNormInstance(_FilterInstanceBase):
    """Two-pass peak normalisation: measure peak (``astats``), apply gain.

    Preserves source sample rate and bit depth — it only applies a ``volume``
    gain to shift the measured peak to ``target_dbfs``.
    """

    type:        Literal["peaknorm"] = "peaknorm"
    target_dbfs: float

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
        max_volume_db = float(match.group(1))
        gain_db       = self.target_dbfs - max_volume_db
        return FilterStep(
            af         = f"volume={gain_db:.4f}dB",
            needs_pass = False,
            out_layout = layout,
        )


class LoudNormInstance(_FilterInstanceBase):
    """Two-pass EBU R128 loudness normalisation (``loudnorm``).

    Pass 1 measures integrated loudness / true peak / range via
    ``loudnorm=…:print_format=json``; pass 2 linear-normalises using the
    measured values scraped from the pass-1 JSON block.

    Field names are lower-case; the filter emits loudnorm's canonical
    upper-case option names (``I=``/``TP=``/``LRA=`` and ``measured_*``) — per
    ``ffmpeg -h filter=loudnorm`` the lower-case ``i``/``tp``/``lra`` spellings
    are the aliases, so no long-name switch is needed (unlike ``dynaudnorm``,
    where ``f``/``g``/``p``/``m``/``r`` were the short forms actually used).
    """

    type: Literal["loudnorm"] = "loudnorm"
    i:   float
    tp:  float
    lra: float

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Analyse on the first call, linear-normalise on the second."""
        targets = f"I={self.i}:TP={self.tp}:LRA={self.lra}"
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


class DynAudNormInstance(_FilterInstanceBase):
    """Single-pass dynamic audio normalisation (``dynaudnorm``).

    Emits ``dynaudnorm=framelen=<>:gausssize=<>:peak=<>:maxgain=<>:targetrms=<>``
    using ffmpeg's **long** option names. To inspect the filter's full option
    set (to revise these params later) run ``ffmpeg -h filter=dynaudnorm``
    (general form ``ffmpeg -h filter=<name>``).
    """

    type: Literal["dynaudnorm"] = "dynaudnorm"
    framelen:  int      # analysis window length in ms (ffmpeg ``framelen``/``f``)
    gausssize: int      # Gaussian window size in frames (ffmpeg ``gausssize``/``g``)
    peak:      float    # target peak magnitude, 0..1 (ffmpeg ``peak``/``p``)
    maxgain:   float    # maximum gain factor (ffmpeg ``maxgain``/``m``)
    targetrms: float    # target RMS, 0 = disabled (ffmpeg ``targetrms``/``r``)

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Return the single-pass ``dynaudnorm`` fragment (ignores ``last_output``)."""
        af = (
            f"dynaudnorm=framelen={self.framelen}:gausssize={self.gausssize}"
            f":peak={self.peak}:maxgain={self.maxgain}:targetrms={self.targetrms}"
        )
        return FilterStep(af=af, needs_pass=False, out_layout=layout)


class DownmixInstance(_FilterInstanceBase):
    """Downmix-only channel fold (``downmix``).

    Emits an index-addressed ``pan`` fragment from :data:`DOWNMIX_MATRICES` when
    the source has more channels than the ``to:`` target; otherwise a no-op
    (``af=""``) that leaves the channels untouched.
    """

    type:   Literal["downmix"] = "downmix"
    to:     str
    matrix: str | None = None

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Return the pan fragment, or a no-op when source channels <= target."""
        target          = ChannelLayout.parse(self.to)
        target_channels = layout_channels(target.normalized)
        if layout.channels <= target_channels:
            # Downmix-only: nothing to reduce, pass channels through untouched.
            return FilterStep(af="", needs_pass=False, out_layout=layout)

        # Pick the matrix to downmix with.
        layout_id = (layout.normalized, target.normalized, self.matrix)
        if layout_id not in DOWNMIX_MATRICES:
            raise ValueError(
                f"No downmix matrix for {layout.normalized} → {target.normalized} "
                f"with matrix={self.matrix!r}"
            )

        pan = ",".join([
            DOWNMIX_FORMAT,
            DOWNMIX_MATRICES[layout_id]
        ])
        return FilterStep(af=pan, needs_pass=False, out_layout=target)


class EncodeInstance(_FilterInstanceBase):
    """Terminal output target (``encode``): no ``-af``, sets ``output_format``."""

    type:                Literal["encode"] = "encode"
    codec:               str
    bitrate_per_channel: str
    extension:           str

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Contribute no ``-af`` fragment; set the terminal output format."""
        return FilterStep(
            af            = "",
            needs_pass    = False,
            out_layout    = layout,
            output_format = EncodeParams(
                codec               = self.codec,
                bitrate_per_channel = self.bitrate_per_channel,
                extension           = self.extension,
            ),
        )


class PassthroughInstance(_FilterInstanceBase):
    """Stream-copy with no filtering — a forward-compatible stub.

    Config-time this is a valid filter (a chain may consist solely of it), but
    executing it is **not yet implemented**. Its :meth:`resolve` fails loudly so
    a passthrough chain can never silently produce an incorrect file.

    Planned (in-memory-stream spec): a passthrough chain surfaces the source
    stream object directly into the phase result — no file produced.
    """

    type: Literal["passthrough"] = "passthrough"

    def resolve(
        self,
        layout:      ChannelLayout,
        last_output: FFmpegRunResult | None,
    ) -> FilterStep:
        """Raise — passthrough execution is not implemented yet (fail loud)."""
        raise NotImplementedError(
            "passthrough is not implemented yet. "
            "It must never silently emit an incorrect file."
        )


FilterInstance = Annotated[
    PeakNormInstance
    | LoudNormInstance
    | DynAudNormInstance
    | DownmixInstance
    | EncodeInstance
    | PassthroughInstance,
    Field(discriminator="type"),
]
"""A validated ``audio.filters`` palette entry — the audio filter union.

The flat YAML form ``{type: <id>, ...params}`` is the only shape: each member's
fields ARE its params, so config, ``model_dump()``, and chain fingerprints all
share one form. The ``type`` tag discriminates; an unknown type id or a param
foreign to the type is a config-load ``ValidationError`` listing the expected
tags. Each member IS its filter — validated config data and ``resolve()``
behaviour on one class; the executor drives ``inst.resolve()`` directly.
"""
