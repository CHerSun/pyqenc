"""Unit tests for PhaseResult derived properties.

Covers:
- ``is_complete``: True for COMPLETED/REUSED, False for FAILED/DRY_RUN
- ``complete``:    Filters artifacts to COMPLETE state only
- ``pending``:     Filters artifacts to ABSENT/ARTIFACT_ONLY/STALE states
- ``did_work``:    True only for COMPLETED outcome
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyqenc.models import PhaseOutcome
from pyqenc.phase import Artifact, PhaseResult
from pyqenc.state import ArtifactState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _artifact(state: ArtifactState) -> Artifact:
    return Artifact(path=Path("/fake/path"), state=state)


def _result(outcome: PhaseOutcome, states: list[ArtifactState] = ()) -> PhaseResult:
    return PhaseResult(
        outcome=outcome,
        artifacts=[_artifact(s) for s in states],
        message="test",
    )


# ---------------------------------------------------------------------------
# is_complete
# ---------------------------------------------------------------------------

class TestIsComplete:
    def test_completed_is_complete(self) -> None:
        assert _result(PhaseOutcome.COMPLETED).is_complete is True

    def test_reused_is_complete(self) -> None:
        assert _result(PhaseOutcome.REUSED).is_complete is True

    def test_failed_is_not_complete(self) -> None:
        assert _result(PhaseOutcome.FAILED).is_complete is False

    def test_dry_run_is_not_complete(self) -> None:
        assert _result(PhaseOutcome.DRY_RUN).is_complete is False


# ---------------------------------------------------------------------------
# complete property
# ---------------------------------------------------------------------------

class TestCompleteProperty:
    def test_returns_only_complete_artifacts(self) -> None:
        result = _result(PhaseOutcome.COMPLETED, [
            ArtifactState.COMPLETE,
            ArtifactState.ABSENT,
            ArtifactState.COMPLETE,
        ])
        assert len(result.complete) == 2
        assert all(a.state == ArtifactState.COMPLETE for a in result.complete)

    def test_empty_when_no_complete_artifacts(self) -> None:
        result = _result(PhaseOutcome.FAILED, [ArtifactState.ABSENT])
        assert result.complete == []

    def test_empty_when_no_artifacts(self) -> None:
        assert _result(PhaseOutcome.REUSED).complete == []


# ---------------------------------------------------------------------------
# pending property
# ---------------------------------------------------------------------------

class TestPendingProperty:
    def test_absent_is_pending(self) -> None:
        result = _result(PhaseOutcome.FAILED, [ArtifactState.ABSENT])
        assert len(result.pending) == 1

    def test_artifact_only_is_pending(self) -> None:
        result = _result(PhaseOutcome.FAILED, [ArtifactState.ARTIFACT_ONLY])
        assert len(result.pending) == 1

    def test_stale_is_not_pending(self) -> None:
        """STALE artifacts are handled by phase-specific logic, not via pending."""
        result = _result(PhaseOutcome.FAILED, [ArtifactState.STALE])
        assert result.pending == []

    def test_complete_is_not_pending(self) -> None:
        result = _result(PhaseOutcome.REUSED, [ArtifactState.COMPLETE])
        assert result.pending == []

    def test_mixed_states(self) -> None:
        result = _result(PhaseOutcome.COMPLETED, [
            ArtifactState.COMPLETE,
            ArtifactState.ABSENT,
            ArtifactState.STALE,
            ArtifactState.ARTIFACT_ONLY,
        ])
        assert len(result.pending) == 2   # only ABSENT + ARTIFACT_ONLY
        assert len(result.complete) == 1


# ---------------------------------------------------------------------------
# did_work
# ---------------------------------------------------------------------------

class TestDidWork:
    def test_completed_did_work(self) -> None:
        assert _result(PhaseOutcome.COMPLETED).did_work is True

    def test_reused_did_not_work(self) -> None:
        assert _result(PhaseOutcome.REUSED).did_work is False

    def test_failed_did_not_work(self) -> None:
        assert _result(PhaseOutcome.FAILED).did_work is False

    def test_dry_run_did_not_work(self) -> None:
        assert _result(PhaseOutcome.DRY_RUN).did_work is False
