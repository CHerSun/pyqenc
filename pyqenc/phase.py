"""Core phase protocol, result types, and shared value objects for the pyqenc pipeline.

This module defines the structural backbone of the phase object model:

- ``ArtifactState``  — re-exported from ``state`` for convenience.
- ``Artifact``       — base dataclass for all phase output artifacts.
- ``PhaseOutcome``   — re-exported from ``models`` for convenience.
- ``PhaseResult``    — result returned by every phase's ``scan()`` / ``run()``.
- ``Phase``          — ``Protocol`` that every phase class must satisfy.
- ``CleanupLevel``   — re-exported from ``models`` for convenience.
- ``Strategy``       — re-exported from ``models`` for convenience.
- ``_build_registry`` — factory that constructs all phase objects in execution
                        order, wires their dependencies, and returns the registry.
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
    "ArtifactState",
    "Artifact",
    "PhaseOutcome",
    "PhaseResult",
    "Phase",
    "CleanupLevel",
    "Strategy",
    "_build_registry",
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
        path:  Path to the primary artifact file on disk.
        state: Current classification of this artifact.
    """

    path:  Path
    state: ArtifactState


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------

@dataclass
class PhaseResult:
    """Result returned by a phase's ``scan()`` or ``run()`` method.

    Attributes:
        outcome:   High-level outcome of the phase execution.
        artifacts: All artifacts the phase is responsible for, in any state.
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

        Includes only ``ABSENT`` (must produce) and ``ARTIFACT_ONLY``
        (sidecar repair needed).  ``STALE`` artifacts are NOT included here —
        each phase handles stale artifacts in its own ``_recover()`` logic:
        re-produce if still needed under current parameters, or leave on disk
        (subject to cleanup level) if no longer needed.
        """
        return [
            a for a in self.artifacts
            if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY)
        ]

    @property
    def did_work(self) -> bool:
        """``True`` when the phase performed real work this run.

        Distinguishes ``COMPLETED`` (work done) from ``REUSED`` (all cached).
        """
        return self.outcome == PhaseOutcome.COMPLETED


# ---------------------------------------------------------------------------
# Phase Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Phase(Protocol):
    """Common interface that every pipeline phase must implement.

    The orchestrator and CLI drive phases exclusively through this protocol,
    without knowing any phase-specific internals.

    Attributes:
        name:         Human-readable phase name used in logs and banners.
        dependencies: Ordered list of phase objects this phase depends on.
        result:       Cached result from the last ``scan()`` or ``run()`` call;
                      ``None`` if neither has been called yet.
    """

    name:         str
    dependencies: list[Phase]
    result:       PhaseResult | None

    def scan(self) -> PhaseResult:
        """Enumerate and classify current artifacts without executing any work.

        Calls ``dep.scan()`` on each dependency that has no cached result,
        checks ``dep.result.is_complete``, then classifies this phase's own
        artifacts.  No files are written or deleted.

        Returns:
            ``PhaseResult`` with all artifacts classified; cached in
            ``self.result``.
        """
        ...

    def run(self, dry_run: bool = False) -> PhaseResult:
        """Recover, execute pending work, cache and return result.

        Calls ``dep.scan()`` on each dependency that has no cached result,
        checks ``dep.result.is_complete``, runs ``_recover()`` internally,
        then executes work for all pending artifacts.

        Args:
            dry_run: When ``True``, report what work would be done without
                     executing it; return ``DRY_RUN`` outcome if any work is
                     pending.

        Returns:
            ``PhaseResult`` with all artifacts ``COMPLETE`` on success, or
            ``FAILED`` / ``DRY_RUN`` otherwise; cached in ``self.result``.
        """
        ...


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
