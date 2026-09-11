"""Core phase protocol, result types, and shared value objects for the pyqenc pipeline.

This module defines the structural backbone of the phase object model:

- ``ArtifactState``   — re-exported from ``state`` for convenience.
- ``Artifact``        — base dataclass for all phase output artifacts.
- ``PhaseOutcome``    — re-exported from ``models`` for convenience.
- ``PhaseResult``     — result returned by every phase's ``run()``.
- ``FinalizeContext`` — pre-resolved end-of-run decisions passed to ``finalize``.
- ``Phase``           — ``Protocol`` that every phase class must satisfy.
- ``CleanupLevel``    — re-exported from ``models`` for convenience.
- ``Strategy``        — re-exported from ``models`` for convenience.
- ``_build_registry`` — factory that constructs all phase objects in execution
                        order, wires their dependencies, and returns the registry.

Every phase has a single execution entry point, ``run()``: it resolves
dependencies, recovers on-disk artifacts, executes any pending work, and caches
its ``PhaseResult``. A separate ``finalize(ctx)`` hook lets each phase perform
end-of-run housekeeping — today, deleting its own artifacts when the runner's
pre-resolved ``FinalizeContext.deep_cleanup`` flag is set.
"""
# CHerSun 2026

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pyqenc.models import CleanupLevel, CropParams, PhaseOutcome, Strategy
from pyqenc.state import ArtifactState

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

__all__ = [
    "Artifact",
    "ArtifactState",
    "CleanupLevel",
    "DependencyStatus",
    "FinalizeContext",
    "Phase",
    "PhaseOutcome",
    "PhaseResult",
    "Strategy",
    "_build_registry",
    "resolve_dependencies",
]


# ---------------------------------------------------------------------------
# Strategy is now defined in models.py and re-exported here for convenience.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Artifact base
# ---------------------------------------------------------------------------

@dataclass
class Artifact:
    """Base class for all phase output artifacts.

    Each phase defines a concrete subclass that adds fully typed metadata
    fields (e.g. ``ChunkArtifact`` adds ``metadata: ChunkMetadata | None``).

    Attributes:
        path:   Path to the primary artifact file on disk.
        state:  Completeness of this artifact.
        wanted: Whether this artifact is selected by the current run. This is a
                DERIVED value: it comes from external input — the user's stream
                filter plus the pipeline mode (e.g. ``video_required``) for
                extraction, and scene detection for chunking — and is never
                chosen or mutated by a phase on its own during recovery.
                Orthogonal to completeness. ``True`` = must be produced if not
                already ``COMPLETE``. ``False`` = present or expected on disk
                but not needed this run; it is retained in place unchanged and
                is not a deletion candidate — deletion only ever happens when
                the user explicitly sets a cleanup level, applied uniformly.
                The default ``True`` ensures all existing construction sites
                are unaffected.
    """

    path:   Path
    state:  ArtifactState
    wanted: bool = True


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------

@dataclass
class PhaseResult:
    """Result returned by a phase's ``run()`` method.

    Attributes:
        outcome:   High-level outcome of the phase execution.
        artifacts: Wanted artifacts only (``wanted=True``). Phases build a full
                   internal artifact list in ``_recover()`` covering both
                   wanted and unwanted entries, then filter to ``wanted=True``
                   before constructing ``PhaseResult``. ``pending`` and
                   ``complete`` derive from this list directly, so callers
                   never need to filter by ``wanted`` themselves.
        message:   Human-readable summary of the phase outcome.
        error:     Error description when ``outcome`` is ``FAILED``; ``None``
                   otherwise.
    """

    outcome:   PhaseOutcome
    artifacts: list[Artifact]
    message:   str
    error:     str | None = None

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """``True`` when all expected artifacts are ``COMPLETE``.

        Derived from ``outcome``: ``COMPLETED`` or ``REUSED`` both mean the
        phase finished successfully with all artifacts in a usable state.
        Downstream phases check this to decide whether to proceed.
        """
        return self.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED)

    @property
    def complete(self) -> list[Artifact]:
        """Artifacts whose state is ``COMPLETE``."""
        return [a for a in self.artifacts if a.state == ArtifactState.COMPLETE]

    @property
    def pending(self) -> list[Artifact]:
        """Artifacts that require active work this run.

        Includes only ``ABSENT`` (must produce) and ``PARTIAL`` (protected
        investment needing the missing component). Since ``artifacts`` already
        contains only ``wanted=True`` entries, no additional ``wanted``
        filtering is needed here. Unwanted artifacts are excluded upstream and
        never reach this property.
        """
        return [
            a for a in self.artifacts
            if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)
        ]

    @property
    def did_work(self) -> bool:
        """``True`` when the phase performed real work this run.

        Distinguishes ``COMPLETED`` (work done) from ``REUSED`` (all cached).
        """
        return self.outcome == PhaseOutcome.COMPLETED


# ---------------------------------------------------------------------------
# FinalizeContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FinalizeContext:
    """Pre-resolved end-of-run decisions computed once by the runner.

    The runner resolves every end-of-run decision a single time and passes the
    result here as a plain flag. Phases read the flag directly in
    ``finalize()`` and never re-derive it from the cleanup level plus the
    terminal indicator. This keeps the decision logic in one place (the runner)
    and lets future end-of-run concerns add fields without changing the
    ``finalize`` signature or touching any phase's decision logic.

    Attributes:
        deep_cleanup: ``True`` only when the run succeeded AND it was not a
                      dry-run AND the target was the terminal-most phase AND the
                      requested cleanup level was ``ALL``. When set, a phase
                      deletes its own consumable artifacts in ``finalize()``.
    """

    deep_cleanup: bool


# ---------------------------------------------------------------------------
# Phase Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Phase(Protocol):
    """Common interface that every pipeline phase must implement.

    The runner and CLI drive phases exclusively through this protocol, without
    knowing any phase-specific internals. Each phase has a single execution
    entry point, ``run()``; ``finalize()`` handles end-of-run housekeeping.

    Attributes:
        name:         Human-readable phase name used in logs and banners.
        dependencies: Ordered list of phase objects this phase depends on.
        result:       Cached result from the last ``run()`` call; ``None`` if
                      ``run()`` has not been called yet.
    """

    name:         str
    dependencies: list[Phase]
    result:       PhaseResult | None

    def run(self, dry_run: bool = False) -> PhaseResult:
        """Resolve dependencies, recover, execute pending work, cache and return.

        Returns any cached ``self.result`` verbatim (in-run memoization). On a
        fresh call it resolves each dependency via ``dep.run(dry_run=...)``,
        emits the phase banner, runs ``_recover()`` internally, then executes
        work for all pending artifacts. On an already-complete phase this is
        side-effect-free and returns ``REUSED``.

        Args:
            dry_run: When ``True``, report what work would be done without
                     executing it; return a ``PENDING`` outcome when any wanted
                     work remains.

        Returns:
            ``PhaseResult`` with all artifacts ``COMPLETE`` on success
            (``COMPLETED`` / ``REUSED``), or ``PENDING`` / ``FAILED``
            otherwise; cached in ``self.result``.
        """
        ...

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for this phase.

        Called by the runner after a successful, non-dry-run execution. Its
        sole current responsibility is deep cleanup: when
        ``ctx.deep_cleanup`` is ``True``, the phase deletes only its **own**
        artifacts. It never touches another phase's artifacts, and a cleanup
        failure never fails the run.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        ...


# ---------------------------------------------------------------------------
# Shared dependency resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DependencyStatus:
    """Outcome of resolving a phase's dependencies (see ``resolve_dependencies``).

    Splits non-complete dependencies into two distinct buckets so callers can
    react differently:

    - ``failed``  is a genuine error condition — the dependency reported
      ``FAILED`` or produced no result at all. The dependent phase must chain
      ``FAILED`` and log at ERROR.
    - ``pending`` means the dependency has legitimate work remaining but has not
      failed. This only ever occurs during a dry-run preview (in execute mode a
      dependency runs its work and never stays ``PENDING``). The dependent phase
      chains ``PENDING`` and logs at INFO — a dry-run cannot be previewed past a
      dependency whose work has not been performed yet.

    Both lists are in dependency order.

    Attributes:
        failed:  Names of deps whose outcome is ``FAILED`` or whose result is
                 missing.
        pending: Names of deps whose outcome is ``PENDING`` (dry-run only).
    """

    failed:  list[str]
    pending: list[str]


def resolve_dependencies(phase: Phase, *, dry_run: bool) -> DependencyStatus:
    """Resolve every dependency of ``phase`` and classify which did not complete.

    This is the single, uniform dependency walk shared by every phase (Req 2):
    for each dependency in ``phase.dependencies`` (in order), it triggers the
    dependency's ``run(dry_run=...)`` when that dependency has no cached result
    yet, then classifies the dependency's outcome. A dependency is bucketed as:

    - ``failed``  when it still has no result, or its cached outcome is
      ``FAILED`` — a genuine error.
    - ``pending`` when its cached outcome is ``PENDING`` — legitimate remaining
      work, only possible in a dry-run preview.

    A dependency that is ``is_complete`` (``COMPLETED`` / ``REUSED``) is in
    neither bucket and the phase may proceed.

    Because a dependency with an existing cached result is never re-run, the
    in-run memoization guarantee holds: each dependency resolves at most once
    per registry instance regardless of how many phases depend on it.

    Args:
        phase:   The phase whose dependencies should be resolved.
        dry_run: Propagated unchanged to each dependency's ``run()``.

    Returns:
        A ``DependencyStatus`` with the failed and pending dependency names in
        dependency order. When both lists are empty all dependencies are
        complete and the phase may proceed with its own work.
    """
    failed:  list[str] = []
    pending: list[str] = []
    for dep in phase.dependencies:
        if dep.result is None:
            dep.run(dry_run=dry_run)
        if dep.result is None or dep.result.outcome is PhaseOutcome.FAILED:
            failed.append(dep.name)
        elif dep.result.outcome is PhaseOutcome.PENDING:
            pending.append(dep.name)
    return DependencyStatus(failed=failed, pending=pending)


# ---------------------------------------------------------------------------
# Phase registry factory
# ---------------------------------------------------------------------------

def _build_registry(
    config:         AppConfig,
    source:         Path,
    work_dir:       Path,
    force:          bool,
    cleanup:        CleanupLevel,
    no_metrics:     bool,
    collector:      MetricsCollector,
    crop_params:    CropParams | None = None,
    video_required: bool              = True,
) -> dict[type[Phase], Phase]:
    """Construct all phase objects in execution order and wire their dependencies.

    ``JobPhase`` receives all volatile per-run parameters (``source``,
    ``work_dir``, ``force``, ``cleanup``, ``no_metrics``) as plain kwargs and
    stores them on ``JobPhaseResult`` so all downstream phases can read them
    via ``self._job.result.*``.  All other phases are constructed with only
    ``(config, registry, collector=collector)`` — they never receive volatile
    args directly.

    The registry is a plain ``dict`` keyed by phase *class* (not instance),
    preserving insertion order (Python 3.7+).

    When ``video_required=True`` (default, all video subcommands), execution
    order matches the full pipeline dependency graph:

    1. ``JobPhase``          — no dependencies
    2. ``ExtractionPhase``   — depends on Job
    3. ``ProbePhase``        — depends on Job, Extraction
    4. ``AudioPhase``        — depends on Job, Extraction
    5. ``ChunkingPhase``     — depends on Job, Probe
    6. ``OptimizationPhase`` — depends on Job, Probe, Chunking
    7. ``EncodingPhase``     — depends on Job, Probe, Chunking, Optimization
    8. ``MergePhase``        — depends on Job, Probe, Encoding, Audio

    When ``video_required=False`` (``audio`` subcommand), ``ProbePhase`` is
    omitted from the registry:

    1. ``JobPhase``          — no dependencies
    2. ``ExtractionPhase``   — depends on Job (video extraction skipped)
    3. ``AudioPhase``        — depends on Job, Extraction

    Args:
        config:         Full validated application configuration.
        source:         Resolved path to the source video file.
        work_dir:       Working directory for all pipeline artifacts.
        force:          When ``True``, wipe existing artifacts before running.
        cleanup:        Artifact retention policy applied after encoding.
        no_metrics:     When ``True``, skip writing ``metrics.yaml`` files.
        collector:      Metrics collector injected into every phase constructor.
        crop_params:    Optional manual crop override forwarded to ``ProbePhase``
                        (video subcommands only); ``None`` falls back to cached
                        value in ``probe.yaml``, then auto-detection.
        video_required: When ``True`` (default), insert ``ProbePhase`` and all
                        downstream video phases.  Pass ``False`` for the
                        ``audio`` subcommand to skip video processing entirely.

    Returns:
        Ordered ``dict[type[Phase], Phase]`` mapping each phase class to its
        constructed instance.  Iterating the values yields phases in execution
        order.
    """
    # Deferred imports to avoid circular dependencies at module load time.
    from pyqenc.phases.audio import AudioPhase
    from pyqenc.phases.extraction import ExtractionPhase
    from pyqenc.phases.job import JobPhase

    registry: dict[type[Phase], Phase] = {}

    # JobPhase receives all volatile kwargs — it stores them on JobPhaseResult
    # so downstream phases can access them via self._job.result.*.
    registry[JobPhase] = JobPhase(
        config, registry,
        source     = source,
        work_dir   = work_dir,
        force      = force,
        cleanup    = cleanup,
        no_metrics = no_metrics,
        collector  = collector,
    )

    # ExtractionPhase follows Job unconditionally.
    # video_required is forwarded so ExtractionPhase can skip video/timestamp
    # extraction when running in audio-only mode (Task 9).
    registry[ExtractionPhase] = ExtractionPhase(
        config, registry,
        video_required = video_required,
        collector      = collector,
    )

    if video_required:
        # ProbePhase sits between Extraction and the remaining video phases.
        # crop_params is forwarded here (not to JobPhase) so ProbePhase owns
        # crop detection and manual overrides.
        from pyqenc.phases.chunking import ChunkingPhase
        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.phases.merge import MergePhase
        from pyqenc.phases.optimization import OptimizationPhase
        from pyqenc.phases.probe import ProbePhase

        registry[ProbePhase] = ProbePhase(
            config, registry,
            crop_params = crop_params,
            collector   = collector,
        )

        for cls in [
            AudioPhase,
            ChunkingPhase,
            OptimizationPhase,
            EncodingPhase,
            MergePhase,
        ]:
            registry[cls] = cls(config, registry, collector=collector)  # type: ignore[call-arg]
    else:
        # Audio-only path: only AudioPhase is needed after Extraction.
        registry[AudioPhase] = AudioPhase(config, registry, collector=collector)

    return registry
