"""Core phase protocol, result types, and shared value objects for the pyqenc pipeline.

This module defines the structural backbone of the phase object model:

- ``ArtifactState``   — re-exported from ``state`` for convenience.
- ``Artifact``        — base dataclass for all phase output artifacts.
- ``PhaseOutcome``    — re-exported from ``models`` for convenience.
- ``PhaseResult``     — result returned by every phase's ``run()``.
- ``FinalizeContext`` — pre-resolved end-of-run decisions passed to ``finalize``.
- ``Recovery``        — single source of truth produced by ``PhaseBase._recover()``.
- ``RecoveryError``   — fatal recover-time invalidation signal.
- ``Phase``           — ``Protocol`` that every phase class must satisfy.
- ``PhaseBase``       — template-method base implementing the uniform ``run()``
                        footprint shared by every phase.
- ``CleanupLevel``    — re-exported from ``models`` for convenience.
- ``Strategy``        — re-exported from ``models`` for convenience.
- ``_build_registry`` — factory that constructs all phase objects in execution
                        order, wires their dependencies, and returns the registry.

Every phase inherits ``PhaseBase``, whose single concrete ``run()`` owns the
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, TypeVar, cast, runtime_checkable

from pyqenc.metrics import MetricKey
from pyqenc.models import CleanupLevel, CropParams, PhaseOutcome, Strategy
from pyqenc.state import ArtifactState
from pyqenc.utils.log_format import emit_phase_banner, log_recovery_line

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
    "PhaseBase",
    "PhaseContractError",
    "PhaseOutcome",
    "PhaseResult",
    "Recovery",
    "RecoveryError",
    "Strategy",
    "_build_registry",
    "resolve_dependencies",
]

logger = logging.getLogger(__name__)

TPhase = TypeVar("TPhase", bound="Phase")
"""A Phase subclass; the return type of ``PhaseBase._dep()``."""


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
# Recovery — single source of truth from _recover()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recovery:
    """What ``PhaseBase._recover()`` hands to the template ``run()``.

    The single source of truth for the phase's view of the world after
    scanning disk: the full internal artifact list and whether any work
    remains this run.

    Attributes:
        artifacts: Full internal artifact list — wanted AND unwanted entries.
                   Phases whose outputs are state sidecars rather than
                   artifacts (job, probe) return an empty list and signal
                   everything through ``pending``.
        pending:   Whether any work remains this run (any wanted artifact
                   ``ABSENT`` / ``PARTIAL``, or — for state phases — the
                   sidecar is absent or invalidated). The template maps it
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
    """Fatal recover-time invalidation raised by ``PhaseBase._recover()``.

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
    """A phase violated the PhaseBase run contract — a programming error.

    Raised by the runner when a phase's ``_execute()`` returned ``PENDING`` on
    an execute run: the template's dry-run branch is the only legitimate
    ``PENDING`` producer, so a surviving ``PENDING`` means a phase hook broke
    its contract. Loud by design (traceback) — a bug to fix, not a runtime
    condition to handle.
    """


# ---------------------------------------------------------------------------
# PhaseBase — template-method base implementing the uniform run()
# ---------------------------------------------------------------------------

class PhaseBase:
    """Template-method base class implementing the uniform phase ``run()``.

    The single concrete ``run()`` below owns the run footprint shared by every
    phase, in this exact order:

    1. In-run memoization guard — a cached ``self.result`` is returned
       verbatim: no re-resolution, no banner, no re-classification.
    2. ``_skip_check()`` — phase-specific skip (e.g. optimization disabled);
       reads constructor state (config) only, never dependency results.
    3. ``_ensure_dependencies()`` — shared dependency walk; a ``FAILED``
       dependency chains a typed ``FAILED`` result with one ERROR line, a
       ``PENDING`` one (dry-run only) chains a typed ``PENDING`` with one
       INFO line. No banner is emitted on either short-circuit.
    4. Banner — iff the ``BANNER`` class flag; exactly once, after deps.
    5. ``_log_key_params()`` — key-parameter logging.
    6. Timed ``_recover()`` under the top-level ``recovery`` metric key;
       returns the :class:`Recovery` single source of truth. A
       :class:`RecoveryError` converts to a typed ``FAILED`` result.
    7. ``log_recovery_line`` over the unfiltered internal artifact list
       (skipped when the phase has no artifacts).
    8. Dry-run branch — unless ``_DRY_RUN_READONLY``: ``PENDING`` when work
       remains, otherwise the reused result. No writes happen.
    9. No pending work — the phase-built reused result (``REUSED``).
    10. Timed ``_execute(wanted, dry_run)`` under the phase's top-level
        metric key. The concrete phase alone decides ``COMPLETED`` /
        ``FAILED``; returning ``PENDING`` from ``_execute`` violates the
        contract and the runner fails loudly on it.

    The template never decides ``COMPLETED`` / ``FAILED`` and never computes
    per-phase payloads — those live in the phase hooks. Timing, banner, guards
    and the wanted-filter exist exactly once, here.

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
        phases:  dict[type[Phase], Phase] | None = None,
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
        self._phases:    dict[type[Phase], Phase] = phases if phases is not None else {}
        self.result:     PhaseResult | None = None

    # ------------------------------------------------------------------
    # Dependency resolution — DEPENDS_ON is the declaration, the registry
    # link is the source of truth, fetched fresh at run time.
    # ------------------------------------------------------------------

    @property
    def dependencies(self) -> list[Phase]:
        """Dependency instances, fetched from the registry on every access.

        Only meaningful after ``_ensure_dependencies`` has confirmed every
        declared type is present (it raises ``TypeError`` otherwise); this
        property itself does not re-validate.
        """
        return [self._phases[cls] for cls in self.DEPENDS_ON if cls in self._phases]

    def _dep(self, dep_cls: type[TPhase]) -> TPhase:
        """Return the dependency instance of ``dep_cls`` from the registry.

        Fetches fresh on every call — the registry is the single source of
        truth, and it may have been populated after this phase's construction.

        Args:
            dep_cls: The dependency's phase class.

        Returns:
            The concrete instance from the registry.

        Raises:
            TypeError: When the declared dependency is missing from the
                registry (mis-wired registry — a programming error).
        """
        instance = self._phases.get(dep_cls)
        if instance is None:
            raise TypeError(
                f"{type(self).__name__} requires {dep_cls.__name__} "
                "in the phase registry (declared in DEPENDS_ON)"
            )
        return cast(TPhase, instance)

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
            self.result = self._make_result(
                PhaseOutcome.FAILED, [], exc.message, error=exc.message,
            )
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
        is never silently dropped). Then runs the shared walk: a ``FAILED``
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
        status = resolve_dependencies(self, dry_run=dry_run)
        if status.failed:
            names = ", ".join(n.capitalize() for n in status.failed)
            err = f"{self.name.capitalize()} cannot run — failed dependencies: {names}"
            self._logger.error(err)
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)
        if status.pending:
            names = ", ".join(n.capitalize() for n in status.pending)
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
        error:     str | None = None,
    ) -> PhaseResult:
        """Assemble the phase's typed result (payload defaults for the phase).

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list (``PhaseResult.artifacts``).
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated typed result.
        """
        raise NotImplementedError


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
