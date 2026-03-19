"""Utility helpers for alive_progress bars."""
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import Enum

from alive_progress import alive_bar

from pyqenc.constants import FAILURE_SYMBOL_MINOR, SKIPPED_SYMBOL, SUCCESS_SYMBOL_MINOR


class AdvanceState(Enum):
    """Outcome of a completed pipeline item reported to :func:`ProgressBar`."""

    SUCCESS = "success"
    """The item was processed successfully."""
    SKIPPED = "skipped"
    """The item was skipped because its output already exists (reused artifact)."""
    FAILED  = "failed"
    """The item failed to process."""


@contextmanager
def ProgressBar(
    total: int | float,
    title: str,
    show_counters: bool = True,
) -> Generator[Callable[[int | float, AdvanceState], None], None, None]:
    """Context manager that opens an ``alive_bar`` progress bar.

    Always uses manual mode (0.0–1.0 fraction, renders as %) when *total* > 0.
    When *total* <= 0 opens an indeterminate spinner instead.

    Yields an ``advance(increment, state)`` callable that callers invoke once
    per completed item.  The bar fraction is computed from the caller's domain
    units — seconds for duration-based phases, item count for count-based phases.

    Args:
        total:         Total weight (seconds or item count).  ``0`` → indeterminate.
        title:         Bar title shown to the user.
        show_counters: When ``True`` (default) bar text shows
                       ``✔ {success}  ⏭ {skipped}`` (plus ``✘ {failed}`` when
                       failures exist).  When ``False`` bar text shows
                       ``{cumulative:.1f} / {total:.1f}`` — useful for streaming
                       sub-second progress feeds where item counters are noise.
    """
    _remaining:  list[float] = [float(total)]
    _cumulative: list[float] = [0.0]
    _success:    list[int]   = [0]
    _skipped:    list[int]   = [0]
    _failed:     list[int]   = [0]
    _original_total: float   = float(total)

    def _text() -> str:
        if show_counters:
            base = f"{SUCCESS_SYMBOL_MINOR} {_success[0]}  {SKIPPED_SYMBOL} {_skipped[0]}"
            return base if _failed[0] == 0 else f"{base}  {FAILURE_SYMBOL_MINOR} {_failed[0]}"
        return f"{_cumulative[0]:.1f} / {_original_total:.1f}"

    if total <= 0:
        # Indeterminate spinner — no fraction arithmetic possible.
        with alive_bar(title=title) as bar:
            def advance(increment: int | float = 1, state: AdvanceState = AdvanceState.SUCCESS) -> None:  # noqa: E501
                if state == AdvanceState.SUCCESS:
                    _success[0] += 1
                elif state == AdvanceState.SKIPPED:
                    _skipped[0] += 1
                else:
                    _failed[0] += 1
                bar.text = _text()  # type: ignore[attr-defined]

            yield advance
        return

    with alive_bar(manual=True, title=title) as bar:
        def advance(increment: int | float = 1, state: AdvanceState = AdvanceState.SUCCESS) -> None:  # noqa: E501
            if state == AdvanceState.SKIPPED:
                _skipped[0] += 1
                _remaining[0] -= increment
                if _remaining[0] <= 0:
                    bar(1.0)  # type: ignore[operator]
                else:
                    bar(min(1.0, _cumulative[0] / _remaining[0]))  # type: ignore[operator]
            elif state == AdvanceState.FAILED:
                _failed[0] += 1
                # no fraction change, no total change
            else:  # SUCCESS
                _success[0] += 1
                _cumulative[0] += increment
                if _remaining[0] > 0:
                    bar(min(1.0, _cumulative[0] / _remaining[0]))  # type: ignore[operator]
            bar.text = _text()  # type: ignore[attr-defined]

        yield advance
