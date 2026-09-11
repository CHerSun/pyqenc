"""Slim, phase-agnostic runner that drives a single target phase.

The ``Runner`` replaces the registry-iterating ``PipelineOrchestrator``. Its
responsibilities are deliberately narrow and phase-agnostic:

1. Run exactly one *target* phase via ``target.run(dry_run=...)``. Dependency
   resolution lives inside the phases, so the runner never iterates the
   registry to *drive* execution (Req 3.1, 3.2, 3.3).
2. Own the run-scoped metrics collector's final flush lifecycle (Req 7.4).
3. Build a uniform run summary from each phase's cached ``PhaseResult.outcome``,
   using only the common ``PhaseResult`` surface (Req 4).
4. Compute the single ``deep_cleanup`` decision once and broadcast
   ``finalize(ctx)`` to every phase on a successful, non-dry-run execution
   (Req 5, 6).

The runner knows only the ``Phase`` / ``PhaseResult`` protocol surface,
``CleanupLevel``, ``PhaseOutcome``, and the registry ``dict``. It has no
phase-specific knowledge and never names a phase-specific artifact path.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pyqenc.constants import THICK_LINE
from pyqenc.metrics import METRICS_YAML_FILENAME, MetricsCollector
from pyqenc.models import CleanupLevel, PhaseOutcome
from pyqenc.phase import FinalizeContext, Phase, PhaseResult
from pyqenc.utils.long_path import LongPath

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunResult — uniform public result type consumed by api.py and the CLI
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Uniform result of a single ``Runner`` invocation (replaces ``PipelineResult``).

    Built solely from the common ``PhaseResult`` surface of every phase in the
    registry, so it is identical in shape for every command (``auto`` and the
    partial subcommands alike).

    Attributes:
        success:             ``True`` when the target phase completed or reused
                             (its ``PhaseResult.is_complete`` is ``True``).
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
        output_files:        Final output file paths, taken from the *target*
                             phase's result only (artifacts under a ``final/``
                             directory).
        error:               Failure description when ``success`` is ``False``;
                             ``None`` otherwise.
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
    """Slim, phase-agnostic driver that runs one target phase and owns run-level concerns.

    The registry and collector are constructed by the caller (``api.py``)
    before being passed here. The runner runs only the target phase, then
    iterates the registry read-only to build the summary and broadcast
    ``finalize``.

    Args:
        registry:         Ordered phase registry produced by ``_build_registry``.
        target:           The terminal phase *class* to run (its instance is
                          looked up in ``registry``).
        collector:        Run-scoped metrics collector, owned for this one run.
        work_dir:         Work directory (used to log the metrics path).
        cleanup:          Requested cleanup level for this run.
        no_metrics:       When ``True``, skip the final ``metrics.yaml`` flush.
        is_terminal_most: ``True`` when ``target`` is the terminal-most phase
                          (``MergePhase``); only such a run is eligible for
                          ``ALL`` deep cleanup.
    """

    def __init__(
        self,
        registry:          dict[type[Phase], Phase],
        target:            type[Phase],
        collector:         MetricsCollector,
        *,
        work_dir:          Path,
        cleanup:           CleanupLevel,
        no_metrics:        bool,
        is_terminal_most:  bool,
    ) -> None:
        self._registry:         dict[type[Phase], Phase] = registry
        self._target:           type[Phase]              = target
        self._collector:        MetricsCollector         = collector
        self._work_dir:         LongPath                 = LongPath(work_dir)
        self._cleanup:          CleanupLevel             = cleanup
        self._no_metrics:       bool                     = no_metrics
        self._is_terminal_most: bool                     = is_terminal_most

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, dry_run: bool = False) -> RunResult:
        """Run the target phase and produce a uniform run summary.

        Runs only the target phase (dependency resolution happens inside the
        phases). A ``PENDING`` outcome from an execute run is treated as a
        failure-to-progress. On a successful, non-dry-run execution the metrics
        collector is flushed and ``finalize`` is broadcast to every phase with
        the pre-resolved ``deep_cleanup`` decision.

        Args:
            dry_run: When ``True``, phases report what work would be done
                     without executing it; no metrics are written and no
                     ``finalize`` is broadcast.

        Returns:
            ``RunResult`` summarising the run.
        """
        target = self._registry[self._target]
        logger.debug("Runner: driving target phase '%s' (dry_run=%s)", target.name, dry_run)

        result: PhaseResult = target.run(dry_run=dry_run)

        # PENDING must never survive an execute run — treat as failure-to-progress.
        if not dry_run and result.outcome == PhaseOutcome.PENDING:
            logger.debug(
                "Runner: target '%s' returned PENDING on an execute run; treating as failure.",
                target.name,
            )
            result = _as_failed(result, "phase did not progress to completion")

        # Whether the run succeeded at its PURPOSE. On an execute run that means
        # the target completed (is_complete). On a dry-run the purpose is a
        # preview: a PENDING target ("work remains here") is the normal,
        # successful preview outcome — only a genuine FAILED makes a dry-run
        # unsuccessful. (PENDING never reaches here on execute; it was converted
        # to FAILED above.)
        if dry_run:
            run_ok = result.outcome != PhaseOutcome.FAILED
        else:
            run_ok = result.is_complete  # PENDING and FAILED are both not-complete.

        summary = self._build_summary(run_ok=run_ok, target_result=result)

        # Final metrics flush on an execute run (Req 7.4); never on dry-run (Req 7.6).
        if not dry_run and not self._no_metrics:
            self._collector.flush()
            logger.info("Metrics written to: %s", self._work_dir / METRICS_YAML_FILENAME)

        # Always unregister the collector from the interrupt-flush registry — on
        # every path (success, failure, dry-run) — now that this run is done
        # (Req 7.7). close() only unregisters; it never writes.
        self._collector.close()

        # Downgrade-and-warn for ALL on a non-terminal command (Req 6.3, 6.4).
        if self._cleanup >= CleanupLevel.ALL and not self._is_terminal_most:
            logger.warning(
                "Full cleanup (--cleanup all) requested but '%s' is not the terminal "
                "command; downgraded to intermediate cleanup.",
                target.name,
            )

        # Single deep_cleanup decision, computed once (Req 6.1).
        deep = (
            run_ok
            and not dry_run
            and self._is_terminal_most
            and self._cleanup >= CleanupLevel.ALL
        )

        # Broadcast finalize only on a successful execute run (Req 5, 6.5, 6.6).
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
        phase-specific internal (Req 4.1, 4.3). Each outcome is bucketed across
        all four ``PhaseOutcome`` values with none silently dropped (Req 4.2).
        Phases with no cached result (e.g. never reached because an earlier
        dependency failed) are simply absent from ``outcomes``.

        Args:
            run_ok:        Whether the target phase completed successfully.
            target_result: The target phase's result (its ``error``/``message``
                           populates ``RunResult.error`` on failure).

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

        # Final output paths come from the TARGET phase's result only (Req 4.5).
        output_files = _collect_output_files(target_result)

        error: str | None = None
        if not run_ok:
            error = (
                target_result.error
                or target_result.message
                or "run did not complete successfully"
            )

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
        the producing phase, not this uniform summary (Req 4.5).

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

def _collect_output_files(result: PhaseResult) -> list[Path]:
    """Return the final output paths from a single phase result.

    A phase (``MergePhase``) stores its artifacts in ``result.artifacts``; each
    ``COMPLETE`` artifact whose path lives inside a ``final/`` directory is a
    pipeline output file. Reads the given (target) phase's result only (Req 4.5).

    Args:
        result: The target phase's result.

    Returns:
        The list of final output file paths (empty when none qualify).
    """
    return [artifact.path for artifact in result.complete if "final" in artifact.path.parts]


def _as_failed(result: PhaseResult, message: str) -> PhaseResult:
    """Return a ``FAILED`` copy of ``result`` carrying ``message`` as the error.

    Used when a phase returns ``PENDING`` on an execute run — a
    failure-to-progress the runner surfaces as an explicit failure (Req 10.6).
    The original artifacts are preserved so the summary still reflects on-disk
    state.

    Args:
        result:  The phase result to convert.
        message: The failure description.

    Returns:
        A new ``PhaseResult`` with ``outcome=FAILED`` and ``error=message``.
    """
    return PhaseResult(
        outcome   = PhaseOutcome.FAILED,
        artifacts = result.artifacts,
        message   = message,
        error     = message,
    )
