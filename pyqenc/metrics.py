"""Pipeline metrics collection and reporting.

Provides `MetricsCollector` (Protocol), `YamlMetricsCollector` (YAML-backed
implementation), and `NoOpMetricsCollector` (no-op for tests / standalone callers).

Metrics are accumulated throughout a pipeline run and persisted incrementally to
``metrics.yaml`` in the work directory root using the `.tmp`-then-rename atomic
write protocol.  The report survives interruptions and resumes across runs.

Usage (orchestrator)::

    collector = YamlMetricsCollector(work_dir=config.work_dir)
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
    "MetricKey",
    # Type aliases
    "MetricsStore",
    # Dataclasses
    "ConvergenceUpdate",
    # Pydantic models
    "AttemptStats",
    "ConvergenceStats",
    "TopLevelEntry",
    "DottedEntry",
    "DottedGroup",
    "TimeDistribution",
    "PipelineMetrics",
    # Protocol + implementations
    "MetricsCollector",
    "NoOpMetricsCollector",
    # Internal helpers exposed for testing
    "_ConvergenceAccumulator",
    "_update_accumulator",
    "_compute_convergence",
    "_compute_top_level_entries",
    "_compute_dotted_groups",
    # Added in task 7:
    "YamlMetricsCollector",
    # Active collector registry (task 19):
    "register_active_collector",
    "flush_active_collector",
]

import contextlib
import logging
import math  # noqa: F401  (used in YamlMetricsCollector — task 7)
import time as _time  # noqa: F401  (used in YamlMetricsCollector — task 7)
from dataclasses import (  # noqa: F401  (field used in ConvergenceAccumulator — task 5)
    dataclass,
    field,
)
from datetime import datetime  # noqa: F401  (used in flush — task 7)
from enum import StrEnum
from pathlib import Path  # noqa: F401  (used in YamlMetricsCollector — task 7)
from typing import TYPE_CHECKING, Protocol  # noqa: F401

import yaml  # noqa: F401  (used in YamlMetricsCollector — task 7)
from pydantic import BaseModel

from pyqenc.constants import DOTTED_KEY_SEPARATOR

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

FLUSH_INTERVAL: int = 3
"""Number of recording calls between automatic incremental flushes."""

METRICS_YAML_FILENAME: str = "metrics.yaml"
"""Filename of the metrics sidecar written to the work directory root."""

_TEMP_SUFFIX: str = ".tmp"

# ---------------------------------------------------------------------------
# Active collector registry — used by CLI signal handler
# ---------------------------------------------------------------------------

_active_collector: MetricsCollector | None = None


def register_active_collector(collector: MetricsCollector | None) -> None:
    """Register *collector* as the process-wide active collector.

    Called by the orchestrator when it constructs a ``YamlMetricsCollector``
    so that the CLI's SIGINT handler can flush it on forced exit.
    Pass ``None`` to clear the registration after the run completes.
    """
    global _active_collector
    _active_collector = collector


def flush_active_collector() -> None:
    """Flush the active collector if one is registered.

    Safe to call even when no collector is registered (no-op).  Used by the
    CLI SIGINT handler so metrics are written before ``os._exit``.
    """
    if _active_collector is not None:
        try:
            _active_collector.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Metrics: flush on exit failed: %s", exc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MetricKey(StrEnum):
    """Flat top-level keys identifying pipeline timing categories.

    Each value is a plain string with no dot separator (e.g. ``"encoding"``,
    ``"merge"``).  Dotted keys are formed at call time by passing one or more
    suffix parts to :meth:`MetricsCollector.time` or :meth:`MetricsCollector.step`,
    which joins them with :data:`~pyqenc.constants.DOTTED_KEY_SEPARATOR` internally.
    """

    JOB          = "job"
    EXTRACTION   = "extraction"
    CHUNKING     = "chunking"
    AUDIO        = "audio"
    ENCODING     = "encoding"
    OPTIMIZATION = "optimization"
    MERGE        = "merge"
    RECOVERY     = "recovery"


# ---------------------------------------------------------------------------
# MetricsStore type alias
# ---------------------------------------------------------------------------

MetricsStore = dict[str, float]
"""Flat mapping from metric key strings to accumulated float seconds.

Keys follow the two-tier naming convention:

- **Top-level** (no dot): wall-clock elapsed time per phase, e.g. ``"encoding"``.
- **Dotted** (one or more dots): per-process run time for sub-actions, e.g.
  ``"encoding.h265"``.  The prefix (everything left of the last dot) groups
  sibling keys for percentage calculation.
"""


# ---------------------------------------------------------------------------
# Private key helper functions
# ---------------------------------------------------------------------------


def _is_top_level(key: str) -> bool:
    """Return ``True`` when *key* contains no metric key separator.

    Top-level keys (e.g. ``"encoding"``) have no dot; dotted keys
    (e.g. ``"encoding.h265"``) have one or more dots.
    """
    return DOTTED_KEY_SEPARATOR not in key


def _last_dot_prefix(key: str) -> str:
    """Return everything to the left of the last metric key separator in *key*.

    For ``"encoding.h265"`` returns ``"encoding"``.
    For ``"a.b.c"`` returns ``"a.b"``.
    """
    return key.rsplit(DOTTED_KEY_SEPARATOR, 1)[0]


def _build_key(key: MetricKey, *parts: str) -> str:
    """Join *key* and zero or more *parts* with the metric key separator.

    With no parts returns the top-level key string (e.g. ``"encoding"``).
    With one or more parts returns a dotted key (e.g. ``"encoding.h265"``).
    """
    return DOTTED_KEY_SEPARATOR.join((key, *parts))


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


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ConvergenceUpdate:
    """Passed by phases to :meth:`MetricsCollector.step` when a chunk's
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
    avg:    float  # arithmetic mean, rounded to 1 decimal place
    max:    int
    stddev: float  # population stddev, rounded to 1 decimal place


class ConvergenceStats(BaseModel):
    """CRF convergence statistics for a single encoding strategy."""

    strategy: str
    chunks:   int
    attempts: AttemptStats


class TopLevelEntry(BaseModel):
    """A single row in the top-level (wall-clock) time distribution list."""

    key:      str  # e.g. "encoding"
    seconds:  int  # integer seconds
    duration: str  # "[Dd ]HH:MM:SS"
    percent:  str  # "X.X%" relative to grand total


class DottedEntry(BaseModel):
    """A single row in a dotted-key breakdown group."""

    key:      str  # e.g. "encoding.h265"
    seconds:  int  # integer seconds
    duration: str  # "[Dd ]HH:MM:SS"
    percent:  str  # "X.X%" relative to prefix total


class DottedGroup(BaseModel):
    """All dotted keys sharing the same last-dot prefix."""

    prefix_seconds:  int              # sum of all sibling dotted key seconds
    prefix_duration: str              # "[Dd ]HH:MM:SS" of prefix total
    breakdown:       list[DottedEntry]  # sorted descending by seconds, zeros omitted


class TimeDistribution(BaseModel):
    """Two-tier time distribution section of the metrics report."""

    total_seconds:  int                    # grand total wall-clock seconds
    total_duration: str                    # "[Dd ]HH:MM:SS" grand total
    top_level:      list[TopLevelEntry]    # sorted descending by seconds, zeros omitted
    dotted:         dict[str, DottedGroup] # keyed by prefix string


class PipelineMetrics(BaseModel):
    """Top-level Pydantic model serialised to ``metrics.yaml``.

    ``convergence`` is ``None`` when no encoded result data has been collected
    (e.g. all chunks were reused from a prior run).

    Note: ``parallelism`` is written separately by the pipeline orchestrator
    and is NOT part of this model.
    """

    time_distribution: TimeDistribution
    convergence:       list[ConvergenceStats] | None = None


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

    Phases use only :meth:`time` and :meth:`step`.  :meth:`flush` is
    intentionally part of the full interface but is **not** called by phases —
    it is reserved for the orchestrator.
    """

    def time(self, key: MetricKey, *parts: str) -> contextlib.AbstractContextManager[None]:
        """Return a context manager that measures wall-clock elapsed for *key*.

        Records ``time.monotonic()`` on enter; on exit accumulates elapsed
        seconds into the store and increments the flush counter.
        Exceptions are re-raised after recording so timing is never lost.
        """
        ...

    def step(
        self,
        key:                MetricKey,
        *parts:             str,
        convergence_update: ConvergenceUpdate | None = None,
    ) -> None:
        """Signal one unit of work completed within a loop.

        Carries no timing — timing is handled exclusively by :meth:`time`.
        Increments the flush counter (may trigger an incremental flush) and
        optionally updates per-strategy Welford convergence accumulators.

        Phases call this after each discrete unit of work (e.g. after each
        encoding attempt or chunk completion).  They have no knowledge of
        flushing — that is self-managed by the collector.
        """
        ...

    def flush(self) -> None:
        """Write the current metrics state to disk.

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
                    avg=round(acc.welford_mean, 1),
                    max=acc.max,
                    stddev=round(stddev, 1),
                ),
            )
        )
    return results or None


# ---------------------------------------------------------------------------
# No-op implementation
# ---------------------------------------------------------------------------


class NoOpMetricsCollector(MetricsCollector):
    """Concrete no-op implementation of :class:`MetricsCollector`.

    Inherits directly from the Protocol so the type checker verifies
    conformance at definition time.  Discards all data without performing
    any I/O.  Used in tests and ``api.py`` standalone callers.
    """

    def time(self, key: MetricKey, *parts: str) -> contextlib.AbstractContextManager[None]:
        """Return a no-op context manager (``contextlib.nullcontext``)."""
        return contextlib.nullcontext()

    def step(
        self,
        key:                MetricKey,
        *parts:             str,
        convergence_update: ConvergenceUpdate | None = None,
    ) -> None:
        """Discard all arguments — no-op."""

    def flush(self) -> None:
        """No-op — nothing to flush."""


# ---------------------------------------------------------------------------
# Module-level metrics computation helpers
# ---------------------------------------------------------------------------


def _compute_top_level_entries(store: MetricsStore) -> tuple[int, list[TopLevelEntry]]:
    """Build sorted top-level entries and grand total from *store*.

    Filters keys where :func:`_is_top_level` is ``True``, computes the grand
    total (sum of all top-level values), formats percentages as ``"X.X%"``
    (one decimal), handles zero grand total → ``"0.0%"``, sorts descending by
    seconds, and omits zero-second entries.

    Returns:
        A ``(grand_total_seconds, entries)`` tuple where *grand_total_seconds*
        is the integer-rounded sum of all top-level values.
    """
    top_level_raw = {k: v for k, v in store.items() if _is_top_level(k)}
    grand_total   = int(round(sum(top_level_raw.values()))) if top_level_raw else 0
    entries: list[TopLevelEntry] = []
    for key, val in top_level_raw.items():
        secs = int(round(val))
        if secs == 0:
            continue
        percent = f"{secs / grand_total * 100:.1f}%" if grand_total > 0 else "0.0%"
        entries.append(TopLevelEntry(
            key=key,
            seconds=secs,
            duration=_format_duration(secs),
            percent=percent,
        ))
    entries.sort(key=lambda e: e.seconds, reverse=True)
    return grand_total, entries


def _compute_dotted_groups(store: MetricsStore) -> dict[str, DottedGroup]:
    """Build prefix-keyed dotted groups from *store*.

    Filters keys where :func:`_is_top_level` is ``False`` (dotted keys),
    groups them by :func:`_last_dot_prefix`, computes prefix totals, formats
    percentages relative to the prefix total, sorts breakdowns descending by
    seconds, omits zero-second entries, and omits prefix groups where all
    values are zero.

    Returns:
        A ``dict`` keyed by prefix string, each value a :class:`DottedGroup`.
    """
    dotted_raw = {k: v for k, v in store.items() if not _is_top_level(k)}
    prefix_groups: dict[str, dict[str, float]] = {}
    for key, val in dotted_raw.items():
        prefix = _last_dot_prefix(key)
        prefix_groups.setdefault(prefix, {})[key] = val

    result: dict[str, DottedGroup] = {}
    for prefix, siblings in prefix_groups.items():
        prefix_total = int(round(sum(siblings.values())))
        if prefix_total == 0:
            continue  # omit prefix groups where all values are zero
        breakdown: list[DottedEntry] = []
        for key, val in siblings.items():
            secs = int(round(val))
            if secs == 0:
                continue
            percent = f"{secs / prefix_total * 100:.1f}%" if prefix_total > 0 else "0.0%"
            breakdown.append(DottedEntry(
                key=key,
                seconds=secs,
                duration=_format_duration(secs),
                percent=percent,
            ))
        breakdown.sort(key=lambda e: e.seconds, reverse=True)
        if not breakdown:
            continue
        result[prefix] = DottedGroup(
            prefix_seconds=prefix_total,
            prefix_duration=_format_duration(prefix_total),
            breakdown=breakdown,
        )
    return result


# ---------------------------------------------------------------------------
# YAML-backed implementation
# ---------------------------------------------------------------------------


class YamlMetricsCollector(MetricsCollector):
    """Concrete YAML-backed implementation of :class:`MetricsCollector`.

    Accumulates wall-clock timing and CRF convergence data throughout a
    pipeline run and persists them incrementally to ``metrics.yaml`` in the
    work directory root using the `.tmp`-then-rename atomic write protocol.

    Args:
        work_dir:   Pipeline work directory root.
        force_wipe: When ``True``, delete any existing ``metrics.yaml`` and
                    start fresh.  When ``False`` (default), load and resume
                    from persisted state.
    """

    def __init__(
        self,
        work_dir:   Path,
        force_wipe: bool = False,
    ) -> None:
        self._work_dir:     Path = work_dir
        self._metrics_path: Path = work_dir / METRICS_YAML_FILENAME
        self._tmp_path:     Path = work_dir / (METRICS_YAML_FILENAME + _TEMP_SUFFIX)

        # Internal accumulators
        self._store:             MetricsStore                       = {}
        self._conv_accumulators: dict[str, _ConvergenceAccumulator] = {}
        self._flush_counter:     int                                = 0
        self._active_timers:     list[tuple[str, float]]            = []

        metrics_file = work_dir / METRICS_YAML_FILENAME
        if force_wipe:
            try:
                metrics_file.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Metrics: could not delete existing %s: %s", metrics_file, exc)
        else:
            self._try_resume(metrics_file)

    def _try_resume(self, metrics_file: Path) -> None:
        """Load persisted state from *metrics_file* and restore accumulators.

        On any failure, logs a WARNING and leaves accumulators at their
        zero-initialised defaults (start fresh).
        """
        if not metrics_file.exists():
            return
        try:
            raw = yaml.safe_load(metrics_file.read_text(encoding="utf-8"))
            pm  = PipelineMetrics.model_validate(raw["pipeline_metrics"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Metrics: failed to load %s, starting fresh: %s", metrics_file, exc)
            return

        # Restore top-level time accumulators
        for entry in pm.time_distribution.top_level:
            try:
                self._store[entry.key] = float(entry.seconds)
            except (KeyError, ValueError):
                logger.debug("Metrics: unknown key %r in persisted file, skipping", entry.key)

        # Restore dotted time accumulators
        for _prefix, group in pm.time_distribution.dotted.items():
            for entry in group.breakdown:
                try:
                    self._store[entry.key] = float(entry.seconds)
                except (KeyError, ValueError):
                    logger.debug("Metrics: unknown key %r in persisted file, skipping", entry.key)

        # Restore convergence accumulators (resume Welford from stddev² * n)
        if pm.convergence is not None:
            for cs in pm.convergence:
                n      = cs.chunks
                stddev = cs.attempts.stddev
                acc    = _ConvergenceAccumulator(
                    n=n,
                    total=cs.attempts.total,
                    min=cs.attempts.min,
                    max=cs.attempts.max,
                    welford_mean=cs.attempts.avg,
                    welford_M2=stddev ** 2 * n,
                )
                self._conv_accumulators[cs.strategy] = acc

        logger.debug("Metrics: resumed from %s", metrics_file)

    # ------------------------------------------------------------------
    # MetricsCollector interface
    # ------------------------------------------------------------------

    def time(self, key: MetricKey, *parts: str) -> contextlib.AbstractContextManager[None]:
        """Return a context manager that accumulates elapsed seconds for *key*.

        Records ``time.monotonic()`` on enter; on exit accumulates elapsed into
        ``_store[resolved_key]``, increments the flush counter, and triggers an
        incremental flush if needed.  Exceptions are re-raised after recording
        elapsed time so timing is never lost.
        """
        return self._TimingContext(self, _build_key(key, *parts))

    class _TimingContext:
        """Inner context manager used by :meth:`YamlMetricsCollector.time`."""

        __slots__ = ("_collector", "_key", "_t0")

        def __init__(self, collector: YamlMetricsCollector, resolved_key: str) -> None:
            self._collector = collector
            self._key       = resolved_key
            self._t0:  float = 0.0

        def __enter__(self) -> None:
            self._t0 = _time.monotonic()
            self._collector._active_timers.append((self._key, self._t0))

        def __exit__(
            self,
            exc_type:  type[BaseException] | None,
            exc_val:   BaseException | None,
            exc_tb:    object,
        ) -> None:
            elapsed = _time.monotonic() - self._t0
            # Remove this timer from active list (remove first matching entry)
            try:
                self._collector._active_timers.remove((self._key, self._t0))
            except ValueError:
                pass  # already removed (shouldn't happen, but be defensive)
            self._collector._store[self._key] = (
                self._collector._store.get(self._key, 0.0) + elapsed
            )
            self._collector._flush_counter += 1
            if self._collector._flush_counter >= FLUSH_INTERVAL:
                self._collector.flush()
                self._collector._flush_counter = 0
            # Always re-raise — we never suppress exceptions

        async def __aenter__(self) -> None:
            self.__enter__()

        async def __aexit__(
            self,
            exc_type:  type[BaseException] | None,
            exc_val:   BaseException | None,
            exc_tb:    object,
        ) -> None:
            self.__exit__(exc_type, exc_val, exc_tb)

    def step(
        self,
        key:                MetricKey,
        *parts:             str,
        convergence_update: ConvergenceUpdate | None = None,
    ) -> None:
        """Signal one unit of work completed within a loop.

        Carries no timing — timing is handled exclusively by :meth:`time`.
        Increments the flush counter (may trigger an incremental flush) and
        optionally updates per-strategy Welford convergence accumulators.
        """
        if convergence_update is not None:
            strategy = convergence_update.strategy
            if strategy not in self._conv_accumulators:
                self._conv_accumulators[strategy] = _ConvergenceAccumulator()
            _update_accumulator(self._conv_accumulators[strategy], convergence_update.attempt_count)

        self._flush_counter += 1
        if self._flush_counter >= FLUSH_INTERVAL:
            self.flush()
            self._flush_counter = 0

    def _snapshot_active_timers(self) -> dict[str, float]:
        """Return partial elapsed seconds for all currently in-flight ``time()`` contexts.

        Does not modify ``_active_timers`` or ``_store`` — the timers are
        still running.  Called by both ``_flush_incremental()`` and ``flush()``
        so that a forced exit captures partial elapsed rather than losing it.
        """
        now = _time.monotonic()
        partial: dict[str, float] = {}
        for key, t0 in self._active_timers:
            partial[key] = partial.get(key, 0.0) + (now - t0)
        return partial

    def _build_metrics(self) -> PipelineMetrics:
        """Assemble a :class:`PipelineMetrics` from current accumulator state.

        Merges partial elapsed from any in-flight ``time()`` contexts so that
        a forced flush captures work-in-progress timing.  Zero-second entries
        are omitted from both the top-level list and dotted breakdowns.
        """
        active_partial = self._snapshot_active_timers()
        effective_store: MetricsStore = {
            k: self._store.get(k, 0.0) + active_partial.get(k, 0.0)
            for k in set(self._store) | set(active_partial)
        }

        grand_total, top_level_entries = _compute_top_level_entries(effective_store)
        dotted_groups                  = _compute_dotted_groups(effective_store)

        time_dist = TimeDistribution(
            total_seconds=grand_total,
            total_duration=_format_duration(grand_total),
            top_level=top_level_entries,
            dotted=dotted_groups,
        )

        convergence_stats = _compute_convergence(self._conv_accumulators)

        return PipelineMetrics(
            time_distribution=time_dist,
            convergence=convergence_stats,
        )

    def _write_atomic(self, metrics: PipelineMetrics) -> None:
        """Serialize *metrics* to YAML and write atomically via .tmp-then-rename."""
        data = {"pipeline_metrics": metrics.model_dump()}
        text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        try:
            self._tmp_path.write_text(text, encoding="utf-8")
            self._tmp_path.replace(self._metrics_path)
        except OSError as exc:
            logger.warning("Metrics: failed to write %s: %s", self._metrics_path, exc)

    def flush(self) -> None:
        """Write the current metrics state to disk atomically.

        Captures partial elapsed from any in-flight ``time()`` contexts so
        that a forced exit (SIGINT, unhandled exception) does not lose
        work-in-progress timing.  On write failure, logs a WARNING and does
        not raise.
        """
        self._write_atomic(self._build_metrics())
