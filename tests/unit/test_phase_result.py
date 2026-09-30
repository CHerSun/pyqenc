"""Unit tests for PhaseResult derived properties.

``artifacts`` is the derived, read-only concatenation of the result subclass's
declared artifact fields (dataclass-fields introspection, ``Artifact``-typed
fields only, declaration order — Req 6.2). A bare ``PhaseResult`` declares no
artifact fields, so a stub subclass stands in for every concrete result.

Covers:
- ``artifacts``: derived concatenation, declaration order, plain fields skipped
- ``is_complete``: True for COMPLETED/REUSED, False for FAILED/PENDING
- ``complete``:    Filters artifacts to COMPLETE state only
- ``pending``:     Filters artifacts to ABSENT/PARTIAL states
- ``did_work``:    True only for COMPLETED outcome

Selection (``wanted``) is orthogonal to completeness: only wanted rows are
placed into the declared fields by ``_make_result``, so an unwanted artifact
(the replacement for the former ``STALE`` state — now ``wanted=False`` with
completeness ``COMPLETE``) never appears in ``artifacts``, ``pending``, or
``complete``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pyqenc.models import PhaseOutcome, Strategy
from pyqenc.phase import Artifact, PhaseResult
from pyqenc.state import ArtifactState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _artifact(state: ArtifactState, wanted: bool = True) -> Artifact:
    return Artifact(payload=Path("/fake/path"), state=state, wanted=wanted)


@dataclass
class _StubResult(PhaseResult):
    """A result shaped like the concrete ones: artifact fields + plain fields."""

    video:      Artifact | None = None
    streams:    list[Artifact]  = field(default_factory=list)
    strategies: list[Strategy]  = field(default_factory=list)  # settings — never contributes
    work_dir:   Path | None     = None


def _result(outcome: PhaseOutcome, states: list[ArtifactState]) -> PhaseResult:
    return _StubResult(
        outcome = outcome,
        message = "test",
        streams = [_artifact(s) for s in states],
    )


# ---------------------------------------------------------------------------
# Derived artifacts — the declared-contract concatenation (Req 6.2)
# ---------------------------------------------------------------------------

class TestDerivedArtifacts:
    def test_concatenates_declared_fields_in_declaration_order(self) -> None:
        """Bug guarded: the derived list dropping rows or reordering them —
        consumers iterate it as the phase's complete external contract."""
        first  = _artifact(ArtifactState.COMPLETE)
        second = _artifact(ArtifactState.ABSENT)
        third  = _artifact(ArtifactState.PARTIAL)
        result = _StubResult(
            outcome = PhaseOutcome.COMPLETED,
            message = "test",
            video   = first,
            streams = [second, third],
        )
        assert result.artifacts == [first, second, third]

    @staticmethod
    def _strategy() -> Strategy:
        from decimal import Decimal

        from pyqenc.models import CodecConfig

        return Strategy(
            preset="slow", profile="h265",
            codec=CodecConfig(
                name="h265-10bit", default_quality=Decimal(20),
                default_preset="slow",
                quality_range=(Decimal(0), Decimal(51)), presets=["slow"],
            ),
            profile_args=[],
        )

    def test_plain_fields_never_contribute(self) -> None:
        """Bug guarded: a settings field (list[Strategy]) or run parameter
        leaking into the artifact contract — internal machinery has no path
        into a result."""
        result = _StubResult(
            outcome    = PhaseOutcome.COMPLETED,
            message    = "test",
            strategies = [self._strategy()],
            work_dir   = Path("/tmp"),
        )
        assert result.artifacts == []

    def test_none_fields_skipped(self) -> None:
        result = _StubResult(outcome=PhaseOutcome.FAILED, message="test")
        assert result.artifacts == []

    def test_bare_result_has_no_artifacts(self) -> None:
        """The base class declares no artifact fields — the contract is the
        subclass's."""
        assert PhaseResult(outcome=PhaseOutcome.REUSED, message="").artifacts == []

    def test_derived_list_is_read_only_view(self) -> None:
        """Bug guarded: mutating the derived list must not affect the result
        (the declared fields are the single storage)."""
        result = _result(PhaseOutcome.COMPLETED, [ArtifactState.COMPLETE])
        derived = result.artifacts
        derived.clear()
        assert len(result.artifacts) == 1


# ---------------------------------------------------------------------------
# is_complete
# ---------------------------------------------------------------------------

class TestIsComplete:
    def test_completed_is_complete(self) -> None:
        assert _result(PhaseOutcome.COMPLETED, []).is_complete is True

    def test_reused_is_complete(self) -> None:
        assert _result(PhaseOutcome.REUSED, []).is_complete is True

    def test_failed_is_not_complete(self) -> None:
        assert _result(PhaseOutcome.FAILED, []).is_complete is False

    def test_pending_is_not_complete(self) -> None:
        assert _result(PhaseOutcome.PENDING, []).is_complete is False


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
        assert _result(PhaseOutcome.REUSED, []).complete == []


# ---------------------------------------------------------------------------
# pending property
# ---------------------------------------------------------------------------

class TestPendingProperty:
    def test_absent_is_pending(self) -> None:
        result = _result(PhaseOutcome.FAILED, [ArtifactState.ABSENT])
        assert len(result.pending) == 1

    def test_artifact_only_is_pending(self) -> None:
        result = _result(PhaseOutcome.FAILED, [ArtifactState.PARTIAL])
        assert len(result.pending) == 1

    def test_unwanted_rows_never_reach_the_result(self) -> None:
        """A former-STALE artifact (now wanted=False, COMPLETE) is filtered
        before fields are populated, so it never surfaces as pending."""
        result = _StubResult(
            outcome = PhaseOutcome.FAILED,
            message = "test",
            streams = [],  # the unwanted row stayed internal
        )
        assert result.artifacts == []
        assert result.pending == []

    def test_complete_is_not_pending(self) -> None:
        result = _result(PhaseOutcome.REUSED, [ArtifactState.COMPLETE])
        assert result.pending == []

    def test_mixed_states(self) -> None:
        """Only wanted rows contribute to pending/complete; unwanted ones are
        excluded upstream (the declared fields are populated from the wanted
        rows only)."""
        result = _result(PhaseOutcome.COMPLETED, [
            ArtifactState.COMPLETE,
            ArtifactState.ABSENT,
            ArtifactState.PARTIAL,
        ])
        assert len(result.pending) == 2             # only ABSENT + PARTIAL
        assert len(result.complete) == 1


# ---------------------------------------------------------------------------
# did_work
# ---------------------------------------------------------------------------

class TestDidWork:
    def test_completed_did_work(self) -> None:
        assert _result(PhaseOutcome.COMPLETED, []).did_work is True

    def test_reused_did_not_work(self) -> None:
        assert _result(PhaseOutcome.REUSED, []).did_work is False


# ---------------------------------------------------------------------------
# Correctness Property 1 — single wrapper, no subclasses (Req 1.2)
# ---------------------------------------------------------------------------

class TestSingleWrapper:
    def test_no_artifact_subclass_exists_project_wide(self) -> None:
        """Bug guarded: a per-phase Artifact subclass reappearing — identity
        would drift back into duplicated wrapper fields, the split brain this
        spec deleted. Every class in the package whose MRO includes Artifact
        must be Artifact itself."""
        import importlib
        import pkgutil

        import pyqenc

        offenders: list[str] = []
        for module_info in pkgutil.walk_packages(pyqenc.__path__, "pyqenc."):
            module = importlib.import_module(module_info.name)
            for attr in vars(module).values():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Artifact)
                    and attr is not Artifact
                    and attr.__module__ == module_info.name
                ):
                    offenders.append(f"{attr.__module__}.{attr.__name__}")
        assert offenders == [], f"Artifact subclasses exist: {offenders}"
