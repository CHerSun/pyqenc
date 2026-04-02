"""Property-based tests for pyqenc/phases/measure.py.

Each test is tagged with the feature and property it validates.
Run with: uv run python -m pytest tests/test_measure_properties.py
"""

# Feature: standalone-measure

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from pyqenc.phases.measure import _screenshot_filename, _screenshot_timestamps_count

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_st_duration = st.floats(min_value=1e-3, max_value=1e9, allow_nan=False, allow_infinity=False)
_st_count    = st.integers(min_value=1, max_value=1000)


# ---------------------------------------------------------------------------
# Property 1: Screenshot timestamp distribution
# ---------------------------------------------------------------------------


@settings(max_examples=500)
@given(duration=_st_duration, count=_st_count)
def test_screenshot_timestamps_count_distribution(duration: float, count: int) -> None:
    """Property 1: Screenshot timestamp distribution.

    For any duration > 0 and count >= 1:
    - Result has exactly ``count`` values.
    - All values are strictly in ``(0, duration)``.
    - Values are evenly spaced with step ``duration / (count + 1)``.

    Validates: Requirement 7.2
    # Feature: standalone-measure, Property 1: Screenshot timestamp distribution
    """
    timestamps = _screenshot_timestamps_count(duration, count)

    # Exactly count values
    assert len(timestamps) == count

    step = duration / (count + 1)

    for i, t in enumerate(timestamps, start=1):
        # All strictly interior to (0, duration)
        assert t > 0.0,        f"timestamp {t} not > 0 (duration={duration}, count={count})"
        assert t < duration,   f"timestamp {t} not < duration={duration} (count={count})"

        # Evenly spaced: t_i == i * step
        expected = i * step
        assert abs(t - expected) < 1e-9, (
            f"timestamp[{i}]={t} != expected {expected} "
            f"(duration={duration}, count={count}, step={step})"
        )


# ---------------------------------------------------------------------------
# Property 2: Screenshot filename sort order
# ---------------------------------------------------------------------------

_st_timestamp = st.floats(min_value=0.0, max_value=359999.999, allow_nan=False, allow_infinity=False)
_st_stem      = st.text(min_size=1, max_size=64, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_-"))


@settings(max_examples=500)
@given(timestamps=st.lists(_st_timestamp, min_size=1, max_size=100, unique=True), stem=_st_stem)
def test_screenshot_filename_sort_order(timestamps: list[float], stem: str) -> None:
    """Property 2: Screenshot filename sort order.

    For any list of distinct timestamps, lexicographic sort of the generated
    filenames matches the numeric sort of the original timestamps.

    Validates: Requirement 7.5
    # Feature: standalone-measure, Property 2: Screenshot filename sort order
    """
    filenames = [_screenshot_filename(t, stem) for t in timestamps]

    numeric_order  = sorted(timestamps)
    filename_order = [_screenshot_filename(t, stem) for t in numeric_order]
    lexicographic  = sorted(filenames)

    assert lexicographic == filename_order, (
        f"Lexicographic filename order does not match numeric timestamp order.\n"
        f"Timestamps (sorted): {numeric_order}\n"
        f"Expected filenames:  {filename_order}\n"
        f"Got (lex sorted):    {lexicographic}"
    )
