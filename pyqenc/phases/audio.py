"""
Audio processing phase for the quality-based encoding pipeline.

This module handles audio stream processing using audio strategies
to generate normalized stereo variants for day and night modes.

Inlined from pyqenc/legacy/pymkva2/:
  BaseStrategy, ConversionStrategy, DownmixStrategy, TrueNormalizeStrategy,
  Task, AudioEngine, SynchronousRunner, AsyncRunner
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from alive_progress import alive_bar, config_handler

from pyqenc.utils.ffmpeg_runner import run_ffmpeg, run_ffmpeg_async

config_handler.set_global(enrich_print=False) # type: ignore
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INTERMEDIATE_CODEC:     str = "flac"
_INTERMEDIATE_EXTENSION: str = "flac"


# ---------------------------------------------------------------------------
# BaseStrategy
# ---------------------------------------------------------------------------

class BaseStrategy(ABC):
    """Base class for all audio processing strategies."""

    def __init__(self, name: str) -> None:
        self.name = name

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
# ConversionStrategy
# ---------------------------------------------------------------------------

class ConversionStrategy(BaseStrategy):
    """Convert audio codec/format.  Intended as a final (terminal) step."""

    def __init__(
        self,
        name:      str,
        codec:     str = "aac",
        extension: str = ".aac",
        bitrate:   str = "192k",
    ) -> None:
        super().__init__(name)
        self.codec     = codec
        self.extension = extension
        self.bitrate   = bitrate

    def check(self, source: Path) -> bool:
        return False  # never applied as an intermediate step

    def plan(self, source: Path) -> Path:
        return source.with_suffix(self.extension)

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = [
            "ffmpeg", "-hide_banner",
            "-i", source,
            "-c:a", self.codec, "-b:a", self.bitrate,
            "-y", output,
        ]
        result = run_ffmpeg(cmd)
        if not result.success:
            raise RuntimeError(f"ffmpeg conversion failed for {source.name}")

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = [
            "ffmpeg", "-hide_banner",
            "-i", source,
            "-c:a", self.codec, "-b:a", self.bitrate,
            "-y", output,
        ]
        result = await run_ffmpeg_async(cmd)
        if not result.success:
            raise RuntimeError(f"ffmpeg conversion failed for {source.name}")


# ---------------------------------------------------------------------------
# DownmixStrategy
# ---------------------------------------------------------------------------

class DownmixStrategy(BaseStrategy):
    """Downmix audio by running ffmpeg with the supplied audio filters."""

    def __init__(
        self,
        name:         str,
        pattern:      str | re.Pattern[str],
        suffix:       str,
        ffmpeg_args:  list[str],
    ) -> None:
        """
        Args:
            name:        Human-readable strategy name.
            pattern:     Substring or compiled regex to match against the source filename.
            suffix:      Replacement string for the matched pattern in the output stem.
            ffmpeg_args: Extra ffmpeg arguments inserted before the output codec flags.

        Example::

            DownmixStrategy("5.1->Stereo", "ch=5.1", "ch=2.0", ffmpeg_args=["-ac", "2"])
        """
        super().__init__(name)
        self.pattern = pattern
        self.suffix  = suffix
        self.args    = ffmpeg_args

    def check(self, source: Path) -> bool:
        if isinstance(self.pattern, re.Pattern):
            return bool(self.pattern.search(source.name))
        return self.pattern in source.name.lower()

    def plan(self, source: Path) -> Path:
        stem = source.stem
        if isinstance(self.pattern, re.Pattern):
            new_stem = self.pattern.sub(self.suffix, stem)
        else:
            new_stem = stem.replace(self.pattern, self.suffix)
        return source.parent / f"{new_stem}.{_INTERMEDIATE_EXTENSION}"

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = (
            ["ffmpeg", "-hide_banner", "-i", source]
            + self.args
            + ["-c:a", _INTERMEDIATE_CODEC, "-y", output]
        )
        result = run_ffmpeg(cmd)
        if not result.success:
            raise RuntimeError(f"ffmpeg downmix failed for {source.name}")

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        cmd: list[str | os.PathLike] = (
            ["ffmpeg", "-hide_banner", "-i", source]
            + self.args
            + ["-c:a", _INTERMEDIATE_CODEC, "-y", output]
        )
        result = await run_ffmpeg_async(cmd)
        if not result.success:
            raise RuntimeError(f"ffmpeg downmix failed for {source.name}")


# ---------------------------------------------------------------------------
# TrueNormalizeStrategy
# ---------------------------------------------------------------------------

class TrueNormalizeStrategy(BaseStrategy):
    """EBU R128 static loudness normalisation strategy."""

    def __init__(self, name: str, prefix: str) -> None:
        super().__init__(name)
        self.prefix  = prefix
        self._substr  = f"{prefix}2.0"
        self._substr2 = f"{prefix}stereo"

    def check(self, source: Path) -> bool:
        return (
            (self._substr in source.name or self._substr2 in source.name)
            and "-norm" not in source.name
        )

    def plan(self, source: Path) -> Path:
        stem = source.stem
        if self._substr2 in stem:
            stem = stem.replace(self._substr2, f"{self._substr}-norm")
        else:
            stem = stem.replace(self._substr, f"{self._substr}-norm")
        return source.parent / f"{stem}.{_INTERMEDIATE_EXTENSION}"

    def execute(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        from ffmpeg_normalize import FFmpegNormalize  # optional dep; imported lazily
        norm = FFmpegNormalize(
            normalization_type="peak",
            target_level=-0.5,
            audio_codec=_INTERMEDIATE_CODEC,
            output_format=_INTERMEDIATE_EXTENSION,
        )
        norm.add_media_file(str(source), str(output))
        norm.run_normalization()

    async def execute_async(self, source: Path, output: Path, dry_run: bool) -> None:
        if dry_run:
            return
        await asyncio.to_thread(self.execute, source, output, dry_run)


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
    is_terminal: bool
    """True if this is a leaf node (final conversion step will be executed)."""
    failed:      bool      = field(default=False)
    """Set to True when execution fails."""
    parent:      Task|None = field(default=None)
    """Parent task, if any."""

    def __hash__(self) -> int:
        return hash(self.output)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Task) and self.output == other.output

    def __repr__(self) -> str:
        flags = ("🏁" if self.is_terminal else "") + ("❌" if self.failed else "")
        suffix = f" {flags}" if flags else ""
        return f"[{self.strategy.name}]{suffix} {self.output}"


# ---------------------------------------------------------------------------
# AudioEngine — plan builder
# ---------------------------------------------------------------------------

@dataclass
class PlanResult:
    """Processing plan produced by :meth:`AudioEngine.build_plan`."""

    tasks:           list[Task]
    found_files:     int
    skipped_files:   int
    skipped_results: int


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

    def build_plan(
        self,
        directory: Path,
        include:   str | None,
        exclude:   str | None,
        keep:      str | None,
        max_depth: int,
    ) -> PlanResult:
        """Scan *directory* and build a processing plan.

        Args:
            directory: Directory containing source audio files.
            include:   Regex string; only files matching this are processed.
            exclude:   Regex string; files matching this are skipped.
            keep:      Regex string; matching files are always finalised.
            max_depth: Maximum strategy-application depth.

        Returns:
            :class:`PlanResult` with the task list and discovery counters.
        """
        include_re = re.compile(include) if include else None
        exclude_re = re.compile(exclude) if exclude else None
        keep_re    = re.compile(keep)    if keep    else None

        initial = [
            Path(f) for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in (".flac", ".mka")
        ]
        found_files = len(initial)

        queue: deque[tuple[Path, int, Task | None]] = deque(
            (f, 0, None) for f in initial
        )
        seen:  set[str]  = set()
        tasks: list[Task] = []

        skipped_files   = 0
        skipped_results = 0

        def _to_skip(path: Path) -> bool:
            if exclude_re and exclude_re.search(path.name):
                return True
            if include_re and not include_re.search(path.name):
                return True
            return False

        while queue:
            target, depth, parent = queue.popleft()

            if _to_skip(target):
                if depth == 0:
                    skipped_files += 1
                else:
                    skipped_results += 1
                continue

            matched = [s for s in self.strategies if s.check(target)]
            if not matched and depth == 0:
                skipped_files += 1
                continue

            force_keep   = keep_re and keep_re.search(target.name)
            stop_reached = max_depth and depth >= max_depth
            is_terminal  = stop_reached or not matched

            if self.finalizer_strategy and (is_terminal or force_keep):
                out  = self.finalizer_strategy.plan(target)
                task = Task(target, out, self.finalizer_strategy, depth, True, parent=parent)
                tasks.append(task)

            if is_terminal:
                continue

            for strategy in matched:
                new_out = strategy.plan(target)
                if new_out.name in seen:
                    continue
                seen.add(new_out.name)
                current = Task(target, new_out, strategy, depth + 1, False, parent=parent)
                tasks.append(current)
                queue.append((new_out, depth + 1, current))

        return PlanResult(
            tasks           = tasks,
            found_files     = found_files,
            skipped_files   = skipped_files,
            skipped_results = skipped_results,
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
        self._skipped_results: int         = plan.skipped_results
        self.tasks:            list[Task]  = plan.tasks
        self._started:         bool        = False

    def process(self, dry_run: bool) -> PlanExecutionResult:
        """Execute the plan.  May only be called once."""
        assert not self._started, "process() must be called only once"
        self._started = True

        count_success = count_failed = count_skipped = 0
        try:
            with alive_bar(len(self.tasks), title="Audio Pipeline") as progress:
                for task in self.tasks:
                    type_label    = "🏁" if task.is_terminal else "⚙️"
                    progress.text = f"{task.strategy.name} {type_label} -> {task.output.name[:30]}"

                    if task.parent and task.parent.failed:
                        task.failed = True
                        count_skipped += 1
                        logger.warning("Skipped (parent failure): %s", task.source.name)
                        progress()  # type: ignore[operator]
                        continue

                    try:
                        task.strategy.execute(task.source, task.output, dry_run)
                        count_success += 1
                        logger.info("SUCCESS [%s] %s", task.strategy.name, task.output.name)
                    except Exception as exc:
                        task.failed = True
                        count_failed += 1
                        logger.error("FAILURE [%s]: %s", task.strategy.name, str(exc)[:70])
                    progress()  # type: ignore[operator]
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
        day_mode_files:   Processed day-mode audio files.
        night_mode_files: Processed night-mode audio files.
        reused:           True if existing files were reused without re-processing.
        needs_work:       True if processing would be performed (dry-run indicator).
        success:          True if processing succeeded.
        error:            Error message when *success* is False.
    """

    day_mode_files:   list[Path]
    night_mode_files: list[Path]
    reused:           bool
    needs_work:       bool
    success:          bool
    error:            str | None = None


def process_audio_streams(
    audio_files: list[Path],
    output_dir:  Path,
    force:       bool = False,
    dry_run:     bool = False,
) -> AudioResult:
    """Process audio files to generate normalised stereo variants.

    Produces:
    - Day mode:   normalised stereo AAC (192 kbps).
    - Night mode: normalised stereo AAC with dynamic-range compression (192 kbps).

    Args:
        audio_files: Extracted audio files to process.
        output_dir:  Directory for processed audio output.
        force:       Re-process even when output files already exist.
        dry_run:     Report status only; do not perform actual processing.

    Returns:
        :class:`AudioResult` with paths to processed audio files.

    Requirements:
        6.3
    """
    try:
        logger.info("Audio processing phase starting")
        output_dir.mkdir(parents=True, exist_ok=True)

        # --- Reuse check ---
        if not force:
            existing_day   = list(output_dir.glob("*_day.aac"))
            existing_night = list(output_dir.glob("*_night.aac"))
            if (
                len(existing_day)   >= len(audio_files)
                and len(existing_night) >= len(audio_files)
            ):
                logger.info(
                    "Reusing existing processed audio: %d day, %d night",
                    len(existing_day), len(existing_night),
                )
                if dry_run:
                    logger.info("[DRY-RUN] Audio processing: Complete (reusing existing files)")
                return AudioResult(
                    day_mode_files   = sorted(existing_day),
                    night_mode_files = sorted(existing_night),
                    reused           = True,
                    needs_work       = False,
                    success          = True,
                )

        # --- Dry-run ---
        if dry_run:
            logger.info("[DRY-RUN] Would process %d audio files", len(audio_files))
            return AudioResult(
                day_mode_files   = [],
                night_mode_files = [],
                reused           = False,
                needs_work       = True,
                success          = True,
            )

        # --- Convert source files to FLAC intermediates ---
        logger.info("Converting %d audio files to FLAC for processing", len(audio_files))
        flac_files: list[Path] = []
        for audio_file in audio_files:
            target = output_dir / (audio_file.stem + ".flac")
            if not target.exists():
                cmd: list[str | os.PathLike] = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-i", audio_file,
                    "-c:a", "flac",
                    "-y", target,
                ]
                try:
                    result = run_ffmpeg(cmd)
                    if result.success:
                        logger.debug("Converted %s -> %s", audio_file.name, target.name)
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
                day_mode_files   = [],
                night_mode_files = [],
                reused           = False,
                needs_work       = False,
                success          = False,
                error            = "Failed to convert audio files to FLAC",
            )

        # --- Strategy definitions ---
        downmix_strategy = DownmixStrategy(
            name        = "Downmix to Stereo",
            pattern     = "ch=",
            suffix      = "ch=2.0",
            ffmpeg_args = ["-ac", "2"],
        )
        normalize_day_strategy = TrueNormalizeStrategy(
            name   = "Normalize Day",
            prefix = "ch=",
        )
        normalize_night_strategy = DownmixStrategy(
            name        = "Normalize Night",
            pattern     = "ch=2.0-norm",
            suffix      = "ch=2.0-norm-night",
            ffmpeg_args = ["-af", "dynaudnorm=f=500:g=31:p=0.95:m=10.0:r=0.5:b=1"],
        )
        aac_finalizer = ConversionStrategy(
            name      = "Convert to AAC",
            codec     = "aac",
            extension = ".aac",
            bitrate   = "192k",
        )

        # --- Day mode ---
        logger.info("Processing day mode audio (normalised stereo)")
        day_engine = AudioEngine(
            strategies = [downmix_strategy, normalize_day_strategy],
            finalizer  = aac_finalizer,
        )
        day_plan   = day_engine.build_plan(
            directory = output_dir,
            include   = None,
            exclude   = "-norm",
            keep      = "-norm",
            max_depth = 2,
        )
        logger.info("Day mode plan: %d tasks", len(day_plan.tasks))
        day_result = SynchronousRunner(day_engine, day_plan).process(dry_run=False)
        logger.info("Day mode: %d succeeded, %d failed", day_result.success, day_result.failed)
        if day_result.failed:
            logger.warning("Day mode processing had %d failures", day_result.failed)

        # --- Night mode ---
        logger.info("Processing night mode audio (normalised stereo + compression)")
        night_engine = AudioEngine(
            strategies = [downmix_strategy, normalize_day_strategy, normalize_night_strategy],
            finalizer  = aac_finalizer,
        )
        night_plan   = night_engine.build_plan(
            directory = output_dir,
            include   = None,
            exclude   = None,
            keep      = "-night",
            max_depth = 3,
        )
        logger.info("Night mode plan: %d tasks", len(night_plan.tasks))
        night_result = SynchronousRunner(night_engine, night_plan).process(dry_run=False)
        logger.info("Night mode: %d succeeded, %d failed", night_result.success, night_result.failed)
        if night_result.failed:
            logger.warning("Night mode processing had %d failures", night_result.failed)

        # --- Collect and rename output files ---
        all_aac        = sorted(output_dir.glob("*.aac"))
        day_mode_files = [f for f in all_aac if "-norm.aac" in f.name and "-night.aac" not in f.name]
        night_mode_files = [f for f in all_aac if "-night.aac" in f.name]

        renamed_day:   list[Path] = []
        renamed_night: list[Path] = []

        for idx, day_file in enumerate(day_mode_files, start=1):
            new_name = output_dir / f"audio_{idx:03d}_day.aac"
            if day_file != new_name:
                day_file.rename(new_name)
                logger.debug("Renamed %s -> %s", day_file.name, new_name.name)
            renamed_day.append(new_name)

        for idx, night_file in enumerate(night_mode_files, start=1):
            new_name = output_dir / f"audio_{idx:03d}_night.aac"
            if night_file != new_name:
                night_file.rename(new_name)
                logger.debug("Renamed %s -> %s", night_file.name, new_name.name)
            renamed_night.append(new_name)

        logger.info(
            "Audio processing complete: %d day files, %d night files",
            len(renamed_day), len(renamed_night),
        )
        return AudioResult(
            day_mode_files   = renamed_day,
            night_mode_files = renamed_night,
            reused           = False,
            needs_work       = False,
            success          = True,
        )

    except Exception as exc:
        logger.critical("Audio processing failed: %s", exc, exc_info=True)
        return AudioResult(
            day_mode_files   = [],
            night_mode_files = [],
            reused           = False,
            needs_work       = False,
            success          = False,
            error            = str(exc),
        )
