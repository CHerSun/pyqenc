"""Utility helpers for alive_progress bars."""
# CHerSun 2026

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import assert_never

from alive_progress import alive_bar, config_handler

from pyqenc.constants import FAILURE_SYMBOL_MINOR, SKIPPED_SYMBOL, SUCCESS_SYMBOL_MINOR

# The single global bar configuration (this module owns all alive_progress
# usage): output printed while a bar is active must pass through byte-stable,
# not re-styled by the bar — so print enrichment is off program-wide.
config_handler.set_global(enrich_print=False)


class AdvanceState(Enum):
    """Outcome reported to :func:`ProgressBar` on each ``advance()`` call."""

    PROGRESS = "progress"
    """Mid-item duration tick — advances the bar fraction but does not
    increment any item counter.  Use for streaming progress feeds (e.g.
    ffmpeg ``out_time_s`` callbacks) where the work unit is still in flight."""
    SUCCESS  = "success"
    """The item was processed successfully."""
    SKIPPED  = "skipped"
    """The item was skipped because its output already exists (reused artifact)."""
    FAILED   = "failed"
    """The item failed to process."""
    COMPLETE = "complete"
    """The phase has fully finished all work.  Forces the bar to 100 % without
    affecting item counters.  Call once as the last action inside the
    ``with ProgressBar(...)`` block when the phase genuinely completed."""


# Canonical type for a progress-advance callable yielded by :func:`ProgressBar`
# and accepted by consumers that drive an outer bar (e.g. merge phase feeding
# its outer bar into a per-file measurement call).
BarAdvance = Callable[[int | float, AdvanceState], None]


@dataclass
class ProgressBarState:
    total : float

    cumulative: float
    remaining: float

    success_count: int = 0
    skipped_count: int = 0
    failed_count:  int = 0

    as_float: bool = False

    def __init__(self, total: int|float) -> None:
        self.total = total
        self.remaining = self.total
        self.as_float = isinstance(total, float)
        self.cumulative = 0.0 if self.as_float else 0

@contextmanager
def ProgressBar(
    total: int | float,
    title: str,
    show_counters: bool = True,
    total_count:   int | None = None,
) -> Generator[BarAdvance]:
    """Context manager that opens an ``alive_bar`` progress bar.

    Always uses manual mode (0.0–1.0 fraction, renders as %) when *total* > 0.
    When *total* <= 0 opens an indeterminate spinner instead.

    Yields an ``advance(increment, state)`` callable that callers invoke to
    report progress.  Pass ``AdvanceState.PROGRESS`` for streaming duration
    ticks (fraction moves, no counter change); pass ``SUCCESS``, ``SKIPPED``,
    or ``FAILED`` when a discrete item completes.  The bar fraction is computed
    from the caller's domain units — seconds for duration-based phases, item
    count for count-based phases.

    Args:
        total:         Total weight (seconds or item count).  ``0`` → indeterminate.
        title:         Bar title shown to the user.
        show_counters: When ``True`` (default) bar text shows
                       ``✔ {success}  ⏭ {skipped}`` (plus ``✘ {failed}`` when
                       failures exist).  When ``False`` bar text shows
                       ``{cumulative:.1f} / {total:.1f}`` — useful for streaming
                       sub-second progress feeds where item counters are noise.
        total_count:   Optional total number of items.  When provided, the counter
                       section shows ``out of {total_count}`` after the item counters.
    """
    bar_state = ProgressBarState(total)

    def _text() -> str:
        elements: list[str] = [
            f"{bar_state.cumulative:.{int(bar_state.as_float)}f} out of {bar_state.remaining:.{int(bar_state.as_float)}f}" # format for int/float depending on state
        ]
        if show_counters:
            # Bar text: "2.5 out of 10.0 (✔ 1  ⏭ 2  ✘ 3  out of 9)"
            counters: list[str] = []
            counters.append(f"{SUCCESS_SYMBOL_MINOR} {bar_state.success_count}")
            if bar_state.skipped_count > 0:
                counters.append(f"{SKIPPED_SYMBOL} {bar_state.skipped_count}")
            if bar_state.failed_count > 0:
                counters.append(f"{FAILURE_SYMBOL_MINOR} {bar_state.failed_count}")
            if total_count is not None:
                counters.append(f"out of {total_count}")
            elements.append("(" + "  ".join(counters) + ")")
        return " ".join(elements)

    if total <= 0:
        # Indeterminate spinner — no fraction arithmetic possible.
        with alive_bar(title=title) as bar:
            def advance(increment: int | float, state: AdvanceState) -> None:
                if state == AdvanceState.PROGRESS:
                    pass  # no fraction in indeterminate mode; spinner animates on its own
                elif state == AdvanceState.SUCCESS:
                    bar_state.success_count += 1
                elif state == AdvanceState.SKIPPED:
                    bar_state.skipped_count += 1
                elif state == AdvanceState.FAILED:
                    bar_state.failed_count += 1
                elif state == AdvanceState.COMPLETE:
                    pass  # indeterminate bars have no fraction to force
                else:
                    assert_never(state)
                bar.text = _text()
            bar.text = _text()
            yield advance
        return

    with alive_bar(manual=True, title=title) as bar:
        def advance(increment: int | float, state: AdvanceState) -> None:
            if state == AdvanceState.COMPLETE:
                # Phase explicitly declares full completion — force 100 %, no counter change.
                bar(1.0)
                bar.text = _text()
                return
            if state == AdvanceState.PROGRESS:
                bar_state.cumulative += increment
            elif state == AdvanceState.SKIPPED:
                bar_state.skipped_count += 1
                bar_state.remaining -= increment
            elif state == AdvanceState.FAILED:
                bar_state.failed_count += 1
                # no fraction change, no total change
            elif state == AdvanceState.SUCCESS:
                bar_state.success_count += 1
                bar_state.cumulative += increment
            else:
                assert_never(state)
            # Update the bar
            bar(1.0 if bar_state.remaining <= 0
                else min(1.0, bar_state.cumulative / bar_state.remaining))
            bar.text = _text()
        bar.text = _text()
        yield advance
