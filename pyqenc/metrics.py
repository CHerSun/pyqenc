"""Pipeline metrics collection and reporting.

Provides `MetricsCollector` (Protocol), `YamlMetricsCollector` (YAML-backed
implementation), and `NoOpMetricsCollector` (no-op for tests / standalone callers).

Metrics are accumulated throughout a pipeline run and persisted incrementally to
``metrics.yaml`` in the work directory root using the `.tmp`-then-rename atomic
write protocol.  The report survives interruptions and resumes across runs.

Usage (orchestrator)::

    collector = YamlMetricsCollector(work_dir=config.work_dir, config=config)
    registry  = _build_registry(config, collector)
    # ... run phases ...
    collector.flush(partial=False)

Usage (standalone / tests)::

    collector = NoOpMetricsCollector()
    registry  = _build_registry(config, collector)
"""

from __future__ import annotations

__all__ = [
    # Enums
    "TimeKey",
    "SpaceKey",
    # Dataclasses
    "ConvergenceUpdate",
    # Pydantic models
    "AttemptStats",
    "ConvergenceStats",
    "TimeEntry",
    "SpaceEntry",
    "TimeDistribution",
    "SpaceDistribution",
    "ConvergenceSection",
    "PipelineMetrics",
    # Protocol + implementations
    "MetricsCollector",
    "NoOpMetricsCollector",
    # Internal helpers exposed for testing
    "_ConvergenceAccumulator",
    "_update_accumulator",
    "_compute_convergence",
    "_measure_space",
    # Added in task 7:
    # "YamlMetricsCollector",
]

import contextlib
import logging
import math          # noqa: F401  (used in YamlMetricsCollector — task 7)
import os            # noqa: F401  (used in _measure_space — task 5)
import signal        # noqa: F401  (used in orchestrator signal handlers — task 19)
import time as _time  # noqa: F401  (used in YamlMetricsCollector — task 7)
from contextlib import contextmanager  # noqa: F401  (used in YamlMetricsCollector — task 7)
from dataclasses import dataclass, field  # noqa: F401  (field used in ConvergenceAccumulator — task 5)
from datetime import datetime  # noqa: F401  (used in flush — task 7)
from enum import StrEnum
from pathlib import Path  # noqa: F401  (used in _measure_space — task 5)
from typing import TYPE_CHECKING, Iterator, Protocol  # noqa: F401

import yaml  # noqa: F401  (used in YamlMetricsCollector — task 7)
from pydantic import BaseModel

if TYPE_CHECKING:
    from pyqenc.config import PipelineConfig  # noqa: F401  (used in YamlMetricsCollector — task 7)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

FLUSH_INTERVAL: int = 10
"""Number of recording calls between automatic incremental flushes."""

METRICS_YAML_FILENAME: str = "metrics.yaml"
"""Filename of the metrics sidecar written to the work directory root."""

_TEMP_SUFFIX: str = ".tmp"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TimeKey(StrEnum):
    """Dotted string keys identifying pipeline timing categories.

    Each value is of the form ``"phase.event"``.  The prefix identifies the
    phase/subsystem; the suffix identifies the event type within it.  Internal
    storage is ``dict[TimeKey, float]`` (accumulated seconds).  Grouping by
    prefix at report time is derived by splitting on ``"."``.
    """

    JOB_PROBE             = "job.probe"
    JOB_CROP_DETECT       = "job.crop_detect"
    EXTRACTION            = "extraction.mkvextract"
    CHUNKING_SCENE_DETECT = "chunking.scene_detect"
    CHUNKING_SPLIT        = "chunking.split"
    AUDIO                 = "audio.processing"
    ENCODING_OPTIMIZATION = "encoding.optimization"
    ENCODING_MAIN         = "encoding.main"
    MERGE_CONCAT          = "merge.concat"
    MERGE_QUALITY_MEASURE = "merge.quality_measure"
    RECOVERY              = "recovery"


class SpaceKey(StrEnum):
    """Dotted string keys identifying artifact storage categories.

    Same ``StrEnum`` pattern as :class:`TimeKey`.  Internal storage is
    ``dict[SpaceKey, int]`` (bytes, exact).  Grouping by prefix at report time
    is derived by splitting on ``"."``.
    """

    SOURCE             = "source"
    EXTRACTED_VIDEO    = "extracted.video"
    EXTRACTED_AUDIO    = "extracted.audio"
    EXTRACTED_OTHER    = "extracted.other"
    CHUNKS             = "chunks"
    AUDIO_INTERMEDIATE = "audio.intermediate"
    AUDIO_FINAL        = "audio.final"
    ENCODING_WORKSPACE = "encoding.workspace"
    ENCODING_OUTPUTS   = "encoding.outputs"
    FINAL              = "final"


# ---------------------------------------------------------------------------
# Helper formatting functions
# ---------------------------------------------------------------------------


def _format_duration(seconds: int) -> str:
    """Format *seconds* as ``[Dd ]HH:MM:SS``.

    The days component is omitted when zero, e.g. ``"00:05:30"`` for 330 s
    and ``"1d 02:03:04"`` for 93784 s.
    """
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_gb(n: int) -> str:
    """Format *n* bytes as a ``"X.XX GB"`` string (1024-based, 2 decimal places)."""
    return f"{n / 1024 ** 3:.2f} GB"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ConvergenceUpdate:
    """Passed by phases to :meth:`MetricsCollector.record_step` when a chunk's
    CRF search converges.
    """

    strategy:      str  # display name (with + separators)
    attempt_count: int  # total attempts for this chunk/strategy pair


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AttemptStats(BaseModel):
    """Per-strategy attempt count statistics."""

    total:  int
    min:    int
    mean:   float  # rounded to 1 decimal place
    max:    int
    stddev: float  # population stddev, rounded to 1 decimal place


class ConvergenceStats(BaseModel):
    """CRF convergence statistics for a single encoding strategy."""

    strategy: str
    chunks:   int
    attempts: AttemptStats


class TimeEntry(BaseModel):
    """A single row in the time distribution breakdown."""

    category: str  # TimeKey value, e.g. "encoding.main"
    seconds:  int  # integer seconds
    duration: str  # "[Dd ]HH:MM:SS"
    percent:  str  # "X.X%"


class SpaceEntry(BaseModel):
    """A single row in the space distribution breakdown."""

    category: str  # SpaceKey value, e.g. "encoding.workspace"
    size:     str  # "X.XX GB"
    percent:  str  # "X.X%"


class TimeDistribution(BaseModel):
    """Time distribution section of the metrics report."""

    updated_at:     str             # "YYYY-MM-DD HH:MM:SS"
    total_seconds:  int
    total_duration: str             # "[Dd ]HH:MM:SS"
    breakdown:      list[TimeEntry] # sorted descending by seconds


class SpaceDistribution(BaseModel):
    """Space distribution section of the metrics report."""

    updated_at: str              # "YYYY-MM-DD HH:MM:SS"
    total_size: str              # "X.XX GB"
    breakdown:  list[SpaceEntry] # sorted descending by bytes


class ConvergenceSection(BaseModel):
    """Convergence statistics section of the metrics report.

    Omitted from the report when no convergence data has been collected.
    """

    updated_at: str                    # same as TimeDistribution.updated_at
    strategies: list[ConvergenceStats]


class PipelineMetrics(BaseModel):
    """Top-level Pydantic model serialised to ``metrics.yaml``.

    ``convergence`` is ``None`` when no encoded result data has been collected
    (e.g. all chunks were reused from a prior run).
    """

    run_date:           str                        # "YYYY-MM-DD HH:MM:SS" — last file write
    partial:            bool
    time_distribution:  TimeDistribution
    space_distribution: SpaceDistribution
    convergence:        ConvergenceSection | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class MetricsCollector(Protocol):
    """Phase-facing recording surface for pipeline metrics.

    Defined as a ``Protocol`` so that alternative backends (e.g. OpenTelemetry,
    Prometheus) can be substituted by injecting a different implementation
    without changing any phase code.  Concrete implementations should inherit
    from this class directly so conformance is verified at dev-time by the type
    checker rather than relying on structural duck-typing.

    Phases use only :meth:`time` and :meth:`record_step`.  :meth:`flush` is
    intentionally part of the full interface but is **not** called by phases —
    it is reserved for the orchestrator.
    """

    def time(self, key: TimeKey) -> contextlib.AbstractContextManager[None]:
        """Return a context manager that accumulates elapsed seconds for *key*.

        Records ``time.monotonic()`` on enter; on exit calls
        ``record_step(key, elapsed)``.
        """
        ...

    def record_step(
        self,
        key:                TimeKey,
        elapsed_seconds:    float,
        convergence_update: ConvergenceUpdate | None = None,
    ) -> None:
        """Accumulate *elapsed_seconds* for *key*.

        If *convergence_update* is provided, the per-strategy Welford
        accumulators are updated for the named strategy.

        Phases call this after each discrete unit of work (e.g. after each
        encoding attempt or chunk completion).  They have no knowledge of
        flushing — that is self-managed by the collector.
        """
        ...

    def flush(self, partial: bool = True) -> None:
        """Write the current metrics state to disk.

        *partial=True* (default) marks the report as in-progress.
        *partial=False* is set only by the orchestrator after all phases
        complete successfully.

        This method is **not** part of the phase-facing surface — phases must
        not call it.
        """
        ...


# ---------------------------------------------------------------------------
# Internal convergence accumulator
# ---------------------------------------------------------------------------


@dataclass
class _ConvergenceAccumulator:
    """Per-strategy running state for Welford's online mean/variance algorithm.

    All fields are updated incrementally via :func:`_update_accumulator` so
    that no raw attempt list needs to be stored in memory or on disk.
    """

    n:            int   = 0    # number of chunks that completed convergence
    total:        int   = 0    # sum of all attempt counts
    min:          int   = 0    # minimum attempt count seen
    max:          int   = 0    # maximum attempt count seen
    welford_mean: float = 0.0  # running mean (Welford)
    welford_M2:   float = 0.0  # running sum of squared deviations (Welford)


def _update_accumulator(acc: _ConvergenceAccumulator, x: int) -> None:
    """Update *acc* with a new observation *x* using Welford's online algorithm."""
    acc.n     += 1
    acc.total += x
    acc.min    = x if acc.n == 1 else min(acc.min, x)
    acc.max    = x if acc.n == 1 else max(acc.max, x)
    delta      = x - acc.welford_mean
    acc.welford_mean += delta / acc.n
    acc.welford_M2   += delta * (x - acc.welford_mean)  # uses updated mean


def _compute_convergence(
    accumulators: dict[str, _ConvergenceAccumulator],
) -> list[ConvergenceStats] | None:
    """Build :class:`ConvergenceStats` list from running accumulators.

    Returns ``None`` when all accumulators have ``n == 0`` (no data collected),
    which causes the ``convergence`` section to be omitted from the YAML output.
    Results are sorted by strategy name for deterministic output.
    """
    if all(acc.n == 0 for acc in accumulators.values()):
        return None

    results: list[ConvergenceStats] = []
    for strategy, acc in sorted(accumulators.items()):
        if acc.n == 0:
            continue
        stddev = 0.0 if acc.n == 1 else math.sqrt(acc.welford_M2 / acc.n)
        results.append(
            ConvergenceStats(
                strategy=strategy,
                chunks=acc.n,
                attempts=AttemptStats(
                    total=acc.total,
                    min=acc.min,
                    mean=round(acc.welford_mean, 1),
                    max=acc.max,
                    stddev=round(stddev, 1),
                ),
            )
        )
    return results or None


# ---------------------------------------------------------------------------
# Space measurement
# ---------------------------------------------------------------------------


def _measure_space(work_dir: Path, config: "PipelineConfig") -> dict[SpaceKey, int]:
    """Measure on-disk sizes for each :class:`SpaceKey` category.

    Performs a point-in-time filesystem scan using only ``Path.stat()`` and
    directory traversal — no ffprobe or ffmpeg calls.  Missing directories or
    files contribute ``0`` bytes.  ``OSError`` on individual ``stat()`` calls
    is caught and logged at DEBUG level.

    Args:
        work_dir: Pipeline work directory root.
        config:   Pipeline configuration (provides ``source_video`` path).

    Returns:
        Mapping of :class:`SpaceKey` to byte counts.
    """

    def _safe_size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError as exc:
            logger.debug("Space scan: cannot stat %s: %s", p, exc)
            return 0

    def _sum_dir_recursive(d: Path) -> int:
        if not d.is_dir():
            return 0
        return sum(_safe_size(f) for f in d.rglob("*") if f.is_file())

    def _sum_dir_flat(d: Path, *, suffix: str | None = None, exclude_suffix: str | None = None) -> int:
        """Sum files in *d* (non-recursive), optionally filtered by suffix."""
        if not d.is_dir():
            return 0
        total = 0
        for f in d.iterdir():
            if not f.is_file():
                continue
            if suffix is not None and f.suffix.lower() != suffix:
                continue
            if exclude_suffix is not None and f.suffix.lower() == exclude_suffix:
                continue
            total += _safe_size(f)
        return total

    extracted = work_dir / "extracted"
    audio_dir = work_dir / "audio"

    # extracted/ split: .mkv → video, .mka → audio, everything else → other
    extracted_video = 0
    extracted_audio = 0
    extracted_other = 0
    if extracted.is_dir():
        for f in extracted.iterdir():
            if not f.is_file():
                continue
            suf = f.suffix.lower()
            sz  = _safe_size(f)
            if suf == ".mkv":
                extracted_video += sz
            elif suf == ".mka":
                extracted_audio += sz
            else:
                extracted_other += sz

    return {
        SpaceKey.SOURCE:             _safe_size(config.source_video),
        SpaceKey.EXTRACTED_VIDEO:    extracted_video,
        SpaceKey.EXTRACTED_AUDIO:    extracted_audio,
        SpaceKey.EXTRACTED_OTHER:    extracted_other,
        SpaceKey.CHUNKS:             _sum_dir_recursive(work_dir / "chunks"),
        SpaceKey.AUDIO_INTERMEDIATE: _sum_dir_flat(audio_dir, suffix=".flac"),
        SpaceKey.AUDIO_FINAL:        _sum_dir_flat(audio_dir, exclude_suffix=".flac"),
        SpaceKey.ENCODING_WORKSPACE: _sum_dir_recursive(work_dir / "encoding"),
        SpaceKey.ENCODING_OUTPUTS:   _sum_dir_recursive(work_dir / "encoded"),
        SpaceKey.FINAL:              _sum_dir_recursive(work_dir / "final"),
    }


# ---------------------------------------------------------------------------
# No-op implementation
# ---------------------------------------------------------------------------


class NoOpMetricsCollector(MetricsCollector):
    """Concrete no-op implementation of :class:`MetricsCollector`.

    Inherits directly from the Protocol so the type checker verifies
    conformance at definition time.  Discards all data without performing
    any I/O.  Used in tests and ``api.py`` standalone callers.
    """

    def time(self, key: TimeKey) -> contextlib.AbstractContextManager[None]:
        """Return a no-op context manager (``contextlib.nullcontext``)."""
        return contextlib.nullcontext()

    def record_step(
        self,
        key:                TimeKey,
        elapsed_seconds:    float,
        convergence_update: ConvergenceUpdate | None = None,
    ) -> None:
        """Discard all arguments — no-op."""

    def flush(self, partial: bool = True) -> None:
        """No-op — nothing to flush."""
