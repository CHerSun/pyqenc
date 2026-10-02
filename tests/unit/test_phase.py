"""Unit tests for the Phase template run() — observable behavior only.

A minimal stub phase exercises the template mechanics through the public
``run()``: memoization, banner emission, timed recovery, wanted-filtering,
the dry-run / no-pending branches, and the runner's hard assertion on a
PENDING-surviving execute run.

Only observable surfaces are asserted: the returned ``PhaseResult``, the
log stream (banner / recovery line), the ``metrics.yaml`` report written by
a real collector (shared helpers from ``tests.test_metrics_integration``),
and ``finalize`` being called or not. No template internals are inspected.
"""
# CHerSun 2026

import logging
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.metrics import (
    MetricKey,
    NoOpMetricsCollector,
    YamlMetricsCollector,
    _live_collectors,
)
from pyqenc.models import CleanupLevel, PhaseOutcome
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseContractError,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.runner import Runner
from pyqenc.state import ArtifactState
from tests.test_metrics_integration import _recorded_metrics, _top_level_keys

_APP_CONFIG: AppConfig = load_app_config(default_only=True)


@dataclass
class _StubResult(PhaseResult):
    """The stub's declared artifact contract: one list field."""

    rows: list[Artifact] = field(default_factory=list)


class _StubPhase(Phase):
    """Minimal concrete phase: canned recovery, recording execute, base result."""

    name = "stub"
    _METRIC_KEY = MetricKey.PROBE  # any real member; PROBE is unused by stubs

    def __init__(
        self,
        collector,
        recovery: Recovery,
        execute_result: PhaseResult | None = None,
        registry: PhaseRegistry | None = None,
        *,
        banner: bool = True,
        recover_error: RecoveryError | None = None,
    ) -> None:
        super().__init__(_APP_CONFIG, registry if registry is not None else {}, collector=collector)
        self.BANNER = banner
        self._recovery = recovery
        self._execute_result = execute_result
        self._recover_error = recover_error
        self.recover_calls = 0
        self.execute_calls = 0

    def _recover(self) -> Recovery:
        self.recover_calls += 1
        if self._recover_error is not None:
            raise self._recover_error
        return self._recovery

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> PhaseResult:
        self.execute_calls += 1
        self.executed_wanted = wanted
        self.executed_dry_run = dry_run
        if self._execute_result is not None:
            return self._execute_result
        return self._make_result(PhaseOutcome.COMPLETED, wanted, "executed")

    def _make_result(
        self,
        outcome: PhaseOutcome,
        artifacts: list[Artifact],
        message: str,
    ) -> PhaseResult:
        return _StubResult(
            outcome=PhaseOutcome(outcome), message=message, rows=list(artifacts),
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> PhaseResult:
        self.reused_built = True
        return super()._reused_result(wanted, message)


class _DepStubPhase(_StubPhase):
    """Stub declaring a dependency on another stub phase."""

    name = "depstub"
    DEPENDS_ON = (_StubPhase,)


# ---------------------------------------------------------------------------
# DEPENDS_ON — static declaration, registry fetch at run
# ---------------------------------------------------------------------------


class TestDeclaredDependencies:
    def test_registry_may_be_populated_after_construction(self) -> None:
        """The registry link is stored; instances are fetched at run time.

        The registry is populated incrementally (each phase is constructed
        before its dependents are registered), so construction must not read
        it. Populating the declared dependency AFTER the target's
        construction is enough for the run to succeed.
        """
        registry: PhaseRegistry = {}
        target = _DepStubPhase(NoOpMetricsCollector(), Recovery(pending=False), registry=registry)

        dep = _StubPhase(NoOpMetricsCollector(), Recovery(pending=False))
        dep.result = dep._make_result(PhaseOutcome.REUSED, [], "already run")
        registry[_StubPhase] = dep  # populated after target construction

        result = target.run()
        assert result.outcome is PhaseOutcome.REUSED
        assert target._dep_result(_StubPhase) is dep.result

    def test_missing_declared_dependency_raises_loudly(self) -> None:
        """A declared dependency absent from the registry is never dropped."""
        registry: PhaseRegistry = {}
        target = _DepStubPhase(NoOpMetricsCollector(), Recovery(pending=False), registry=registry)
        # registry stays empty — the declared dep is absent

        with pytest.raises(AssertionError, match="requires _StubPhase"):
            target.run()


def _spy_collector() -> MagicMock:
    from pyqenc.metrics import MetricsCollector

    collector = MagicMock(spec=MetricsCollector)
    collector.time.return_value = __import__("contextlib").nullcontext()
    return collector


def _art(state: ArtifactState, *, wanted: bool = True) -> Artifact:
    return Artifact(payload=Path(f"{state.value}_{wanted}.mkv"), state=state, wanted=wanted)


# ---------------------------------------------------------------------------
# Memoization guard
# ---------------------------------------------------------------------------


class TestMemoization:
    def test_second_run_returns_cached_result_without_rerunning_recovery(
        self, tmp_path: Path
    ) -> None:
        phase = _StubPhase(NoOpMetricsCollector(), Recovery(pending=True))
        first = phase.run()
        assert first is phase.result

        second = phase.run()
        assert second is first
        assert phase.recover_calls == 1
        assert phase.execute_calls == 1


# ---------------------------------------------------------------------------
# Banner emission
# ---------------------------------------------------------------------------


class TestBanner:
    def test_banner_emitted_once_when_enabled(self, caplog: pytest.LogCaptureFixture) -> None:
        phase = _StubPhase(NoOpMetricsCollector(), Recovery(pending=True), banner=True)
        with caplog.at_level(logging.INFO):
            phase.run()
        banners = [r for r in caplog.records if r.message == "STUB"]
        assert len(banners) == 1

    def test_no_banner_when_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        phase = _StubPhase(NoOpMetricsCollector(), Recovery(pending=True), banner=False)
        with caplog.at_level(logging.INFO):
            phase.run()
        assert not any(r.message == "STUB" for r in caplog.records)


# ---------------------------------------------------------------------------
# Recovery timing and the recovery line
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_recovery_timed_and_line_counts_wanted_states(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bug guarded: the recovery line dropping the internal/unwanted split
        or breaking the identity wanted == complete + partial + absent — the
        line is the user's only honest view of what remains.  The recovery
        scan must also reach the run's metrics.yaml as a recorded timing row."""
        artifacts = [
            _art(ArtifactState.COMPLETE),
            _art(ArtifactState.ABSENT),
            _art(ArtifactState.PARTIAL),
            _art(ArtifactState.COMPLETE, wanted=False),
        ]
        holder: dict[str, PhaseResult] = {}

        def run(collector) -> None:
            phase = _StubPhase(collector, Recovery.from_artifacts(artifacts))
            with caplog.at_level(logging.INFO):
                holder["result"] = phase.run()

        metrics = _recorded_metrics(tmp_path, run)
        assert "recovery" in _top_level_keys(metrics), (
            f"recovery timing missing from metrics.yaml, got: {_top_level_keys(metrics)}"
        )
        assert any(
            "Recovery: 4 total, 3 wanted (1 complete, 1 partial, 1 absent) — resuming"
            in r.message
            for r in caplog.records
        )
        # Wanted-only exposure; the unwanted artifact stays internal.
        result = holder["result"]
        assert len(result.artifacts) == 3
        assert result.artifacts[0].wanted

    def test_bannerless_phase_emits_soft_start_separator(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bug guarded: a banner-less phase (job, probe) emitted its output
        with no separator at all — its lines visually merged into the previous
        phase's section and read as that phase logging twice."""
        phase = _StubPhase(
            NoOpMetricsCollector(),
            Recovery.from_artifacts([_art(ArtifactState.COMPLETE)]),
            banner=False,
        )
        with caplog.at_level(logging.INFO):
            phase.run()

        messages = [r.message for r in caplog.records]
        assert "" in messages, (
            f"blank separator line missing before the start line: {messages}"
        )
        assert "Starting stub..." in messages, (
            f"soft start line missing: {messages}"
        )
        assert messages.index("Starting stub...") < next(
            i for i, m in enumerate(messages) if m.startswith("Recovery:")
        ), f"start line must precede the recovery line: {messages}"

    def test_fully_reused_ledger_says_all_reused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bug guarded: the suffix claiming "resuming" (or anything but full
        reuse) when every wanted row is complete — the template fast-exits to
        REUSED and this line is the only uniform reuse signal a phase emits."""
        artifacts = [
            _art(ArtifactState.COMPLETE),
            _art(ArtifactState.COMPLETE),
            _art(ArtifactState.COMPLETE, wanted=False),
        ]
        phase = _StubPhase(NoOpMetricsCollector(), Recovery.from_artifacts(artifacts))
        with caplog.at_level(logging.INFO):
            phase.run()

        assert any(
            "Recovery: 3 total, 2 wanted (2 complete, 0 partial, 0 absent) — all reused"
            in r.message
            for r in caplog.records
        )

    def test_fresh_ledger_says_nothing_to_reuse(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Bug guarded: a fresh ledger (nothing complete) reported with a
        resume-flavored suffix — no work exists to resume; every wanted row
        must be produced."""
        phase = _StubPhase(
            NoOpMetricsCollector(),
            Recovery.from_artifacts([_art(ArtifactState.ABSENT)]),
        )
        with caplog.at_level(logging.INFO):
            phase.run()

        assert any(
            "Recovery: 1 total, 1 wanted (0 complete, 0 partial, 1 absent) — nothing to reuse"
            in r.message
            for r in caplog.records
        )

    def test_recovery_error_becomes_failed_result(self) -> None:
        phase = _StubPhase(
            NoOpMetricsCollector(),
            Recovery(pending=True),
            recover_error=RecoveryError("fatal mismatch"),
        )
        result = phase.run()
        assert result.outcome is PhaseOutcome.FAILED
        assert result.message == "fatal mismatch"
        assert phase.execute_calls == 0


# ---------------------------------------------------------------------------
# Branches: dry-run, reuse, execute
# ---------------------------------------------------------------------------


class TestBranches:
    def test_dry_run_with_pending_returns_pending_without_executing(self) -> None:
        phase = _StubPhase(NoOpMetricsCollector(), Recovery(pending=True))
        result = phase.run(dry_run=True)
        assert result.outcome is PhaseOutcome.PENDING
        assert phase.execute_calls == 0

    def test_dry_run_without_pending_returns_reused(self) -> None:
        phase = _StubPhase(
            NoOpMetricsCollector(), Recovery(pending=False), banner=True
        )
        result = phase.run(dry_run=True)
        assert result.outcome is PhaseOutcome.REUSED
        assert getattr(phase, "reused_built", False)
        assert phase.execute_calls == 0

    def test_execute_run_reuses_when_nothing_pending(self) -> None:
        phase = _StubPhase(NoOpMetricsCollector(), Recovery(pending=False))
        result = phase.run(dry_run=False)
        assert result.outcome is PhaseOutcome.REUSED
        assert phase.execute_calls == 0

    def test_execute_run_records_recovery_and_phase_time(self, tmp_path: Path) -> None:
        """An execute run reports both the recovery scan and the phase's own
        execution time in metrics.yaml, and executes with wanted rows only.

        Bug guarded: an execute run losing either timing row from the report,
        or the template handing ``_execute`` unwanted rows (internal ledger
        leakage into production).

        Validates: Requirements 6.5
        """
        artifacts = [
            _art(ArtifactState.ABSENT),
            _art(ArtifactState.COMPLETE),
            _art(ArtifactState.COMPLETE, wanted=False),
        ]
        holder: dict[str, _StubPhase] = {}

        def run(collector) -> None:
            phase = _StubPhase(collector, Recovery.from_artifacts(artifacts))
            holder["phase"] = phase
            phase.run(dry_run=False)

        metrics   = _recorded_metrics(tmp_path, run)
        top_level = _top_level_keys(metrics)
        assert {"recovery", "probe"} <= top_level, (
            f"expected recovery and the phase key in metrics.yaml, got: {sorted(top_level)}"
        )

        phase = holder["phase"]
        assert phase.execute_calls == 1
        assert phase.executed_wanted is not None
        assert all(a.wanted for a in phase.executed_wanted)


# ---------------------------------------------------------------------------
# Runner hard assertion — PENDING surviving an execute run
# ---------------------------------------------------------------------------


class _PendingStub(_StubPhase):
    """Contract-violating phase: _execute returns PENDING."""

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> PhaseResult:
        super()._execute(wanted, dry_run)
        return self._make_result(PhaseOutcome.PENDING, wanted, "violating")


class _FinalizeRecorder(_StubPhase):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.finalized = False

    def finalize(self, ctx: FinalizeContext) -> None:
        self.finalized = True


def _runner_with(target: _StubPhase, collector, *, no_metrics: bool = False, work_dir: Path | None = None) -> Runner:
    registry: PhaseRegistry = {type(target): target}
    return Runner(
        registry,
        type(target),
        collector,
        work_dir=work_dir if work_dir is not None else Path("."),
        cleanup=CleanupLevel.NONE,
        no_metrics=no_metrics,
        is_terminal_most=False,
    )


class TestCollectOutputFiles:
    def test_complete_merged_rows_paths_only(self) -> None:
        """Bug guarded: deliverable collection taking anything but the merge
        result's complete MergedVideo payloads (a directory sniff, an
        incomplete row, a mirror field) — the runner's output_files would
        lie about what the run produced."""
        from pyqenc.models import PhaseOutcome
        from pyqenc.phases.merge import MergePhaseResult
        from pyqenc.stream_model import MergedVideo

        def _row(stem: str, state: ArtifactState) -> Artifact:
            return Artifact(
                payload=MergedVideo(
                    source_stem=stem,
                    strategy=_STRATEGY,
                    output_path=Path(f"D:/w/merged/{stem} {_STRATEGY.safe_name()}.mkv"),
                ),
                state=state,
            )

        _STRATEGY = _merge_strategy()
        result = MergePhaseResult(
            outcome=PhaseOutcome.COMPLETED,
            message="ok",
            merged=[_row("a", ArtifactState.COMPLETE), _row("b", ArtifactState.ABSENT)],
        )
        assert result.output_paths == [
            Path(f"D:/w/merged/a {_STRATEGY.safe_name()}.mkv"),
        ]


def _merge_strategy():
    from decimal import Decimal

    from pyqenc.models import CodecConfig, Strategy

    return Strategy(
        preset="slow", profile="h265",
        codec=CodecConfig(
            name="h265-10bit", default_quality=Decimal(20),
            default_preset="slow",
            quality_range=(Decimal(0), Decimal(51)), presets=["slow"],
        ),
        profile_args=[],
    )


class TestRunnerContractAssertion:
    def test_pending_on_execute_raises_phase_contract_error(self, tmp_path: Path) -> None:
        """Bug guarded: a contract-violating run losing its metrics — the
        metrics.yaml written by the runner's flush is the debug evidence for
        the crash report, so it must exist even on the violation path."""
        collector = YamlMetricsCollector(work_dir=tmp_path, force_wipe=True)
        target = _PendingStub(collector, Recovery(pending=True))
        runner = _runner_with(target, collector, work_dir=tmp_path)

        with pytest.raises(PhaseContractError, match="PENDING on an execute run"):
            runner.run(dry_run=False)

        # Debug evidence preserved (metrics.yaml flushed before the loud raise)
        # and the always-unregister invariant holds even on the violation path.
        assert (tmp_path / "metrics.yaml").exists(), (
            "metrics.yaml must be flushed even on a contract-violating run"
        )
        assert collector not in _live_collectors

    def test_pending_on_dry_run_does_not_raise(self) -> None:
        collector = _spy_collector()
        target = _PendingStub(collector, Recovery(pending=True))
        runner = _runner_with(target, collector)

        summary = runner.run(dry_run=True)
        assert summary.success is True

    def test_finalize_broadcast_on_success_not_on_violation(self) -> None:
        ok = _FinalizeRecorder(_spy_collector(), Recovery(pending=False))
        _runner_with(ok, ok._collector).run(dry_run=False)
        assert ok.finalized is True

        violating = _PendingStub(_spy_collector(), Recovery(pending=True))
        with pytest.raises(PhaseContractError):
            _runner_with(violating, violating._collector).run(dry_run=False)
        assert violating.execute_calls == 1
