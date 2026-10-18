"""Tests for ``run_bounded`` — bounded concurrent execution.

**Validates: concurrency spec Req 6–8**
"""

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyqenc.utils.concurrency import run_bounded


class TestRunBoundedHappyPath:
    def test_results_in_input_order_at_every_bound(self) -> None:
        """Results come back in input order at bound 1 and above.

        Bug condition: completion-order results (plain task append) reach a
        phase that expects input order — e.g. measure's per-video summary
        listing videos in a random order per run.
        """
        items = list(range(12))

        async def jittered(item: int) -> int:
            await asyncio.sleep((item % 4) * 0.001)
            return item * 10

        for bound in (1, 2, 3, 12, 20):
            results = asyncio.run(run_bounded(items, bound, jittered))
            assert results == [i * 10 for i in items]

    def test_empty_items_return_empty_list(self) -> None:
        """An empty job list completes immediately with no workers."""
        calls: list[int] = []

        async def record(item: int) -> int:
            calls.append(item)
            return item

        assert asyncio.run(run_bounded([], 3, record)) == []
        assert calls == []


class TestRunBoundedProperties:
    @given(
        items=st.lists(st.integers(min_value=0, max_value=999), max_size=25),
        bound=st.integers(min_value=1, max_value=8),
    )
    @settings(max_examples=40)
    def test_identity_starts_in_input_order_and_never_exceeds_bound(
        self, items: list[int], bound: int,
    ) -> None:
        """Properties: starts follow input order; in-flight jobs never exceed
        the bound; results equal the input under the identity process.

        Bug condition: worker startup reordering (starts not matching the
        pending-list order) breaks the same-order-at-any-bound contract, or
        a semaphore-less overshoot runs more jobs than the knob allows.
        """
        started: list[int] = []
        active = 0
        peak = 0

        async def tracked(item: int) -> int:
            nonlocal active, peak
            started.append(item)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return item

        results = asyncio.run(run_bounded(items, bound, tracked))

        assert started == items
        assert results == items
        assert peak <= bound


class TestRunBoundedErrorHandling:
    def test_raising_process_propagates_to_caller(self) -> None:
        """A raising job is the bug path — the exception is not swallowed.

        Bug condition: containment inside the runner would hide domain bugs
        as silently missing results.
        """

        async def explode(item: int) -> int:
            if item == 2:
                raise RuntimeError("bug path")
            return item

        with pytest.raises(RuntimeError, match="bug path"):
            asyncio.run(run_bounded([1, 2, 3], 2, explode))
