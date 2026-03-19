"""Unit tests for ProgressBar and AdvanceState in pyqenc/utils/alive.py."""
import pytest

from pyqenc.utils.alive import AdvanceState, ProgressBar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect(total: float, calls: list[tuple[float, AdvanceState]], show_counters: bool = True) -> tuple[list[float], list[str]]:
    """Run ProgressBar with the given advance calls; return (fractions, texts)."""
    fractions: list[float] = []
    texts: list[str] = []

    class _FakeBar:
        def __init__(self) -> None:
            self.text: str = ""
            self._fraction: float = 0.0

        def __call__(self, fraction: float) -> None:
            self._fraction = fraction
            fractions.append(fraction)

    fake = _FakeBar()

    # Patch alive_bar so we don't need a real terminal.
    import pyqenc.utils.alive as alive_mod
    from contextlib import contextmanager
    from collections.abc import Generator

    @contextmanager
    def _mock_alive_bar(**kwargs: object) -> Generator[_FakeBar, None, None]:
        yield fake

    original = alive_mod.alive_bar
    alive_mod.alive_bar = _mock_alive_bar  # type: ignore[assignment]
    try:
        with ProgressBar(total, title="Test", show_counters=show_counters) as advance:
            for increment, state in calls:
                advance(increment, state)
                texts.append(fake.text)
    finally:
        alive_mod.alive_bar = original  # type: ignore[assignment]

    return fractions, texts


# ---------------------------------------------------------------------------
# SUCCESS
# ---------------------------------------------------------------------------

def test_success_advances_fraction() -> None:
    fractions, _ = _collect(10.0, [(5.0, AdvanceState.SUCCESS), (5.0, AdvanceState.SUCCESS)])
    assert fractions[0] == pytest.approx(0.5)
    assert fractions[1] == pytest.approx(1.0)


def test_success_fraction_capped_at_1() -> None:
    fractions, _ = _collect(5.0, [(10.0, AdvanceState.SUCCESS)])
    assert fractions[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# SKIPPED
# ---------------------------------------------------------------------------

def test_skipped_reduces_remaining_and_recomputes_fraction() -> None:
    # total=10, skip 5 → remaining=5, cumulative=0 → fraction=0/5=0
    fractions, _ = _collect(10.0, [(5.0, AdvanceState.SKIPPED)])
    assert fractions[0] == pytest.approx(0.0)


def test_skipped_fraction_reaches_1_when_remaining_hits_0() -> None:
    fractions, _ = _collect(10.0, [(10.0, AdvanceState.SKIPPED)])
    assert fractions[0] == pytest.approx(1.0)


def test_skipped_then_success_fraction() -> None:
    # total=10, skip 4 → remaining=6; then success 3 → cumulative=3, fraction=3/6=0.5
    fractions, _ = _collect(10.0, [(4.0, AdvanceState.SKIPPED), (3.0, AdvanceState.SUCCESS)])
    assert fractions[1] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# FAILED
# ---------------------------------------------------------------------------

def test_failed_does_not_change_fraction() -> None:
    fractions, _ = _collect(10.0, [(5.0, AdvanceState.SUCCESS), (5.0, AdvanceState.FAILED)])
    # Only one fraction update (from SUCCESS); FAILED produces no bar() call
    assert len(fractions) == 1
    assert fractions[0] == pytest.approx(0.5)


def test_failed_does_not_reduce_remaining() -> None:
    # After FAILED, a subsequent SUCCESS should still use original remaining
    fractions, _ = _collect(10.0, [(5.0, AdvanceState.FAILED), (5.0, AdvanceState.SUCCESS)])
    assert fractions[0] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Indeterminate mode (total=0)
# ---------------------------------------------------------------------------

def test_indeterminate_mode_does_not_crash() -> None:
    fractions, texts = _collect(0.0, [(1.0, AdvanceState.SUCCESS)])
    # No fraction calls in indeterminate mode
    assert fractions == []
    # But text is still updated
    assert len(texts) == 1


# ---------------------------------------------------------------------------
# Bar text format
# ---------------------------------------------------------------------------

def test_text_no_failures() -> None:
    _, texts = _collect(10.0, [(1.0, AdvanceState.SUCCESS), (1.0, AdvanceState.SKIPPED)])
    # After success: ✔ 1  ⏭ 0
    assert "✔ 1" in texts[0]
    assert "⏭ 0" in texts[0]
    assert "✘" not in texts[0]
    # After skipped: ✔ 1  ⏭ 1
    assert "⏭ 1" in texts[1]


def test_text_with_failures() -> None:
    _, texts = _collect(10.0, [(1.0, AdvanceState.FAILED)])
    assert "✘ 1" in texts[0]


def test_text_show_counters_false() -> None:
    _, texts = _collect(10.0, [(5.0, AdvanceState.SUCCESS)], show_counters=False)
    # Should show "5.0 / 10.0"
    assert "5.0 / 10.0" in texts[0]
