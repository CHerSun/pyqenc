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
