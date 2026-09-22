"""Unit tests for the PhaseBase template run() — observable behavior only.

A minimal stub phase exercises the template mechanics through the public
``run()``: memoization, banner emission, timed recovery, wanted-filtering,
the dry-run / no-pending branches, and the runner's hard assertion on a
PENDING-surviving execute run.

Only observable surfaces are asserted: the returned ``PhaseResult``, the
log stream (banner / recovery line), the collector's ``time`` calls, and
``finalize`` being called or not. No template internals are inspected.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from pyqenc.app_config import AppConfig, load_app_config
from pyqenc.metrics import MetricKey, NoOpMetricsCollector
from pyqenc.models import CleanupLevel, PhaseOutcome
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseBase,
    PhaseContractError,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.runner import Runner
from pyqenc.state import ArtifactState

_APP_CONFIG: AppConfig = load_app_config(default_only=True)


class _StubPhase(PhaseBase):
    """Minimal concrete phase: canned recovery, recording execute, base result."""

    name = "stub"
    _METRIC_KEY = MetricKey.PROBE  # any real member; PROBE is unused by stubs

    def __init__(
        self,
        collector,
        recovery: Recovery,
        execute_result: PhaseResult | None = None,
        *,
        banner: bool = True,
        recover_error: RecoveryError | None = None,
    ) -> None:
        super().__init__(cast("AppConfig", _APP_CONFIG), None, collector=collector)
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
        error: str | None = None,
    ) -> PhaseResult:
        return PhaseResult(
            outcome=PhaseOutcome(outcome), artifacts=artifacts, message=message, error=error
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> PhaseResult:
        self.reused_built = True
        return super()._reused_result(wanted, message)


def _spy_collector() -> MagicMock:
    from pyqenc.metrics import MetricsCollector

    collector = MagicMock(spec=MetricsCollector)
    collector.time.return_value = __import__("contextlib").nullcontext()
    return collector


def _art(state: ArtifactState, *, wanted: bool = True) -> Artifact:
    return Artifact(path=Path(f"{state.value}_{wanted}.mkv"), state=state, wanted=wanted)


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
    def test_recovery_timed_and_line_logged_with_unwanted_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        artifacts = [_art(ArtifactState.COMPLETE), _art(ArtifactState.COMPLETE, wanted=False)]
        phase = _StubPhase(_spy_collector(), Recovery.from_artifacts(artifacts))
        with caplog.at_level(logging.INFO):
            result = phase.run()

        assert [call.args[0] for call in _collector_of(phase).time.call_args_list] == [
            MetricKey.RECOVERY
        ]
        assert any("2 total, 1 unwanted" in r.message for r in caplog.records)
        # Wanted-only exposure; the unwanted artifact stays internal.
        assert len(result.artifacts) == 1
        assert result.artifacts[0].wanted

    def test_recovery_error_becomes_failed_result(self) -> None:
        phase = _StubPhase(
            NoOpMetricsCollector(),
            Recovery(pending=True),
            recover_error=RecoveryError("fatal mismatch"),
        )
        result = phase.run()
        assert result.outcome is PhaseOutcome.FAILED
        assert result.error == "fatal mismatch"
        assert phase.execute_calls == 0


def _collector_of(phase: _StubPhase) -> MagicMock:
    return phase._collector


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

    def test_execute_runs_under_top_level_key_with_wanted_only(self) -> None:
        artifacts = [
            _art(ArtifactState.ABSENT),
            _art(ArtifactState.COMPLETE),
            _art(ArtifactState.COMPLETE, wanted=False),
        ]
        collector = _spy_collector()
        phase = _StubPhase(collector, Recovery.from_artifacts(artifacts))
        phase.run(dry_run=False)

        assert phase.execute_calls == 1
        assert phase.executed_wanted is not None
        assert all(a.wanted for a in phase.executed_wanted)
        keys = [call.args[0] for call in collector.time.call_args_list]
        assert keys == [MetricKey.RECOVERY, MetricKey.PROBE]


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


def _runner_with(target: _StubPhase, collector, *, no_metrics: bool = False) -> Runner:
    registry: dict[type[Phase], Phase] = {type(target): target}
    return Runner(
        registry,
        type(target),
        collector,
        work_dir=Path("."),
        cleanup=CleanupLevel.NONE,
        no_metrics=no_metrics,
        is_terminal_most=False,
    )


class TestRunnerContractAssertion:
    def test_pending_on_execute_raises_phase_contract_error(self) -> None:
        collector = _spy_collector()
        target = _PendingStub(collector, Recovery(pending=True))
        runner = _runner_with(target, collector)

        with pytest.raises(PhaseContractError, match="PENDING on an execute run"):
            runner.run(dry_run=False)

        # Debug evidence preserved and the always-unregister invariant holds
        # even on the violation path.
        assert collector.flush.called
        assert collector.close.called

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
