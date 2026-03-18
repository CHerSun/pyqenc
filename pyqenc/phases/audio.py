"""
Audio processing phase for the quality-based encoding pipeline.

This module handles audio stream processing using audio strategies
to generate normalised stereo variants for day and night modes.

Strategy classes:
  BaseStrategy, ConversionStrategy,
  DownmixStrategy71to51, DownmixStrategy51to20Std,
  DownmixStrategy51to20Night, DownmixStrategy51to20NBoost,
  NormStrategy, DynaudnormStrategy,
  Task, AudioEngine, SynchronousRunner, AsyncRunner
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from alive_progress import alive_bar, config_handler

from pyqenc.constants import (
    AUDIO_CH_51,
    AUDIO_CH_71,
    AUDIO_STEM_SEPARATOR,
    _NORMALISED_PREFIXES,
)
from pyqenc.utils.ffmpeg_runner import run_ffmpeg, run_ffmpeg_async

config_handler.set_global(enrich_print=False) # type: ignore
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INTERMEDIATE_CODEC:     str = "flac"
_INTERMEDIATE_EXTENSION: str = "flac"


# ---------------------------------------------------------------------------
# EBU R128 loudnorm constants
# ---------------------------------------------------------------------------

_LOUDNORM_TARGET_I:   str = "-23.0"
"""Target integrated loudness (LUFS) for EBU R128 normalisation."""
_LOUDNORM_TARGET_TP:  str = "-0.5"
"""Target true peak (dBFS) for EBU R128 normalisation."""
_LOUDNORM_TARGET_LRA: str = "7.0"
"""Target loudness range (LU) for EBU R128 normalisation."""

_LOUDNORM_JSON_RE = re.compile(
    r"\[Parsed_loudnorm[^\]]*\]\s*(\{.*?\})",
    re.DOTALL,
)
"""Regex to extract the loudnorm JSON measurement block from ffmpeg stderr."""


async def _two_pass_loudnorm(
    source:        Path,
    output:        Path,
    extra_filters: list[str] | None = None,
) -> None:
    """Run a 2-pass EBU R128 loudnorm normalisation, optionally with prepended filters.

    Pass 1 measures integrated loudness and true peak by running ffmpeg with
    ``loudnorm=print_format=json`` and a null output (no file written).
    Pass 2 applies linear normalisation using the measured values (and any
    ``extra_filters`` prepended) and writes the output FLAC file via the
    ``.tmp``-then-rename protocol.

    Args:
        source:        Input audio file.
        output:        Intended output FLAC path.
        extra_filters: Optional list of ffmpeg audio filter strings to prepend
                       before the ``loudnorm`` filter in both passes (e.g. a
                       downmix filter).  ``None`` means no extra filters.

    Raises:
        RuntimeError: If pass 1 does not produce a parseable loudnorm JSON block.
        RuntimeError: If pass 2 ffmpeg command fails.
    """
    filters_prefix = list(extra_filters) if extra_filters else []

    # ------------------------------------------------------------------
    # Pass 1 — analysis only (no output file)
    # ------------------------------------------------------------------
    analysis_filter = ",".join(
        filters_prefix
        + [f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}:print_format=json"]
    )
    pass1_cmd: list[str | os.PathLike] = [
        "ffmpeg",
        "-i",    source,
        "-af",   analysis_filter,
        "-f",    "null",
        "-",
    ]
    logger.debug("loudnorm pass 1: %s", source.name)
    pass1_result = await run_ffmpeg_async(pass1_cmd, output_file=None)

    # Parse the JSON block from stderr
    stderr_text = "\n".join(pass1_result.stderr_lines)
    match = _LOUDNORM_JSON_RE.search(stderr_text)
    if not match:
        raise RuntimeError(
            f"loudnorm pass 1 did not produce a parseable JSON block for {source.name!r}. "
            f"ffmpeg exit code: {pass1_result.returncode}"
        )

    try:
        measured = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse loudnorm JSON for {source.name!r}: {exc}"
        ) from exc

    input_i   = measured.get("input_i",   "-23.0")
    input_tp  = measured.get("input_tp",  "-0.5")
    input_lra = measured.get("input_lra", "7.0")
    input_thresh = measured.get("input_thresh", "-33.0")
    target_offset = measured.get("target_offset", "0.0")

    logger.debug(
        "loudnorm pass 1 measured: I=%s TP=%s LRA=%s thresh=%s offset=%s",
        input_i, input_tp, input_lra, input_thresh, target_offset,
    )

    # ------------------------------------------------------------------
    # Pass 2 — linear normalisation → output FLAC
    # ------------------------------------------------------------------
    normalise_filter = (
        f"loudnorm=I={_LOUDNORM_TARGET_I}:TP={_LOUDNORM_TARGET_TP}:LRA={_LOUDNORM_TARGET_LRA}"
        f":linear=true"
        f":measured_I={input_i}:measured_TP={input_tp}:measured_LRA={input_lra}"
        f":measured_thresh={input_thresh}:offset={target_offset}:print_format=none"
    )
    pass2_filter = ",".join(filters_prefix + [normalise_filter])
    pass2_cmd: list[str | os.PathLike] = [
        "ffmpeg",
        "-i",    source,
        "-af",   pass2_filter,
        "-c:a",  _INTERMEDIATE_CODEC,
        output,
    ]
    logger.debug("loudnorm pass 2: %s → %s", source.name, output.name)
    pass2_result = await run_ffmpeg_async(pass2_cmd, output_file=output)

    if not pass2_result.success:
        raise RuntimeError(
            f"loudnorm pass 2 failed for {source.name!r} "
            f"(exit code {pass2_result.returncode})"
        )

    logger.info("loudnorm complete: %s", output.name)


# ---------------------------------------------------------------------------
# BaseStrategy
# ---------------------------------------------------------------------------

class BaseStrategy(ABC):
    """Base class for all audio processing strategies."""

    def __init__(self, name: str, strategy_short: str) -> None:
        self.name           = name
        self.strategy_short = strategy_short

    def output_path(self, source: Path, extension: str = "flac") -> Path:
        """Construct the output path using the ``{strategy_short} ← {stem}.{extension}`` convention."""
        return source.parent / f"{self.strategy_short} {AUDIO_STEM_SEPARATOR} {source.stem}.{extension}"

    @abstractmethod
    def check(self, source: Path) -> bool:
        """Return True if this strategy should be applied to *source*."""
        raise NotImplementedError

    @abstractmethod
    def plan(self, source: Path) -> Path:
        """Return the planned output Path for *source* (used during plan building)."""
        raise NotImplementedError

    @abstractmethod
    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        """Execute the strategy on *source*, writing to *output*.

        If *dry_run* is True, no actual processing is performed.
        """
        raise NotImplementedError

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        """Async execution contract — default wraps synchronous *execute* in a thread."""
        await asyncio.to_thread(self.execute, source, output, dry_run)


# ---------------------------------------------------------------------------
# AudioConversionProfile
# ---------------------------------------------------------------------------

@dataclass
class AudioConversionProfile:
    """Codec/bitrate/extension profile for the final delivery conversion step.

    Attributes:
        codec:     ffmpeg audio codec name (e.g. ``"aac"``).
        bitrate:   Target bitrate string (e.g. ``"192k"``).
        extension: Output file extension including the leading dot (e.g. ``".aac"``).
    """

    codec:     str
    bitrate:   str
    extension: str


# ---------------------------------------------------------------------------
# Channel-count lookup (used by ConversionStrategy for bitrate scaling)
# ---------------------------------------------------------------------------

_CHANNEL_COUNTS: dict[str, int] = {
    "ch=2.0":    2,
    "ch=stereo": 2,
    "ch=5.1":    6,
    "ch=7.1":    8,
}
"""Maps channel layout tags to their channel count for proportional bitrate scaling."""


def _filename_prefix(source: Path) -> str:
    """Return the part of the filename *before* the first ``←`` separator.

    For a raw extracted file (no separator) this is the whole filename, which
    contains the channel layout tag (e.g. ``ch=5.1``).  For any processed
    output the prefix is just the ``strategy_short`` (e.g. ``2.0 std``), which
    never contains a channel layout tag.

    Using the prefix for ``check()`` prevents channel layout tags buried in
    chained stems from triggering downmix strategies on already-processed files.
    """
    sep = f" {AUDIO_STEM_SEPARATOR} "
    name = source.name
    idx = name.find(sep)
    return name[:idx] if idx != -1 else name


def _is_raw_source(source: Path) -> bool:
    """Return True when *source* is a raw extracted file (no ``←`` separator in name)."""
    return f" {AUDIO_STEM_SEPARATOR} " not in source.name


# ---------------------------------------------------------------------------
# DownmixStrategy71to51
# ---------------------------------------------------------------------------

class DownmixStrategy71to51(BaseStrategy):
    """Single-pass 7.1 → 5.1 downmix (no normalisation needed; no clipping risk)."""

    # 7.1 → 5.1: drop the wide-left/wide-right pair, keep the rest
    _FILTER: str = "pan=5.1|FL=FL|FR=FR|FC=FC|LFE=LFE|BL=BL|BR=BR"

    def __init__(self) -> None:
        super().__init__(name="7.1→5.1 Downmix", strategy_short="5.1")

    def check(self, source: Path) -> bool:
        """Return True only when source is a raw extracted file with the 7.1 channel layout tag."""
        return _is_raw_source(source) and AUDIO_CH_71 in source.name

    def plan(self, source: Path) -> Path:
        return self.output_path(source)

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-i",   source,
            "-af",  self._FILTER,
            "-c:a", _INTERMEDIATE_CODEC,
            output,
        ]
        result = run_ffmpeg(cmd, output_file=output)
        if not result.success:
            raise RuntimeError(f"7.1→5.1 downmix failed for {source.name!r}")

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-i",   source,
            "-af",  self._FILTER,
            "-c:a", _INTERMEDIATE_CODEC,
            output,
        ]
        result = await run_ffmpeg_async(cmd, output_file=output)
        if not result.success:
            raise RuntimeError(f"7.1→5.1 downmix failed for {source.name!r}")


# ---------------------------------------------------------------------------
# DownmixStrategy51to20Std
# ---------------------------------------------------------------------------

class DownmixStrategy51to20Std(BaseStrategy):
    """5.1 → 2.0 standard downmix + EBU R128 normalisation (2-pass).

    Uses ffmpeg's default downmix matrix (``-ac 2``), which ignores the LFE channel.
    """

    _FILTER: str = "aresample=matrix_encoding=dplii,pan=stereo|FL=FL+0.707*FC+0.707*BL|FR=FR+0.707*FC+0.707*BR"

    def __init__(self) -> None:
        super().__init__(name="5.1→2.0 Std Downmix+Norm", strategy_short="2.0 std")

    def check(self, source: Path) -> bool:
        """Return True for raw 5.1 sources or the direct 7.1→5.1 downmix output."""
        return (_is_raw_source(source) and AUDIO_CH_51 in source.name) or _filename_prefix(source) == "5.1"

    def plan(self, source: Path) -> Path:
        return self.output_path(source)

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        asyncio.run(_two_pass_loudnorm(source, output, extra_filters=[self._FILTER]))

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        await _two_pass_loudnorm(source, output, extra_filters=[self._FILTER])


# ---------------------------------------------------------------------------
# DownmixStrategy51to20Night
# ---------------------------------------------------------------------------

class DownmixStrategy51to20Night(BaseStrategy):
    """5.1 → 2.0 night-mode downmix + EBU R128 normalisation (2-pass).

    Incorporates the LFE channel mildly (0.5× gain) for better bass reproduction
    at low listening volumes.
    """

    _FILTER: str = (
        "pan=stereo"
        "|FL=FL+0.707*FC+0.5*LFE+0.707*BL"
        "|FR=FR+0.707*FC+0.5*LFE+0.707*BR"
    )

    def __init__(self) -> None:
        super().__init__(name="5.1→2.0 Night Downmix+Norm", strategy_short="2.0 night")

    def check(self, source: Path) -> bool:
        """Return True for raw 5.1 sources or the direct 7.1→5.1 downmix output."""
        return (_is_raw_source(source) and AUDIO_CH_51 in source.name) or _filename_prefix(source) == "5.1"

    def plan(self, source: Path) -> Path:
        return self.output_path(source)

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        asyncio.run(_two_pass_loudnorm(source, output, extra_filters=[self._FILTER]))

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        await _two_pass_loudnorm(source, output, extra_filters=[self._FILTER])


# ---------------------------------------------------------------------------
# DownmixStrategy51to20NBoost
# ---------------------------------------------------------------------------

class DownmixStrategy51to20NBoost(BaseStrategy):
    """5.1 → 2.0 night-mode boosted downmix + EBU R128 normalisation (2-pass).

    Incorporates the LFE channel with a stronger boost (0.9× gain) for
    pronounced bass at low listening volumes.
    """

    _FILTER: str = (
        "pan=stereo"
        "|FL=FL+0.707*FC+0.9*LFE+0.707*BL"
        "|FR=FR+0.707*FC+0.9*LFE+0.707*BR"
    )

    def __init__(self) -> None:
        super().__init__(name="5.1→2.0 NBoost Downmix+Norm", strategy_short="2.0 nboost")

    def check(self, source: Path) -> bool:
        """Return True for raw 5.1 sources or the direct 7.1→5.1 downmix output."""
        return (_is_raw_source(source) and AUDIO_CH_51 in source.name) or _filename_prefix(source) == "5.1"

    def plan(self, source: Path) -> Path:
        return self.output_path(source)

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        asyncio.run(_two_pass_loudnorm(source, output, extra_filters=[self._FILTER]))

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        await _two_pass_loudnorm(source, output, extra_filters=[self._FILTER])


# ---------------------------------------------------------------------------
# NormStrategy
# ---------------------------------------------------------------------------

class NormStrategy(BaseStrategy):
    """Standalone EBU R128 static normalisation (2-pass, no downmix).

    Applied to any source that has not yet been normalised — i.e. whose filename
    does not start with any of the ``_NORMALISED_PREFIXES``.
    """

    def __init__(self) -> None:
        super().__init__(name="EBU R128 Norm", strategy_short="norm")

    def check(self, source: Path) -> bool:
        """Return True for raw extracted sources or the 7.1→5.1 downmix output that have not been normalised.

        The ``5.1 ←`` downmix output is not a raw source but still needs
        normalisation treatment — it is a 5.1 FLAC that has never been through
        EBU R128.  All other processed outputs (anything else with a ``←``) are
        excluded.
        """
        is_eligible = _is_raw_source(source) or _filename_prefix(source) == "5.1"
        return is_eligible and not source.name.startswith(_NORMALISED_PREFIXES)

    def plan(self, source: Path) -> Path:
        return self.output_path(source)

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        asyncio.run(_two_pass_loudnorm(source, output))

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        await _two_pass_loudnorm(source, output)


# ---------------------------------------------------------------------------
# DynaudnormStrategy
# ---------------------------------------------------------------------------

class DynaudnormStrategy(BaseStrategy):
    """Dynamic normalisation applied on top of any statically normalised output.

    Applied only to files whose filename starts with one of the
    ``_NORMALISED_PREFIXES`` (i.e. ``norm ←``, ``2.0 std ←``, etc.).
    """

    _FILTER: str = "dynaudnorm=f=500:g=31:p=0.95:m=10.0:r=0.5:b=1"

    def __init__(self) -> None:
        super().__init__(name="Dynamic Norm", strategy_short="dynaudnorm")

    def check(self, source: Path) -> bool:
        """Return True when the file is a direct normalised output, or dynaudnorm applied to a 5.1 downmix norm output.

        Accepts:
        - Any non-raw file starting with a normalised prefix (``norm ←``,
          ``2.0 std ←``, etc.) — the standard case.
        - The ``norm ← 5.1 ←`` output, which starts with ``norm ←`` and is
          not raw, so it is already covered by the above.

        Rejects:
        - Raw sources (``_is_raw_source``).
        - ``dynaudnorm ←`` outputs (their name starts with ``dynaudnorm``, not
          a normalised prefix, so ``startswith`` already excludes them).
        """
        return source.name.startswith(_NORMALISED_PREFIXES) and not _is_raw_source(source)

    def plan(self, source: Path) -> Path:
        return self.output_path(source)

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-i",   source,
            "-af",  self._FILTER,
            "-c:a", _INTERMEDIATE_CODEC,
            output,
        ]
        result = run_ffmpeg(cmd, output_file=output)
        if not result.success:
            raise RuntimeError(f"dynaudnorm failed for {source.name!r}")

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-i",   source,
            "-af",  self._FILTER,
            "-c:a", _INTERMEDIATE_CODEC,
            output,
        ]
        result = await run_ffmpeg_async(cmd, output_file=output)
        if not result.success:
            raise RuntimeError(f"dynaudnorm failed for {source.name!r}")


# ---------------------------------------------------------------------------
# ConversionStrategy
# ---------------------------------------------------------------------------

class ConversionStrategy(BaseStrategy):
    """Profile-aware final delivery conversion to AAC (or other codec).

    Selects the conversion profile by matching the channel layout tag present
    in the source filename.  Falls back to the ``"2.0"`` profile with a warning
    when no layout tag matches.

    CBR mode is enforced unconditionally via ``-b:a <bitrate>`` (no ``-vbr``).
    """

    _DEFAULT_PROFILE_KEY: str = "2.0"

    def __init__(
        self,
        profiles:              dict[str, AudioConversionProfile],
        base_bitrate_override: str | None = None,
    ) -> None:
        """
        Args:
            profiles:              Map of channel-layout key → :class:`AudioConversionProfile`.
                                   Must contain at least a ``"2.0"`` fallback entry.
            base_bitrate_override: When set, treat this as the base bitrate for 2.0 stereo
                                   and scale proportionally for other channel counts.
        """
        super().__init__(name="AAC Conversion", strategy_short="aac")
        self.profiles              = profiles
        self.base_bitrate_override = base_bitrate_override

    def check(self, source: Path) -> bool:
        """Always returns False — applied via keep/convert filter only."""
        return False

    def plan(self, source: Path) -> Path:
        profile = self._select_profile(source)
        return self.output_path(source, extension=profile.extension.lstrip("."))

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        profile = self._select_profile(source)
        bitrate = self._resolve_bitrate(source, profile)
        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-i",   source,
            "-c:a", profile.codec,
            "-b:a", bitrate,
            output,
        ]
        result = run_ffmpeg(cmd, output_file=output)
        if not result.success:
            raise RuntimeError(f"AAC conversion failed for {source.name!r}")

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        profile = self._select_profile(source)
        bitrate = self._resolve_bitrate(source, profile)
        cmd: list[str | os.PathLike] = [
            "ffmpeg",
            "-i",   source,
            "-c:a", profile.codec,
            "-b:a", bitrate,
            output,
        ]
        result = await run_ffmpeg_async(cmd, output_file=output)
        if not result.success:
            raise RuntimeError(f"AAC conversion failed for {source.name!r}")

    def _select_profile(self, source: Path) -> AudioConversionProfile:
        """Select the conversion profile by scanning the source filename for a channel layout tag."""
        for layout_key in self.profiles:
            if f"ch={layout_key}" in source.name:
                return self.profiles[layout_key]
        logger.warning(
            "No channel layout tag matched in %r; falling back to '%s' profile",
            source.name, self._DEFAULT_PROFILE_KEY,
        )
        return self.profiles[self._DEFAULT_PROFILE_KEY]

    def _resolve_bitrate(self, source: Path, profile: AudioConversionProfile) -> str:
        """Resolve the effective bitrate, scaling from base override when supplied."""
        if self.base_bitrate_override is None:
            return profile.bitrate

        # Parse base bitrate (e.g. "192k" → 192)
        base_str = self.base_bitrate_override.lower().rstrip("k")
        try:
            base_kbps = int(base_str)
        except ValueError:
            logger.warning(
                "Cannot parse base_bitrate_override %r; using profile bitrate %r",
                self.base_bitrate_override, profile.bitrate,
            )
            return profile.bitrate

        # Determine channel count from source filename
        source_channels = 2  # default to stereo
        for tag, count in _CHANNEL_COUNTS.items():
            if tag in source.name:
                source_channels = count
                break

        scaled_kbps = int(base_kbps * source_channels / 2)
        return f"{scaled_kbps}k"


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """A single audio processing task in the pipeline graph."""

    source:      Path
    """Source audio file."""
    output:      Path
    """Output path — must be unique across all tasks (used for hashing)."""
    strategy:    BaseStrategy
    """Strategy to apply."""
    depth:       int
    """Depth in the task tree (0 = root)."""
    failed:      bool      = field(default=False)
    """Set to True when execution fails."""
    parent:      Task|None = field(default=None)
    """Parent task, if any."""

    def __hash__(self) -> int:
        return hash(self.output)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Task) and self.output == other.output

    def __repr__(self) -> str:
        flags = "❌" if self.failed else ""
        suffix = f" {flags}" if flags else ""
        return f"[{self.strategy.name}]{suffix} {self.output}"


# ---------------------------------------------------------------------------
# AudioEngine — plan builder
# ---------------------------------------------------------------------------

@dataclass
class PlanResult:
    """Processing plan produced by :meth:`AudioEngine.build_plan`."""

    tasks:         list[Task]
    found_files:   int
    skipped_files: int


@dataclass
class PlanExecutionResult:
    """Execution counters returned by a runner."""

    success: int
    failed:  int
    skipped: int


class AudioEngine:
    """Orchestrates the application of strategies to audio files.

    Attributes:
        strategies:         Ordered list of transformation strategies.
        finalizer_strategy: Optional strategy applied to terminal nodes.
    """

    def __init__(
        self,
        strategies: list[BaseStrategy],
        finalizer:  BaseStrategy | None = None,
    ) -> None:
        self.strategies        = strategies
        self.finalizer_strategy = finalizer

        # Validate uniqueness of strategy_short across all registered strategies
        all_strategies = list(strategies) + ([finalizer] if finalizer else [])
        seen_shorts: dict[str, str] = {}
        for s in all_strategies:
            if s.strategy_short in seen_shorts:
                raise ValueError(
                    f"Duplicate strategy_short '{s.strategy_short}' on '{s.name}' "
                    f"(already used by '{seen_shorts[s.strategy_short]}')"
                )
            seen_shorts[s.strategy_short] = s.name

    def build_plan(
        self,
        directory:      Path,
        convert_filter: str,
    ) -> PlanResult:
        """Scan *directory* and build a processing plan.

        Each source file is expanded through the registered strategies via BFS.
        After each new output path is enqueued, its filename is tested against
        *convert_filter*; any match causes a :class:`ConversionStrategy` task to
        be appended immediately (with that output as its source).

        Graph termination is natural: ``dynaudnorm ←`` prefixed outputs never
        satisfy any strategy's ``check()``, so the BFS stops without needing an
        explicit depth limit or ``is_terminal`` flag.

        Args:
            directory:      Directory containing source audio files.
            convert_filter: Compiled-regex string; outputs whose filename matches
                            are passed to the finalizer strategy for conversion.

        Returns:
            :class:`PlanResult` with the task list and discovery counters.
        """
        convert_re = re.compile(convert_filter)

        initial = [
            Path(f) for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in (".flac", ".mka")
        ]
        found_files = len(initial)

        queue: deque[tuple[Path, int, Task | None]] = deque(
            (f, 0, None) for f in initial
        )
        seen:  set[str]   = set()
        tasks: list[Task] = []

        skipped_files = 0

        while queue:
            target, depth, parent = queue.popleft()

            matched = [s for s in self.strategies if s.check(target)]
            if not matched and depth == 0:
                skipped_files += 1
                continue

            for strategy in matched:
                new_out = strategy.plan(target)
                if new_out.name in seen:
                    continue
                seen.add(new_out.name)
                current = Task(target, new_out, strategy, depth + 1, parent=parent)
                tasks.append(current)
                queue.append((new_out, depth + 1, current))

                # Finalizer dispatch: if the new output matches the convert filter,
                # append a ConversionStrategy task for it immediately.
                if self.finalizer_strategy and convert_re.search(new_out.name):
                    conv_out  = self.finalizer_strategy.plan(new_out)
                    conv_task = Task(new_out, conv_out, self.finalizer_strategy, depth + 2, parent=current)
                    tasks.append(conv_task)

        return PlanResult(
            tasks         = tasks,
            found_files   = found_files,
            skipped_files = skipped_files,
        )


# ---------------------------------------------------------------------------
# SynchronousRunner
# ---------------------------------------------------------------------------

class SynchronousRunner:
    """Execute a :class:`PlanResult` synchronously with a live progress bar."""

    def __init__(self, engine: AudioEngine, plan: PlanResult) -> None:
        self._engine:          AudioEngine = engine
        self._found_files:     int         = plan.found_files
        self._skipped_files:   int         = plan.skipped_files
        self.tasks:            list[Task]  = plan.tasks
        self._started:         bool        = False

    def process(self, dry_run: bool) -> PlanExecutionResult:
        """Execute the plan.  May only be called once.

        In dry-run mode, prints the full task list without executing and without
        a progress bar.  In normal mode, displays a live ``alive_bar`` with a
        running summary counter ``✔ {success}  ✘ {failed}  ⏭ {skipped}``.
        """
        assert not self._started, "process() must be called only once"
        self._started = True

        count_success = count_failed = count_skipped = 0

        if dry_run:
            print(f"  Audio pipeline — {len(self.tasks)} planned task(s):")
            for task in self.tasks:
                print(f"    [{task.strategy.strategy_short}]  {task.output.name}")
            return PlanExecutionResult(count_success, count_failed, count_skipped)

        def _summary() -> str:
            return f"✔ {count_success}  ✘ {count_failed}  ⏭ {count_skipped}"

        try:
            with alive_bar(len(self.tasks), title="Audio Pipeline") as progress:
                for task in self.tasks:
                    if task.parent and task.parent.failed:
                        task.failed = True
                        count_skipped += 1
                        logger.warning("Skipped (parent failure): %s", task.source.name)
                        progress.text = _summary()  # type: ignore[attr-defined]
                        progress()                  # type: ignore[operator]
                        continue

                    try:
                        task.strategy.execute(task.source, task.output, dry_run=False)
                        count_success += 1
                        logger.info("SUCCESS [%s] %s", task.strategy.name, task.output.name)
                    except Exception as exc:
                        task.failed = True
                        count_failed += 1
                        logger.error("FAILURE [%s]: %s", task.strategy.name, str(exc)[:70])

                    progress.text = _summary()  # type: ignore[attr-defined]
                    progress()                  # type: ignore[operator]
        finally:
            return PlanExecutionResult(count_success, count_failed, count_skipped)


# ---------------------------------------------------------------------------
# AsyncRunner
# ---------------------------------------------------------------------------

class AsyncRunner:
    """Execute a :class:`PlanResult` concurrently using asyncio."""

    def __init__(
        self,
        engine:       AudioEngine,
        plan:         PlanResult,
        max_parallel: int = 4,
    ) -> None:
        self._engine:     AudioEngine                    = engine
        self._semaphore:  asyncio.Semaphore              = asyncio.Semaphore(max_parallel)
        self.tasks:       list[Task]                     = plan.tasks
        self.registry:    dict[Path, asyncio.Task[bool]] = {}
        self._progress:   Callable[..., None] | None     = None
        self._started:    bool                           = False

    async def process(self, dry_run: bool = False) -> PlanExecutionResult:
        """Execute the plan concurrently.  May only be called once."""
        assert not self._started, "process() must be called only once"
        self._started = True

        parent_tasks  = {t.parent for t in self.tasks if t.parent}
        terminal_tasks = set(self.tasks) - parent_tasks
        if not terminal_tasks:
            raise RuntimeError("Cyclic dependencies detected in plan.")

        with alive_bar(len(self.tasks), title="Audio Pipeline") as progress:
            self._progress = progress
            await asyncio.gather(*(self._get_or_execute(t, dry_run) for t in terminal_tasks))
            self._progress = None

        succeeded = sum(1 for t in self.tasks if not t.failed)
        failed    = sum(1 for t in self.tasks if t.failed)
        return PlanExecutionResult(succeeded, failed, len(self.tasks) - succeeded - failed)

    async def _get_or_execute(self, task: Task, dry_run: bool) -> bool:
        key = task.output
        if key not in self.registry:
            coro = self._run_task(task, dry_run)
            t    = asyncio.create_task(coro)
            if self._progress:
                t.add_done_callback(lambda _fut, p=self._progress: p())
            self.registry[key] = t
        return await self.registry[key]

    async def _run_task(self, task: Task, dry_run: bool) -> bool:
        if task.parent:
            parent_ok = await self._get_or_execute(task.parent, dry_run)
            if not parent_ok:
                task.failed = True
                logger.warning("Skipped (parent failure): %s", task.source.name)
                return False

        async with self._semaphore:
            try:
                await task.strategy.execute_async(task.source, task.output, dry_run)
                logger.info("SUCCESS [%s] %s", task.strategy.name, task.output.name)
                return True
            except Exception as exc:
                task.failed = True
                logger.error("FAILURE [%s]: %s", task.strategy.name, str(exc)[:70])
                return False


# ---------------------------------------------------------------------------
# AudioResult + process_audio_streams
# ---------------------------------------------------------------------------

@dataclass
class AudioResult:
    """Result of the audio processing phase.

    Attributes:
        output_files: All processed audio files produced (AAC delivery files).
        reused:       True if existing files were reused without re-processing.
        needs_work:   True if processing would be performed (dry-run indicator).
        success:      True if processing succeeded.
        error:        Error message when *success* is False.
    """

    output_files: list[Path]
    reused:       bool
    needs_work:   bool
    success:      bool
    error:        str | None = None


def process_audio_streams(
    audio_files:        list[Path],
    output_dir:         Path,
    force:              bool       = False,
    dry_run:            bool       = False,
    audio_convert:      str | None = None,
    audio_codec:        str | None = None,
    audio_base_bitrate: str | None = None,
) -> AudioResult:
    """Process audio files through the full strategy graph and convert to AAC delivery files.

    Applies the complete audio processing graph:
    - 7.1 → 5.1 downmix (single-pass)
    - 5.1 → 2.0 std / night / nboost downmix + EBU R128 (2-pass each)
    - norm (EBU R128 only, 2-pass, for any non-normalised source)
    - dynaudnorm (dynamic normalisation on top of any normalised output)
    - AAC conversion finalizer (applied to files matching the convert filter)

    Args:
        audio_files:        Extracted audio files to process.
        output_dir:         Directory for processed audio output.
        force:              Re-process even when output files already exist.
        dry_run:            Report status only; do not perform actual processing.
        audio_convert:      Regex pattern selecting processed audio files to convert to the
                            final delivery format. Overrides the config-derived
                            ``audio_output.convert_filter`` when provided.
        audio_codec:        Override audio codec for all conversion profiles (e.g. ``'aac'``).
        audio_base_bitrate: Base bitrate for 2.0 stereo conversion (e.g. ``'192k'``). Bitrates
                            for other channel layouts are scaled proportionally by channel count.

    Returns:
        :class:`AudioResult` with paths to all produced AAC delivery files.

    Requirements:
        7.1, 7.2, 9.2, 9.3
    """
    try:
        logger.info("Audio processing phase starting")
        output_dir.mkdir(parents=True, exist_ok=True)

        # --- Load config profiles and apply CLI overrides ---
        from pyqenc.config import ConfigManager
        config_manager = ConfigManager()
        audio_output_config = config_manager.get_audio_output_config()

        # Build effective profiles: start from config, apply codec/bitrate overrides
        effective_profiles: dict[str, AudioConversionProfile] = {}
        for layout, profile in audio_output_config.profiles.items():
            effective_codec   = audio_codec or profile.codec
            effective_bitrate = profile.bitrate
            if audio_base_bitrate:
                # Scale from the supplied base (2.0 stereo = 2 channels)
                base_str = audio_base_bitrate.lower().rstrip("k")
                try:
                    base_kbps = int(base_str)
                    ch_count  = _CHANNEL_COUNTS.get(f"ch={layout}", 2)
                    effective_bitrate = f"{int(base_kbps * ch_count / 2)}k"
                except ValueError:
                    logger.warning(
                        "Cannot parse audio_base_bitrate %r; using config bitrate %r for layout %r",
                        audio_base_bitrate, profile.bitrate, layout,
                    )
            effective_profiles[layout] = AudioConversionProfile(
                codec     = effective_codec,
                bitrate   = effective_bitrate,
                extension = profile.extension,
            )

        # Effective convert filter: CLI override takes precedence over config
        effective_convert_filter = audio_convert or audio_output_config.convert_filter

        # --- Reuse check ---
        if not force:
            existing_aac = sorted(output_dir.glob("*.aac"))
            if existing_aac:
                logger.info("Reusing existing processed audio: %d AAC file(s)", len(existing_aac))
                if dry_run:
                    logger.info("[DRY-RUN] Audio processing: Complete (reusing existing files)")
                return AudioResult(
                    output_files = existing_aac,
                    reused       = True,
                    needs_work   = False,
                    success      = True,
                )

        # --- Dry-run ---
        if dry_run:
            logger.info("[DRY-RUN] Would process %d audio files", len(audio_files))
            # Build and display the plan without executing
            _build_and_display_dry_run_plan(
                audio_files            = audio_files,
                output_dir             = output_dir,
                effective_profiles     = effective_profiles,
                effective_convert_filter = effective_convert_filter,
                audio_base_bitrate     = audio_base_bitrate,
            )
            return AudioResult(
                output_files = [],
                reused       = False,
                needs_work   = True,
                success      = True,
            )

        # --- Convert source files to FLAC intermediates ---
        logger.info("Converting %d audio files to FLAC for processing", len(audio_files))
        flac_files: list[Path] = []
        for audio_file in audio_files:
            target = output_dir / (audio_file.stem + ".flac")
            if not target.exists():
                cmd: list[str | os.PathLike] = [
                    "ffmpeg",
                    "-i",   audio_file,
                    "-c:a", "flac",
                    target,
                ]
                try:
                    result = run_ffmpeg(cmd, output_file=target)
                    if result.success:
                        logger.debug("Converted %s → %s", audio_file.name, target.name)
                    else:
                        logger.error("Failed to convert %s", audio_file.name)
                        continue
                except Exception as exc:
                    logger.error("Failed to convert %s: %s", audio_file.name, exc)
                    continue
            flac_files.append(target)

        if not flac_files:
            logger.error("No audio files were successfully converted to FLAC")
            return AudioResult(
                output_files = [],
                reused       = False,
                needs_work   = False,
                success      = False,
                error        = "Failed to convert audio files to FLAC",
            )

        # --- Instantiate all strategies ---
        aac_finalizer = ConversionStrategy(
            profiles              = effective_profiles,
            base_bitrate_override = audio_base_bitrate,
        )
        engine = AudioEngine(
            strategies = [
                DownmixStrategy71to51(),
                DownmixStrategy51to20Std(),
                DownmixStrategy51to20Night(),
                DownmixStrategy51to20NBoost(),
                NormStrategy(),
                DynaudnormStrategy(),
            ],
            finalizer = aac_finalizer,
        )

        # --- Build and execute the unified plan ---
        plan = engine.build_plan(
            directory      = output_dir,
            convert_filter = effective_convert_filter,
        )
        logger.info("Audio pipeline plan: %d tasks", len(plan.tasks))

        exec_result = SynchronousRunner(engine, plan).process(dry_run=False)
        logger.info(
            "Audio pipeline complete: %d succeeded, %d failed, %d skipped",
            exec_result.success, exec_result.failed, exec_result.skipped,
        )
        if exec_result.failed:
            logger.warning("Audio pipeline had %d failure(s)", exec_result.failed)

        # --- Collect all produced AAC delivery files ---
        output_files = sorted(output_dir.glob("*.aac"))
        logger.info("Audio processing complete: %d AAC delivery file(s)", len(output_files))

        return AudioResult(
            output_files = output_files,
            reused       = False,
            needs_work   = False,
            success      = True,
        )

    except Exception as exc:
        logger.critical("Audio processing failed: %s", exc, exc_info=True)
        return AudioResult(
            output_files = [],
            reused       = False,
            needs_work   = False,
            success      = False,
            error        = str(exc),
        )


def _build_and_display_dry_run_plan(
    audio_files:             list[Path],
    output_dir:              Path,
    effective_profiles:      dict[str, AudioConversionProfile],
    effective_convert_filter: str,
    audio_base_bitrate:      str | None,
) -> None:
    """Build and print the audio processing plan for dry-run mode (no execution).

    Args:
        audio_files:              Source audio files.
        output_dir:               Output directory (used as plan root).
        effective_profiles:       Resolved conversion profiles.
        effective_convert_filter: Compiled convert-filter regex string.
        audio_base_bitrate:       Base bitrate override (passed to ConversionStrategy).
    """
    aac_finalizer = ConversionStrategy(
        profiles              = effective_profiles,
        base_bitrate_override = audio_base_bitrate,
    )
    engine = AudioEngine(
        strategies = [
            DownmixStrategy71to51(),
            DownmixStrategy51to20Std(),
            DownmixStrategy51to20Night(),
            DownmixStrategy51to20NBoost(),
            NormStrategy(),
            DynaudnormStrategy(),
        ],
        finalizer = aac_finalizer,
    )
    # Temporarily copy source files into output_dir so build_plan can discover them
    # (they may not exist yet in dry-run mode — use the actual extracted files directly
    # by scanning the parent directory for .mka files matching audio_files).
    # We build the plan against the output_dir; if it's empty we show a placeholder.
    plan = engine.build_plan(
        directory      = output_dir,
        convert_filter = effective_convert_filter,
    )
    if plan.tasks:
        runner = SynchronousRunner(engine, plan)
        runner.process(dry_run=True)
    else:
        logger.info("[DRY-RUN] Audio pipeline: no tasks planned (output_dir may be empty)")
        logger.info("[DRY-RUN] Source files that would be processed: %d", len(audio_files))
