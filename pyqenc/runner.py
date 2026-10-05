"""Slim, phase-agnostic runner that drives the run's terminal phases.

Responsibilities:

1. Run the *terminal* phases in order via ``target.run(dry_run=...)``;
   dependency resolution lives inside the phases, so the runner never
   iterates the registry to *drive* execution. A shared dependency reached
   by several terminals runs once — later walks hit the per-instance
   memoization guard.
2. Own the run-scoped metrics collector's final flush lifecycle.
3. Build a uniform run summary from each phase's cached ``PhaseResult.outcome``,
   using only the common ``PhaseResult`` surface.
4. Compute the single ``deep_cleanup`` decision once and broadcast
   ``finalize(ctx)`` to every phase on a successful, non-dry-run execution.

The runner knows only the ``Phase`` / ``PhaseResult`` protocol surface,
``CleanupLevel``, ``PhaseOutcome``, and the registry ``dict`` — plus the one
sanctioned exception: reading the merge terminal's deliverable contract
(``Artifact[MergedVideo]`` payloads) to collect the run's output files. It
never names any other phase's internals.

Spec: .kiro/specs/2026-09-09 phase-terminal-runner/ (multi-terminal
extension: 2026-10-05 cli-intent-commands).
"""
# CHerSun 2026

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pyqenc.constants import THICK_LINE
from pyqenc.metrics import METRICS_YAML_FILENAME, MetricsCollector
from pyqenc.models import CleanupLevel, PhaseOutcome
from pyqenc.phase import (
    FinalizeContext,
    Phase,
    PhaseContractError,
    PhaseRegistry,
    PhaseResult,
)
from pyqenc.phases.merge import MergePhase, MergePhaseResult
from pyqenc.utils.long_path import LongPath

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunResult — uniform public result type consumed by api.py and the CLI
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Uniform result of a single ``Runner`` invocation.

    Built solely from the common ``PhaseResult`` surface of every phase in the
    registry, so it is identical in shape for every command (``auto`` and the
    partial subcommands alike).

    Attributes:
        success:             ``True`` when every terminal phase completed or
                             was reused (each ``PhaseResult.is_complete``).
        outcomes:            Ordered ``{phase_name: PhaseOutcome}`` collected
                             from every phase that has a cached ``result``.
        phases_executed:     Names of phases whose outcome is ``COMPLETED``
                             (performed real work this run).
        phases_reused:       Names of phases whose outcome is ``REUSED`` (all
                             artifacts already present; no work performed).
        phases_needing_work: Names of phases whose outcome is ``PENDING`` (wanted
                             work remains; in a dry-run these are the phases that
                             would need to do work).
        phases_failed:       Names of phases whose outcome is ``FAILED``.
        output_files:        Final output file paths, taken from the LAST
                             terminal's result only — a merge terminal's
                             complete outputs; empty for every other terminal.
        error:               Failure description when ``success`` is ``False``
                             (the last driven terminal's ``message`` — on
                             ``FAILED`` it IS the error description); ``None``
                             otherwise.
    """

    success:             bool
    outcomes:            dict[str, PhaseOutcome] = field(default_factory=dict)
    phases_executed:     list[str]               = field(default_factory=list)
    phases_reused:       list[str]               = field(default_factory=list)
    phases_needing_work: list[str]               = field(default_factory=list)
    phases_failed:       list[str]               = field(default_factory=list)
    output_files:        list[Path]              = field(default_factory=list)
    error:               str | None              = None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Runner:
    """Slim, phase-agnostic driver that runs the terminal phases and owns run-level concerns.

    The registry and collector are constructed by the caller (``api.py``)
    before being passed here. The runner runs the terminal phases in order
    (stopping at the first unsuccessful one), then iterates the registry
    read-only to build the summary and broadcast ``finalize``.

    Args:
        registry:         Ordered phase registry produced by ``_build_registry``.
        targets:          The terminal phase *classes* to run, in drive order
                          (their instances are looked up in ``registry``).
        collector:        Run-scoped metrics collector, owned for this one run.
        work_dir:         Work directory (used to log the metrics path).
        cleanup:          Requested cleanup level for this run.
        no_metrics:       When ``True``, skip the final ``metrics.yaml`` flush.
    """

    def __init__(
        self,
        registry:         PhaseRegistry,
        targets:          tuple[type[Phase], ...],
        collector:        MetricsCollector,
        *,
        work_dir:         Path,
        cleanup:          CleanupLevel,
        no_metrics:       bool,
    ) -> None:
        self._registry:   PhaseRegistry = registry
        self._targets:    tuple[type[Phase], ...]   = targets
        self._collector:  MetricsCollector         = collector
        self._work_dir:   LongPath                 = LongPath(work_dir)
        self._cleanup:    CleanupLevel             = cleanup
        self._no_metrics: bool                     = no_metrics
        # Derived, never hand-set: a run whose LAST terminal is MergePhase is
        # the terminal-most run, eligible for ``ALL`` deep cleanup.
        self._is_terminal_most: bool = bool(targets) and targets[-1] is MergePhase

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, dry_run: bool = False) -> RunResult:
        """Run the terminal phases in order and produce a uniform run summary.

        Terminals are driven in the given order; dependency resolution happens
        inside the phases, and a shared dependency reached again by a later
        walk simply reads its memoized result. The walk stops at the first
        unsuccessful terminal — later terminals' dependency walks would only
        cascade the failure. A ``PENDING`` outcome from an execute run is
        treated as a failure-to-progress. On a successful, non-dry-run
        execution the metrics collector is flushed and ``finalize`` is
        broadcast to every phase with the pre-resolved ``deep_cleanup``
        decision.

        Args:
            dry_run: When ``True``, phases report what work would be done
                     without executing it; no metrics are written and no
                     ``finalize`` is broadcast.

        Returns:
            ``RunResult`` summarising the run.
        """
        results: list[PhaseResult] = []
        run_ok = True
        contract_violation_name: str | None = None

        for target_cls in self._targets:
            target = self._registry[target_cls]
            logger.debug("Runner: driving target phase '%s' (dry_run=%s)", target.name, dry_run)
            result = target.run(dry_run=dry_run)
            results.append(result)

            # PENDING on an execute run is a phase-contract violation, not a
            # runtime condition: the template's dry-run branch is the only
            # legitimate PENDING producer, so a surviving PENDING means a phase
            # hook broke its contract. Metrics are still flushed (debug
            # evidence) and the collector closed (always-unregister invariant);
            # finalize is skipped and the run fails loudly below.
            if not dry_run and result.outcome is PhaseOutcome.PENDING:
                contract_violation_name = target.name
                logger.error(
                    "Internal error: phase '%s' returned PENDING on an execute run — "
                    "this is a bug in the phase implementation; please report it.",
                    target.name,
                )

            # Whether the run succeeded at its PURPOSE so far. On an execute
            # run that means each terminal completed (is_complete). On a
            # dry-run the purpose is a preview: a PENDING terminal ("work
            # remains here") is the normal, successful preview outcome — only
            # a genuine FAILED makes a dry-run unsuccessful.
            if dry_run:
                terminal_ok = result.outcome != PhaseOutcome.FAILED
            else:
                terminal_ok = result.is_complete  # PENDING and FAILED are both not-complete.
            if not terminal_ok:
                run_ok = False
                break

        # The last driven terminal's result carries the run's output/error
        # surface (for a merge-final run, the deliverable contract).
        target_result = results[-1]
        summary = self._build_summary(run_ok=run_ok, target_result=target_result)

        # Final metrics flush on an execute run; never on dry-run.
        if not dry_run and not self._no_metrics:
            self._collector.flush()
            logger.info("Metrics written to: %s", self._work_dir / METRICS_YAML_FILENAME)

        # Always unregister the collector from the interrupt-flush registry — on
        # every path (success, failure, dry-run, contract violation) — now that
        # this run is done. close() only unregisters; it never writes.
        self._collector.close()

        if contract_violation_name is not None:
            self._log_summary(summary, dry_run=dry_run)
            raise PhaseContractError(
                f"phase '{contract_violation_name}' returned PENDING on an execute run "
                "(phase contract violation — _execute must return COMPLETED or FAILED)"
            )

        # Downgrade-and-warn for ALL on a non-terminal command.
        if self._cleanup >= CleanupLevel.ALL and not self._is_terminal_most:
            logger.warning(
                "Full cleanup (--cleanup all) requested but '%s' is not the terminal "
                "command; downgraded to intermediate cleanup.",
                self._registry[self._targets[-1]].name,
            )

        # Single deep_cleanup decision, computed once.
        deep = (
            run_ok
            and not dry_run
            and self._is_terminal_most
            and self._cleanup >= CleanupLevel.ALL
        )

        # Broadcast finalize only on a successful execute run.
        if run_ok and not dry_run:
            ctx = FinalizeContext(deep_cleanup=deep)
            for phase in self._registry.values():
                phase.finalize(ctx)

        self._log_summary(summary, dry_run=dry_run)
        return summary

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_summary(self, *, run_ok: bool, target_result: PhaseResult) -> RunResult:
        """Build the uniform run summary from cached phase outcomes.

        Reads only ``phase.name`` and ``phase.result.outcome`` for every phase
        that has a cached result — the common ``PhaseResult`` surface, never a
        phase-specific internal. Each outcome is bucketed across all four
        ``PhaseOutcome`` values with none silently dropped. Phases with no cached result (e.g. never reached because an earlier
        dependency failed) are simply absent from ``outcomes``.

        Args:
            run_ok:        Whether the target phase completed successfully.
            target_result: The target phase's result (its ``message``
                           populates ``RunResult.error`` on failure — on
                           ``FAILED`` the message IS the error description).

        Returns:
            The assembled ``RunResult``.
        """
        outcomes:            dict[str, PhaseOutcome] = {}
        phases_executed:     list[str]               = []
        phases_reused:       list[str]               = []
        phases_needing_work: list[str]               = []
        phases_failed:       list[str]               = []

        for phase in self._registry.values():
            if phase.result is None:
                continue
            outcome = phase.result.outcome
            outcomes[phase.name] = outcome
            match outcome:
                case PhaseOutcome.COMPLETED:
                    phases_executed.append(phase.name)
                case PhaseOutcome.REUSED:
                    phases_reused.append(phase.name)
                case PhaseOutcome.PENDING:
                    phases_needing_work.append(phase.name)
                case PhaseOutcome.FAILED:
                    phases_failed.append(phase.name)

        # Final output paths come from the TARGET phase's result only; a merge
        # target carries the deliverable contract, every other target has none.
        output_files: list[Path] = (
            target_result.output_paths
            if isinstance(target_result, MergePhaseResult) else []
        )

        error: str | None = None
        if not run_ok:
            error = target_result.message or "run did not complete successfully"

        return RunResult(
            success             = run_ok,
            outcomes            = outcomes,
            phases_executed     = phases_executed,
            phases_reused       = phases_reused,
            phases_needing_work = phases_needing_work,
            phases_failed       = phases_failed,
            output_files        = output_files,
            error               = error,
        )

    def _log_summary(self, summary: RunResult, *, dry_run: bool) -> None:
        """Emit the uniform run summary for every command.

        Reports counts only (executed / reused / needing-work / failed); the
        reporting of specific final output *paths* stays the responsibility of
        the producing phase, not this uniform summary.

        Args:
            summary: The assembled run summary.
            dry_run: Whether this was a dry-run (changes the wording).
        """
        logger.info(THICK_LINE)
        if dry_run:
            logger.info("[DRY-RUN] Run preview completed")
            logger.info(
                "Phases complete:     %d",
                len(summary.phases_executed) + len(summary.phases_reused),
            )
            logger.info("Phases needing work: %d", len(summary.phases_needing_work))
        elif summary.success:
            logger.info("Run completed")
            logger.info("Phases executed: %d", len(summary.phases_executed))
            logger.info("Phases reused:   %d", len(summary.phases_reused))
        else:
            logger.error("Run FAILED: %s", summary.error)
            logger.error(
                "Phases executed: %d. Reused: %d. Failed: [%s].",
                len(summary.phases_executed),
                len(summary.phases_reused),
                ", ".join(summary.phases_failed) if summary.phases_failed else "none",
            )
        logger.info(THICK_LINE)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
