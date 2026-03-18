"""Utility helpers for alive_progress bars."""
from collections.abc import Callable, Generator
from contextlib import contextmanager
from math import floor

from alive_progress import alive_bar

from pyqenc.constants import PROGRESS_DURATION_UNIT, WARNING_SYMBOL


def update_bar(bar: Callable[[float], None] | None, increment: float = 1.0, failed: int = 0) -> None:
    """Advance a progress bar handle, optionally showing a failed-chunk warning.

    Args:
        bar:       The advance callable yielded by ``duration_bar``, or ``None`` to no-op.
        increment: Seconds (or fraction) to advance.  Pass ``0.0`` to update text only.
        failed:    Number of failed chunks to display in the bar text.
    """
    if bar is not None:
        bar.text = "" if failed <= 0 else f"{WARNING_SYMBOL} {failed} failed"  # type: ignore[attr-defined]
        if increment != 0.0:
            bar(increment)


@contextmanager
def duration_bar(
    total_seconds: float,
    title: str,
    unit: str = PROGRESS_DURATION_UNIT,
) -> Generator[Callable[[float], None], None, None]:
    """Context manager that opens a duration-based ``alive_bar`` in manual mode.

    Yields an ``advance(seconds: float) -> None`` callable.  The bar fraction is
    updated as ``min(1.0, cumulative_seconds / total_seconds)``.  On normal exit
    the bar is set to ``1.0`` (complete).

    When ``total_seconds <= 0`` the bar opens as an indeterminate spinner and
    ``advance`` becomes a no-op.

    Args:
        total_seconds: Total video duration this bar represents (seconds).
        title:         Bar title shown to the user.
        unit:          Unit label appended to the throughput readout.
    """
    if total_seconds <= 0:
        # Indeterminate spinner — no fraction arithmetic possible.
        with alive_bar(title=title, unit=unit) as _bar:
            def _noop(_seconds: float) -> None:  # noqa: ANN001
                pass
            yield _noop
        return

    cumulative: list[float] = [0.0]
    failed: list[int] = [0]

    ## manual mode with 0.0-1.0 percentage. Looks bad - always shows % instead of numbers with units.
    # with alive_bar(manual=True, title=title, unit=unit) as bar:
    #     def advance(seconds: float) -> None:
    #         cumulative[0] += seconds
    #         bar(min(1.0, cumulative[0] / total_seconds))  # type: ignore[operator]
    #
    #     yield advance
    #     bar(1.0)  # ensure bar reaches 100 % on normal exit
    total_int = floor(total_seconds)
    with alive_bar(total_int, title=title, unit=unit) as bar:
        def advance(seconds: float, _failed: int = 0) -> None:
            failed[0] += _failed
            prev: int = floor(cumulative[0])
            cumulative[0] += seconds
            if failed[0]:
                bar.text = f"{WARNING_SYMBOL} {failed[0]} failed"
            bar(floor(cumulative[0]) - prev)

        yield advance

