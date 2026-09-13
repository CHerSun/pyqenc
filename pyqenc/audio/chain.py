"""Chain resolution and the generic combined-``-af`` execution loop.

This module ties the filter palette (config-side :class:`FilterInstance`) to the
runnable filter classes (:mod:`pyqenc.audio.filters`) and drives them through
their passes as a single combined ffmpeg ``-af`` invocation, split only where a
filter genuinely needs a measurement pass.

Two concerns live here:

- **Resolution** — :func:`resolve_chain` inlines every referenced filter's
  params into a :class:`ResolvedChain` (used for both execution and the sidecar)
  and computes the chain's effective terminal output format ``encode``: the last
  ``encode`` filter's format, or :data:`FLAC_DEFAULT` when the chain declares
  none. No synthetic FLAC filter is added to ``filters`` — the default lives only
  as the resolved ``encode`` value.

- **Execution** — :func:`execute_chain` runs one (track, chain) job. The loop is
  fully generic: it holds the accumulating invariant ``-af`` chain, the current
  layout, the effective output format, and the current filter's most recent
  measurement result, and drives each filter through :meth:`FilterType.resolve`.
  It contains **zero** filter-type-specific logic (Req 6.5). A filter finishes
  the instant it returns ``needs_pass=False``; at that moment its fragment is
  frozen and ``last_output`` is cleared so it can never leak to the next filter
  (Req 6.3).

The ffmpeg run callable is **injectable** (``runner`` parameter, defaulting to
:func:`~pyqenc.utils.ffmpeg_runner.run_ffmpeg_async`) so tests drive the loop
with a spy that records invocations and inspects the ``-af`` strings without
launching a real ffmpeg.

``EncodeParams`` is imported from :mod:`pyqenc.audio.filters` (its canonical
home) rather than redefined here, avoiding a circular import — ``chain.py``
imports from ``filters.py``, never the reverse.
"""
# CHerSun 2026

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from pyqenc.app_config import ChainSpec, FilterInstance
from pyqenc.audio.filters import (
    EncodeFilter,
    EncodeParams,
    FilterType,
    get_filter_class,
)
from pyqenc.audio.layout import ChannelLayout
from pyqenc.constants import (
    AF_CHAIN_SEPARATOR,
    CHAIN_FILENAME_SUFFIX,
    FFMPEG_ARG_AF,
    FFMPEG_ARG_BITRATE_A,
    FFMPEG_ARG_CODEC_A,
    FFMPEG_ARG_FORMAT,
    FFMPEG_ARG_INPUT,
    FFMPEG_ARG_MAP,
    FFMPEG_ARG_NO_DATA,
    FFMPEG_ARG_NO_SUBS,
    FFMPEG_ARG_NO_VIDEO,
    FFMPEG_EXECUTABLE,
    FFMPEG_MAP_FIRST_AUDIO,
    FFMPEG_NULL_MUXER,
    FFMPEG_NULL_SINK,
    FLAC_CODEC,
    FLAC_EXTENSION,
    OUTPUT_FORMAT_MUXERS,
)
from pyqenc.utils.ffmpeg_runner import (
    FFmpegRunResult,
    run_ffmpeg_async,
)
from pyqenc.utils.long_path import LongPath

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default terminal output format
# ---------------------------------------------------------------------------

FLAC_DEFAULT: EncodeParams = EncodeParams(
    codec               = FLAC_CODEC,
    bitrate_per_channel = "0k",
    extension           = FLAC_EXTENSION,
)
"""The terminal output format for a chain that declares no ``encode`` filter.

FLAC is lossless and bitrate-agnostic, so ``bitrate_per_channel`` is a documented
**unused placeholder** for the FLAC default — the executor emits ``-c:a flac``
with **no** ``-b:a`` (FLAC ignores bitrate). It is realised as the executor's
initial ``output_format`` rather than a synthetic filter injected into the chain
(Req 4.7)."""


# ---------------------------------------------------------------------------
# The injectable ffmpeg run callable
# ---------------------------------------------------------------------------

FFmpegRunner = Callable[..., Awaitable[FFmpegRunResult]]
"""Signature-compatible with :func:`run_ffmpeg_async`.

Injected into :func:`execute_chain` so tests supply a spy runner that records
each invocation's ``cmd``/``output_file`` and returns a canned
:class:`FFmpegRunResult` — no real ffmpeg. Only the two keyword args the executor
passes (``cmd`` positionally, ``output_file=...``) are exercised."""


# ---------------------------------------------------------------------------
# Resolved chain
# ---------------------------------------------------------------------------

class ResolvedChain(BaseModel):
    """A chain with every referenced filter's params inlined and encode resolved.

    Used both to execute the chain and as the sidecar record whose equality
    drives invalidation. ``encode`` is **always populated** (the last ``encode``
    filter, else :data:`FLAC_DEFAULT`) so the sidecar captures the concrete
    output format and a change to it invalidates correctly.

    Attributes:
        name:    The chain's configured, filesystem-safe name (the
                 ``chain=<name>`` output-filename suffix).
        filters: The referenced filters as concrete, param-inlined
                 :class:`FilterInstance` objects, in chain order. No synthetic
                 FLAC filter is present.
        encode:  The effective terminal output format (last ``encode`` filter's
                 format, or :data:`FLAC_DEFAULT`).
    """

    model_config = ConfigDict(frozen=True)

    name:    str
    filters: list[FilterInstance]
    encode:  EncodeParams


def chain_signature(chain: ResolvedChain) -> str:
    """Return the canonical compact signature string for a resolved chain.

    The signature is :meth:`ResolvedChain.model_dump_json` — a deterministic,
    compact JSON string (Pydantic emits fields in declaration order). It is used
    purely for **equality-based invalidation**: two chains are considered the
    same iff their signatures are equal. This is the single canonical signature
    function shared by the sidecar (:class:`~pyqenc.state.AudioSidecar`) and the
    phase, so persistence and comparison never diverge (DRY).

    Args:
        chain: The resolved chain to sign.

    Returns:
        The compact JSON signature string.
    """
    return chain.model_dump_json()


def resolve_chain(spec: ChainSpec, palette: dict[str, FilterInstance]) -> ResolvedChain:
    """Resolve a :class:`ChainSpec` against the palette into a :class:`ResolvedChain`.

    Inlines each referenced filter's params (by palette lookup) and computes the
    effective ``encode``: the **last** ``encode`` filter's format (last-encode-
    wins, Req 4.8), or :data:`FLAC_DEFAULT` when the chain declares none
    (Req 4.7). No synthetic FLAC filter is appended to ``filters``.

    Args:
        spec:    The config-side chain definition (name + ordered filter names).
        palette: The ``audio.filters`` palette (name → validated
                 :class:`FilterInstance`). All names in ``spec.filters`` are
                 guaranteed present by config validation.

    Returns:
        The fully-resolved chain.
    """
    filters = [palette[name] for name in spec.filters]

    encode = FLAC_DEFAULT
    for inst in filters:
        if inst.type == EncodeFilter.type_id:
            # Last-encode-wins: keep overwriting so the final encode filter stands.
            encode = _encode_params_from(inst)

    return ResolvedChain(name=spec.name, filters=filters, encode=encode)


def _encode_params_from(inst: FilterInstance) -> EncodeParams:
    """Build the terminal :class:`EncodeParams` from an ``encode`` filter instance.

    Args:
        inst: A :class:`FilterInstance` whose ``type`` is ``encode``; its
              ``params`` is an ``EncodeFilterParams`` (codec, bitrate_per_channel,
              extension).

    Returns:
        The equivalent :class:`EncodeParams` terminal target.
    """
    params = inst.params
    return EncodeParams(
        codec               = params.codec,        # type: ignore[attr-defined]
        bitrate_per_channel = params.bitrate_per_channel,  # type: ignore[attr-defined]
        extension           = params.extension,    # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Bitrate scaling
# ---------------------------------------------------------------------------

def _scale_bitrate(per_channel: str, channels: int) -> str:
    """Scale a per-channel bitrate string by the output channel count.

    Parses a bitrate like ``64k`` / ``0.5m``, multiplies by ``channels``, and
    re-renders in ``k`` (e.g. ``64k`` × 2 → ``128k``). A value that cannot be
    parsed is returned unchanged (a defensive fallback; config values are simple).

    Args:
        per_channel: The per-channel bitrate string (``encode`` filter's
                     ``bitrate_per_channel``), e.g. ``64k`` or ``0.5m``.
        channels:    The final layout's channel count.

    Returns:
        The total bitrate string (e.g. ``128k``).
    """
    raw = per_channel.strip().lower()
    try:
        if raw.endswith("k"):
            return f"{int(raw[:-1]) * channels}k"
        if raw.endswith("m"):
            return f"{int(float(raw[:-1]) * 1000) * channels}k"
    except ValueError:
        logger.warning("Cannot parse bitrate_per_channel %r; using as-is", per_channel)
    return per_channel


# ---------------------------------------------------------------------------
# Output naming
# ---------------------------------------------------------------------------

def chain_output_path(
    source:     LongPath,
    chain_name: str,
    extension:  str,
    output_dir: LongPath,
) -> LongPath:
    """Build the output path for a (track, chain) job.

    The name is ``<source-stem> chain=<chain-name>.<ext>`` (Req 8.1–8.3),
    preserving the source stem unchanged. The output **directory** is supplied by
    the caller (the phase owns its dedicated audio dir) rather than derived from
    the source's parent, so chain outputs never land next to the source tracks
    (Phase Contract: each phase owns its own folder).

    Args:
        source:     The extracted source track path (its stem names the output).
        chain_name: The chain's configured name.
        extension:  The output extension without the dot (from the effective
                    encode, or ``flac``).
        output_dir: The dedicated directory chain outputs are written to.

    Returns:
        The final output path (a :class:`LongPath`).
    """
    name = f"{source.stem}{CHAIN_FILENAME_SUFFIX}{chain_name}.{extension}"
    return output_dir / name


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def _build_measurement_cmd(source: LongPath, af: str) -> list[str | os.PathLike]:
    """Build a measurement-pass command: apply ``af`` and write to the null muxer.

    No file is produced (``-f null -``), so the caller passes ``output_file=None``
    to the runner. The filter reads its measured value from the result's
    ``stderr_lines``. ``-vn -sn -dn`` drop any non-audio streams so the pass
    operates on the audio stream alone (consistent with the application pass; the
    flags never touch the audio stream's rate or timestamps).

    Args:
        source: The source track path.
        af:     The comma-joined ``-af`` chain to measure with (never empty — a
                measurement pass always has at least the measuring filter's ``af``).

    Returns:
        The ffmpeg command (executable first; the runner injects progress flags).
    """
    return [
        FFMPEG_EXECUTABLE,
        FFMPEG_ARG_INPUT, str(source),
        FFMPEG_ARG_MAP,   FFMPEG_MAP_FIRST_AUDIO,
        FFMPEG_ARG_NO_VIDEO, FFMPEG_ARG_NO_SUBS, FFMPEG_ARG_NO_DATA,  # audio-only
        FFMPEG_ARG_AF,    af,
        FFMPEG_ARG_FORMAT, FFMPEG_NULL_MUXER,
        FFMPEG_NULL_SINK,
    ]


def _build_application_cmd(
    source:  LongPath,
    output:  LongPath,
    af:      str,
    encode:  EncodeParams,
    channels: int,
) -> list[str | os.PathLike]:
    """Build the final application-pass command producing the output file.

    ``-vn -sn -dn`` drop video, subtitle, and data streams so the produced file
    contains ONLY the audio stream (no stray ``bin_data``/data stream carried
    through by the muxer). These flags never resample or retime the audio — they
    only drop the non-audio streams, preserving source rate and timestamps.

    Emits ``-af`` only when ``af`` is non-empty (an all-empty chain omits it,
    Req 6.1). Emits ``-c:a <codec>``; for lossy codecs also ``-b:a <scaled>``
    (bitrate scaled by the final layout's channel count). FLAC emits **no**
    ``-b:a`` (it ignores bitrate). The exact ``output`` Path object is present in
    the command so the runner can enforce ``.tmp``-then-rename.

    Args:
        source:   The source track path.
        output:   The final output path (present in cmd for the runner).
        af:       The comma-joined ``-af`` chain (may be empty).
        encode:   The effective terminal output format.
        channels: The final layout's channel count (for bitrate scaling).

    Returns:
        The ffmpeg command.
    """
    cmd: list[str | os.PathLike] = [
        FFMPEG_EXECUTABLE,
        FFMPEG_ARG_INPUT, str(source),
        FFMPEG_ARG_MAP,   FFMPEG_MAP_FIRST_AUDIO,
        FFMPEG_ARG_NO_VIDEO, FFMPEG_ARG_NO_SUBS, FFMPEG_ARG_NO_DATA,  # audio-only output
    ]
    if af:
        cmd += [FFMPEG_ARG_AF, af]
    cmd += [FFMPEG_ARG_CODEC_A, encode.codec]
    if encode.codec != FLAC_CODEC:
        cmd += [FFMPEG_ARG_BITRATE_A, _scale_bitrate(encode.bitrate_per_channel, channels)]
    cmd.append(output)
    return cmd


# ---------------------------------------------------------------------------
# The generic executor loop
# ---------------------------------------------------------------------------

class ChainExecutionError(RuntimeError):
    """Raised when a (track, chain) job fails (a measurement or application pass)."""


async def execute_chain(
    resolved:     ResolvedChain,
    source:       LongPath,
    track_layout: ChannelLayout,
    output_dir:   LongPath,
    runner:       FFmpegRunner = run_ffmpeg_async,
) -> LongPath:
    """Execute one (track, chain) job as a combined ``-af`` chain, split at passes.

    The loop is fully generic (Req 6.5): it drives each filter through
    :meth:`FilterType.resolve`, comma-joining non-empty fragments, running a
    measurement pass whenever a filter reports ``needs_pass`` and feeding only
    that single latest result back to the same filter. When a filter finishes it
    freezes its fragment into the invariant chain, adopts its ``out_layout``, and
    clears ``last_output`` (Req 6.3). With K measuring filters the loop issues
    exactly K measurement passes + 1 final application pass (Req 6.2, 6.4).

    Args:
        resolved:     The resolved chain (inlined filters + effective ``encode``).
        source:       The extracted source track path.
        track_layout: The source track's channel layout.
        output_dir:   The dedicated directory the output file (and its ``.tmp``
                      sibling) is written to.
        runner:       The ffmpeg run callable (injectable for tests; defaults to
                      :func:`run_ffmpeg_async`).

    Returns:
        The produced output path.

    Raises:
        ChainExecutionError:  When a measurement or the application pass fails.
        NotImplementedError:  When the chain contains a ``passthrough`` filter
                              (surfaced from the filter's ``resolve``).
    """
    runnable: deque[FilterType] = deque(
        get_filter_class(inst.type)(inst.params) for inst in resolved.filters
    )

    af_parts:      list[str]              = []            # frozen fragments (invariant)
    output_format: EncodeParams           = resolved.encode  # authoritative effective target
    layout:        ChannelLayout          = track_layout
    last_output:   FFmpegRunResult | None = None         # only the CURRENT filter's latest pass

    while runnable:
        step = runnable[0].resolve(layout, last_output)
        # Belt-and-suspenders: honour a filter that overrides the format. The
        # resolved.encode is already the effective target (last-encode-wins), so
        # a real encode filter's output_format matches it; keeping this keeps the
        # loop consistent with the FilterStep contract without a type branch.
        if step.output_format is not None:
            output_format = step.output_format

        trial = af_parts + ([step.af] if step.af else [])   # drop empty fragments

        if step.needs_pass:
            last_output = await _run_measurement(runner, source, trial, resolved.name)
        else:
            last_output = None                              # filter finished — never leak onward
            af_parts    = trial                             # freeze fragment
            layout      = step.out_layout
            runnable.popleft()

    af     = AF_CHAIN_SEPARATOR.join(af_parts)
    output = chain_output_path(source, resolved.name, output_format.extension, output_dir)
    cmd    = _build_application_cmd(source, output, af, output_format, layout.channels)

    # The `.tmp`-then-rename protocol hides the real extension from ffmpeg, so it
    # cannot infer the muxer. Supply the explicit `-f <muxer>` for the target
    # container (flac -> flac, m4a -> ipod); an unmapped extension falls back to
    # the runner's Matroska default.
    muxer = OUTPUT_FORMAT_MUXERS.get(output_format.extension)
    logger.debug("[%s] apply → %s (-af %r, -f %s)", resolved.name, output.name, af, muxer)
    result = await runner(cmd, output_file=output, output_format=muxer)
    if not result.success:
        raise ChainExecutionError(
            f"chain {resolved.name!r} application pass failed for {source.name!r} "
            f"(ffmpeg exit code {result.returncode})"
        )
    return output


async def _run_measurement(
    runner:     FFmpegRunner,
    source:     LongPath,
    trial:      list[str],
    chain_name: str,
) -> FFmpegRunResult:
    """Run one measurement pass with the joined ``trial`` filters (writes no file).

    Args:
        runner:     The injectable ffmpeg run callable.
        source:     The source track path.
        trial:      The invariant fragments plus the measuring filter's ``af``.
        chain_name: The chain name (for context in the error message).

    Returns:
        The measurement result; the owning filter scrapes its value from
        ``stderr_lines`` on the next ``resolve`` call.

    Raises:
        ChainExecutionError: When the measurement pass exits non-zero.
    """
    af  = AF_CHAIN_SEPARATOR.join(trial)
    cmd = _build_measurement_cmd(source, af)
    logger.debug("[%s] measure (-af %r)", chain_name, af)
    result = await runner(cmd, output_file=None)
    if not result.success:
        raise ChainExecutionError(
            f"chain {chain_name!r} measurement pass failed for {source.name!r} "
            f"(ffmpeg exit code {result.returncode})"
        )
    return result
