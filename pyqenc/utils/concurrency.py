"""Bounded concurrent execution of independent jobs."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence


async def run_bounded[ItemT, ResultT](
    items:   Sequence[ItemT],
    bound:   int,
    process: Callable[[ItemT], Awaitable[ResultT]],
) -> list[ResultT]:
    """Run ``process`` over *items* with at most *bound* jobs in flight.

    Jobs start in input order and one result is returned per job, in input
    order, regardless of completion order. ``bound == 1`` is one worker
    walking the list — sequential in input order on the same machinery.

    Args:
        items:   Pre-filtered, pre-sorted jobs; classification (which items
                 need work) and ordering stay with the caller.
        bound:   Maximum simultaneous jobs (``>= 1``).
        process: One job's work. Owns all of its progress reporting and its
                 error containment — a raise propagates to the caller (the
                 bug path) instead of being contained here.

    Returns:
        One result per input item, in input order.
    """
    assert bound >= 1, "bound comes from a validated concurrency knob"

    pending: deque[tuple[int, ItemT]] = deque(enumerate(items))
    done:    dict[int, ResultT]       = {}

    async def worker() -> None:
        while pending:
            i, item = pending.popleft()
            done[i] = await process(item)

    await asyncio.gather(*(worker() for _ in range(min(bound, len(pending)))))
    return [done[i] for i in range(len(items))]
