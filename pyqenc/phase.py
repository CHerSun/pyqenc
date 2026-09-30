"""Core phase protocol, result types, and shared value objects for the pyqenc pipeline.

This module defines the structural backbone of the phase object model:

- ``ArtifactState``   — re-exported from ``state`` for convenience.
- ``Artifact``        — the generic artifact wrapper: a typed payload plus
                        its recovery/selection facts (the only artifact class).
- ``PhaseOutcome``    — re-exported from ``models`` for convenience.
- ``PhaseResult``     — result returned by every phase's ``run()``.
- ``FinalizeContext`` — pre-resolved end-of-run decisions passed to ``finalize``.
- ``Recovery``        — single source of truth produced by ``Phase._recover()``.
- ``RecoveryError``   — fatal recover-time invalidation signal.
- ``Phase``           — template-method base class every phase inherits;
                        its concrete ``run()`` owns the uniform footprint.
- ``PhaseRegistry``   — type alias for the registry ``dict`` (phase class →
                        its instance).
- ``CleanupLevel``    — re-exported from ``models`` for convenience.
- ``Strategy``        — re-exported from ``models`` for convenience.
- ``_build_registry`` — factory that constructs all phase objects in execution
                        order, wires their dependencies, and returns the registry.

Every phase inherits ``Phase``, whose single concrete ``run()`` owns the
uniform footprint — memoization guard, skip check, dependency resolution,
banner, timed recovery, recovery summary, dry-run / no-pending branches, and
timed execution — so no phase can forget a step or drift from the contract.
Status decisions stay with the concrete phases: ``_recover()`` declares what
work is pending, and ``_execute()`` returns the outcome the phase chooses.
A separate ``finalize(ctx)`` hook lets each phase perform end-of-run
housekeeping — today, deleting its own artifacts when the runner's
pre-resolved ``FinalizeContext.deep_cleanup`` flag is set.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pyqenc.metrics import MetricKey
from pyqenc.models import CleanupLevel, CropParams, PhaseOutcome, Strategy
from pyqenc.state import ArtifactState
from pyqenc.utils.log_format import emit_phase_banner, log_recovery_line
from pyqenc.utils.long_path import LongPath

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

__all__ = [
    "Artifact",
    "ArtifactState",
    "CleanupLevel",
    "FinalizeContext",
    "Phase",
    "PhaseContractError",
    "PhaseOutcome",
    "PhaseRegistry",
    "PhaseResult",
    "Recovery",
    "RecoveryError",
    "Strategy",
    "_build_registry",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy — re-exported from models.py for convenience.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Artifact — the generic wrapper (the only artifact class)
# ---------------------------------------------------------------------------

@dataclass
class Artifact[PayloadT]:
    """One investable entity (the payload) plus its recovery/selection facts.

    An artifact is the thing we act on and invest in — the unit of recovery
    (presence-based, resumable), of selection, and of inter-phase transfer.
    Identity and metadata live on the typed payload (a stream-model entity);
    the wrapper adds only the two recovery axes. No subclass of this class
    exists; every ledger row and result field is a direct ``Artifact[...]``
    instantiation with a concrete payload type.

    There is no ``path`` field: file-backed locations derive from the payload;
    virtual payloads have none. The wrapper is mutable and never persisted —
    the owning phase flips ``state`` to ``COMPLETE`` as ``_execute()``
    verifies each production; sidecars persist payload info slices only.

    Type parameter:
        PayloadT: The stream-model entity this artifact wraps.

    Attributes:
        payload: The typed entity this row is about.
        state:   Completeness of this artifact (presence-based).
        wanted:  Whether this artifact is selected by the current run. This is a
                 DERIVED value: it comes from external input — the user's stream
                 filter plus the pipeline mode (e.g. ``video_required``) for
                 extraction, and scene detection for chunking — and is never
                 chosen or mutated by a phase on its own during recovery.
                 Orthogonal to completeness. ``True`` = must be produced if not
                 already ``COMPLETE``. ``False`` = present or expected on disk
                 but not needed this run; it is retained in place unchanged and
                 is not a deletion candidate — deletion only ever happens when
                 the user explicitly sets a cleanup level, applied uniformly.
    """

    payload: PayloadT
    state:   ArtifactState
    wanted:  bool = True


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------

@dataclass
class PhaseResult:
    """Result returned by a phase's ``run()`` method.

    ``artifacts`` is NOT storage: it is the derived, read-only concatenation
    of the subclass's declared artifact fields (in declaration order) — the
    external contract. Internal ledger rows (``wanted=False``, internal
    artifacts) have no path into a result; phases place wanted rows into
    their declared fields in ``_make_result``.

    Attributes:
        outcome: The phase outcome.
        message: The single human-readable string. On ``FAILED`` this IS the
                 failure description; partial-failure detail (count plus
                 identifiers) folds into it.
    """

    outcome: PhaseOutcome
    message: str

    @property
    def artifacts(self) -> list[Artifact[object]]:
        """Derived concatenation of the declared artifact fields.

        Dataclass-fields introspection over the concrete result class,
        ``Artifact``-typed fields only, in declaration order — the declared
        fields are the contract. Plain settings/run-parameter
        fields never contribute. Field names come from the dataclass field
        list itself, so this is the one sanctioned dynamic access in the
        codebase.
        """
        rows: list[Artifact[object]] = []
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Artifact):
                rows.append(value)
            elif isinstance(value, list):
                rows.extend(v for v in value if isinstance(v, Artifact))
        return rows

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
# Recovery — single source of truth from _recover()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recovery:
    """What ``Phase._recover()`` hands to the template ``run()``.

    The single source of truth for the phase's view of the world after
    scanning disk: the full internal artifact list and whether any work
    remains this run.

    Attributes:
        artifacts: The full internal artifact ledger — wanted AND unwanted
                   rows. Every phase emits a real ledger (job's source File,
                   probe's extended stream, chunking's windows included); the
                   only empty-ledger case is a phase whose row set is
                   unknowable before its work runs (chunking before scene
                   detection), which signals ``pending`` directly.
        pending:   Whether any work remains this run (any wanted artifact
                   ``ABSENT`` / ``PARTIAL``). The template maps it
                   mechanically: ``pending and dry_run`` → ``PENDING``,
                   ``not pending`` → ``REUSED``, ``pending`` → execute.
    """

    artifacts: list[Artifact] = field(default_factory=list)
    pending:   bool           = False

    @classmethod
    def from_artifacts(cls, artifacts: list[Artifact]) -> Recovery:
        """Derive ``pending`` from a full internal artifact list.

        Args:
            artifacts: The internal list including ``wanted=False`` entries.

        Returns:
            A ``Recovery`` whose ``pending`` is True when any wanted artifact
            is ``ABSENT`` or ``PARTIAL``.
        """
        pending = any(
            a.wanted and a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)
            for a in artifacts
        )
        return cls(artifacts=artifacts, pending=pending)


class RecoveryError(Exception):
    """Fatal recover-time invalidation raised by ``Phase._recover()``.

    Raised when persisted state contradicts the current run's inputs so
    fundamentally that the phase cannot proceed — e.g. a chunking-mode change
    or an encoding probe change without ``--force``, or a job source mismatch.
    This is the *invalidation* axis (persisted settings may be read and
    compared), distinct from completeness (presence-based only). The template
    catches it and converts it to the phase's typed ``FAILED`` result.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PhaseContractError(RuntimeError):
    """A phase violated the Phase run contract — a programming error.

    Raised by the runner when a phase's ``_execute()`` returned ``PENDING`` on
    an execute run: the template's dry-run branch is the only legitimate
    ``PENDING`` producer, so a surviving ``PENDING`` means a phase hook broke
    its contract. Loud by design (traceback) — a bug to fix, not a runtime
    condition to handle.
    """


# ---------------------------------------------------------------------------
# Phase — template-method base implementing the uniform run()
# ---------------------------------------------------------------------------

class Phase[ResultT: PhaseResult]:
    """Template-method base class implementing the uniform phase ``run()``.

    Type parameter:
        ResultT: The phase's typed result class — declared by subclassing
                 (``class JobPhase(Phase[JobPhaseResult])``), which links the
                 class to its result type through inheritance: ``_dep_result``
                 and ``.result`` then carry the concrete type with no casts,
                 overloads, or base-module imports (no circular dependencies).

    The single concrete ``run()`` owns the run footprint shared by every
    phase, in exact order: memoization guard → ``_skip_check()`` → dependency
    resolution → banner → timed ``_recover()`` → dry-run / no-pending
    branches → timed ``_execute(wanted, dry_run)``. The template never
    decides ``COMPLETED`` / ``FAILED`` and never computes per-phase payloads —
    those live in the phase hooks; timing, banner, guards, and the
    wanted-filter exist exactly once, here.

    Class attributes:
        name:              Human-readable phase name (logs, banners, summary).
        DEPENDS_ON:        Static, read-only declaration of the phase TYPES
                           this phase requires. The set is fixed per phase and
                           never mutated; the concrete instances are fetched
                           from the registry when ``run()`` resolves
                           dependencies (the registry is populated
                           incrementally, so construction time is too early).
        BANNER:            Whether ``run()`` emits the thick-line banner.
                           Phases whose work is not user-significant (job
                           setup, probe) set ``False`` and log concise INFO
                           substitutes instead.
        _METRIC_KEY:       Top-level metric key timing this phase's execution.
        _DRY_RUN_READONLY: When ``True``, a dry-run still performs the
                           phase's read-only work (only writes are skipped)
                           instead of returning a preview — the phase falls
                           through to ``_execute(dry_run=True)``. Job only.
    """

    name:              str       = "phase"
    DEPENDS_ON: ClassVar[tuple[type[Phase], ...]] = ()
    BANNER:            bool      = True
    _METRIC_KEY:       MetricKey = MetricKey.RECOVERY  # overridden per phase
    _DRY_RUN_READONLY: bool      = False

    def __init__(
        self,
        config:  AppConfig,
        phases:  PhaseRegistry | None = None,
        *,
        collector: MetricsCollector,
    ) -> None:
        """Store the shared constructor state and the registry link.

        The registry is NOT read here: it is populated incrementally
        (``_build_registry`` constructs phases one by one), so dependency
        instances are fetched from it when ``run()`` resolves dependencies.

        Args:
            config:    Full validated application configuration.
            phases:    Phase registry link. Must contain an instance of every
                       type in ``DEPENDS_ON`` by the time ``run()`` is called;
                       a declared dependency still missing then raises
                       ``TypeError`` (mis-wired registry — a programming
                       error, never silently dropped). ``None`` is legal only
                       for phases with no dependencies.
            collector: Metrics collector; the template owns all timing calls.
        """
        self._config:    AppConfig        = config
        self._collector: MetricsCollector = collector
        self._phases:    PhaseRegistry = phases if phases is not None else {}
        self.result:     ResultT | None  = None

    # ------------------------------------------------------------------
    # Dependency resolution — DEPENDS_ON is the declaration, the registry
    # link is the source of truth, fetched fresh at run time.
    # ------------------------------------------------------------------

    def _dep_result[R: PhaseResult](self, dep_cls: type[Phase[R]]) -> R:
        """Return the dependency's cached typed result — the dependency accessor.

        Fetches the ``dep_cls`` instance from the registry fresh on every call
        (the registry is the single source of truth, and it may have been
        populated after this phase's construction). The shared dependency walk
        guarantees every declared dependency has run (and cached its result)
        before this phase's hooks execute, so consumers read dependency facts
        through this typed getter instead of re-narrowing ``Phase.result`` at
        every call site. The declared ``Phase[R]`` parametrization is what
        recovers the concrete result type from the phase class at each call
        site.

        Args:
            dep_cls: The dependency's phase class.

        Returns:
            The dependency's typed result.

        Raises:
            TypeError: When the declared dependency is missing from the
                registry (mis-wired registry — a programming error).
            AssertionError: When the dependency has no cached result — a
                phase hook ran before the dependency walk (a programming
                error, never a runtime condition to handle).
        """
        instance = self._phases.get(dep_cls)
        if instance is None:
            raise TypeError(
                f"{type(self).__name__} requires {dep_cls.__name__} "
                "in the phase registry (declared in DEPENDS_ON)"
            )
        result = instance.result
        assert result is not None, (
            f"{dep_cls.__name__}.result guaranteed by the dependency walk"
        )
        return result

    # ------------------------------------------------------------------
    # Public Phase interface — the template run() and default finalize
    # ------------------------------------------------------------------

    @property
    def _logger(self) -> logging.Logger:
        """Logger of the concrete phase's module (keeps log provenance)."""
        return logging.getLogger(type(self).__module__)

    def run(self, dry_run: bool = False) -> PhaseResult:
        """Run the uniform footprint once; the concrete hooks do the work.

        Args:
            dry_run: When ``True``, report what work would be done without
                     executing it (returns ``PENDING`` when any wanted work
                     remains), unless the phase is ``_DRY_RUN_READONLY``.

        Returns:
            The phase's typed ``PhaseResult``, cached on ``self.result`` and
            returned verbatim on subsequent calls.
        """
        # 1. In-run memoization guard: return cached result verbatim.
        if self.result is not None:
            return self.result

        # 2. Phase-specific skip (config-only decision; no banner).
        skip = self._skip_check(dry_run)
        if skip is not None:
            self.result = skip
            return self.result

        # 3. Dependencies: FAILED deps chain FAILED (one ERROR line), PENDING
        #    deps (dry-run only) chain PENDING (one INFO line). No banner.
        dep_result = self._ensure_dependencies(dry_run=dry_run)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        # 4./5. Banner (once, after deps) and key-parameter logging.
        if self.BANNER:
            emit_phase_banner(self.name.upper(), self._logger)
        self._log_key_params()

        # 6. Timed recovery — the single source of truth.
        try:
            with self._collector.time(MetricKey.RECOVERY):
                recovery = self._recover()
        except RecoveryError as exc:
            # Fatal recover-time invalidation — a hard stop (same severity the
            # phases already used for mode/probe/source mismatches).
            self._logger.critical(exc.message)
            self.result = self._make_result(PhaseOutcome.FAILED, [], exc.message)
            return self.result

        # 7. Recovery summary over the unfiltered internal list; wanted
        #    artifacts are selected exactly once, here.
        wanted = [a for a in recovery.artifacts if a.wanted]
        message = (
            log_recovery_line(self._logger, recovery.artifacts, unit=self._recovery_unit())
            if recovery.artifacts
            else ""
        )

        # 8. Dry-run preview (readonly-execute phases fall through to work).
        if dry_run and not self._DRY_RUN_READONLY:
            if recovery.pending:
                self.result = self._make_result(
                    PhaseOutcome.PENDING, wanted,
                    message or f"dry-run: {self.name} not yet complete",
                )
            else:
                self.result = self._reused_result(wanted, message)
            return self.result

        # 9. Nothing pending — the phase-built reused result.
        if not recovery.pending:
            self.result = self._reused_result(wanted, message)
            return self.result

        # 10. Timed execution — the phase alone decides COMPLETED / FAILED.
        with self._collector.time(self._METRIC_KEY):
            result = self._execute(wanted, dry_run=dry_run)
        self.result = result
        return result

    def finalize(self, ctx: FinalizeContext) -> None:
        """Default end-of-run housekeeping: nothing to do.

        Phases with deep-cleanup deletions override this (extraction,
        chunking, encoding); for every other phase the base no-op applies.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        return

    # ------------------------------------------------------------------
    # Shared dependency resolution (thin, uniform wrapper)
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, *, dry_run: bool) -> PhaseResult | None:
        """Resolve dependencies and build the typed short-circuit.

        First fetches every type declared in ``DEPENDS_ON`` from the registry
        link — a declared dependency missing from the registry raises
        ``TypeError`` (mis-wired registry, a programming error; the dependency
        is never silently dropped). A ``FAILED``
        dependency chains a typed ``FAILED`` result, a ``PENDING`` one
        (dry-run only) chains a typed ``PENDING`` result.

        Args:
            dry_run: Propagated unchanged to each dependency's ``run()``.

        Returns:
            A typed ``FAILED`` result if any dependency failed, a typed
            ``PENDING`` result if any dependency is legitimately pending
            (dry-run only), or ``None`` when the phase may proceed.

        Raises:
            TypeError: When a dependency declared in ``DEPENDS_ON`` is absent
                from the registry.
        """
        missing = [
            cls.__name__ for cls in self.DEPENDS_ON if cls not in self._phases
        ]
        if missing:
            raise TypeError(
                f"{type(self).__name__} requires {', '.join(missing)} "
                "in the phase registry (declared in DEPENDS_ON)"
            )
        failed:  list[str] = []
        pending: list[str] = []
        for dep_cls in self.DEPENDS_ON:
            dep = self._phases[dep_cls]
            if dep.result is None:
                dep.run(dry_run=dry_run)
            if dep.result is None or dep.result.outcome is PhaseOutcome.FAILED:
                failed.append(dep.name)
            elif dep.result.outcome is PhaseOutcome.PENDING:
                pending.append(dep.name)

        if failed:
            names = ", ".join(n.capitalize() for n in failed)
            err = f"{self.name.capitalize()} cannot run — failed dependencies: {names}"
            self._logger.error(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)
        if pending:
            names = ", ".join(n.capitalize() for n in pending)
            msg = f"{self.name.capitalize()} dry-run is impossible — work still pending at: {names}"
            self._logger.info(msg)
            return self._make_result(PhaseOutcome.PENDING, [], msg)
        return self._post_dependency_check()

    def _post_dependency_check(self) -> PhaseResult | None:
        """Phase-specific validation after the dependency walk succeeded.

        Override to refuse proceeding on a dependency whose result is
        technically complete yet unusable for this phase (e.g. merge refuses
        to merge when EncodingPhase still holds incomplete artifacts). The
        default imposes no extra checks.

        Returns:
            A typed result to short-circuit with, or ``None`` to proceed.
        """
        return None

    # ------------------------------------------------------------------
    # Hooks — concrete phases implement / override these
    # ------------------------------------------------------------------

    def _skip_check(self, dry_run: bool) -> PhaseResult | None:
        """Phase-specific skip decision made before the template resolves deps.

        Must decide from constructor state (config) only. When the skip path
        itself needs dependency state (e.g. a work_dir for bookkeeping), it
        calls ``self._ensure_dependencies`` itself — the template's step 3 is
        skipped when a skip result is returned, and dependency results are
        memoized, so this is safe. No banner is emitted on this path.

        Args:
            dry_run: The run's dry-run flag (skip bookkeeping may skip writes).

        Returns:
            A typed result to return immediately, or ``None`` to proceed.
        """
        return None

    def _log_key_params(self) -> None:
        """Log the phase's key parameters (after the banner, before recovery)."""
        return

    def _recovery_unit(self) -> str:
        """Singular noun for the recovery summary line (e.g. ``"chunk"``)."""
        return "artifact"

    def _reused_result(self, wanted: list[Artifact], message: str) -> PhaseResult:
        """Build the typed result for the nothing-pending path.

        Default: ``_make_result(REUSED, wanted, message)``. Override when the
        payload must be rebuilt from persisted state (merge replays its
        summary, probe rebuilds cached values) — never to change the outcome.

        Args:
            wanted:   The wanted artifact list (all ``COMPLETE`` here).
            message:  The recovery summary message, or ``""`` for phases
                      without artifacts.

        Returns:
            The typed ``REUSED`` result.
        """
        return self._make_result(
            PhaseOutcome.REUSED, wanted,
            message or f"all {self._recovery_unit()}s reused",
        )

    def _recover(self) -> Recovery:
        """Scan disk and build the single source of truth for this run.

        Owns force-wipe, invalidation, ``.tmp`` pre-clean, and classification.
        Completeness is presence-based only (a present file is never
        incomplete — atomic writes guarantee it); invalidation MAY read
        persisted settings and raises :class:`RecoveryError` on fatal
        mismatch. The returned artifact list must include unwanted-but-present
        entries; ``wanted`` is derived from external input, never mutated here.

        Returns:
            The :class:`Recovery` single source of truth.
        """
        raise NotImplementedError

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> PhaseResult:
        """Produce every wanted artifact; the phase alone decides the outcome.

        A pure executor: it receives the wanted list selected by recovery and
        must return ``COMPLETED`` or ``FAILED`` — never ``PENDING`` (the
        template's dry-run branch is the only PENDING producer; the runner
        treats a surviving PENDING as a contract violation and fails loudly).

        Args:
            wanted:   The wanted artifact list from recovery.
            dry_run:  True only for ``_DRY_RUN_READONLY`` phases; the executor
                      performs all read-only work and skips writes.

        Returns:
            The typed phase result with the outcome the phase chooses.
        """
        raise NotImplementedError

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact],
        message:   str,
    ) -> PhaseResult:
        """Assemble the phase's typed result (payload defaults for the phase).

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list (``PhaseResult.artifacts``).
            message:   Human-readable summary — on ``FAILED``, the error
                       description.

        Returns:
            The populated typed result.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Phase registry
# ---------------------------------------------------------------------------

PhaseRegistry = dict[type[Phase], Phase]
"""The phase registry: maps each phase **class** to its constructed instance.

Built incrementally by ``_build_registry`` and shared by reference with every
phase (fetched from at run time — see ``Phase.DEPENDS_ON``). Keys are plain
``dict`` keys, so lookup by type is direct and order follows insertion
(execution order)."""


# ---------------------------------------------------------------------------
# Phase registry factory
# ---------------------------------------------------------------------------

def _build_registry(
    config:         AppConfig,
    source:         LongPath,
    work_dir:       Path,
    force:          bool,
    cleanup:        CleanupLevel,
    no_metrics:     bool,
    collector:      MetricsCollector,
    crop_params:    CropParams | None = None,
    video_required: bool              = True,
) -> PhaseRegistry:
    """Construct all phase objects in execution order and wire their dependencies.

    ``JobPhase`` receives all volatile per-run parameters (``source``,
    ``work_dir``, ``force``, ``cleanup``, ``no_metrics``) as plain kwargs and
    stores them on ``JobPhaseResult`` so all downstream phases can read them
    via ``self._dep_result(JobPhase)``.  All other phases are constructed with
    only ``(config, registry, collector=collector)`` — they never receive
    volatile args directly.

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
        A ``PhaseRegistry`` (ordered dict) mapping each phase class to its
        constructed instance.  Iterating the values yields phases in execution
        order.
    """
    # Deferred imports to avoid circular dependencies at module load time.
    from pyqenc.phases.audio import AudioPhase
    from pyqenc.phases.extraction import ExtractionPhase
    from pyqenc.phases.job import JobPhase

    registry: PhaseRegistry = {}

    # JobPhase receives all volatile kwargs — it stores them on JobPhaseResult
    # so downstream phases can access them via _dep_result(JobPhase).
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
    # extraction when running in audio-only mode.
    registry[ExtractionPhase] = ExtractionPhase(
        config, registry,
        video_required = video_required,
        collector      = collector,
    )

    if video_required:
        # ProbePhase sits between Extraction and the remaining video phases.
        # crop_params is forwarded here (not to JobPhase) so ProbePhase owns
        # crop detection and manual overrides.
        from pyqenc.phases.chunking import ChunkingPhase  # deferred: phases import phase (registry cycle)
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
