"""Property suite for the causal-Hampel descent (``pyqenc.chunking_split``).

Each test is named after a real failure mode from the chunking research:
segment-length violations, the frame-preservation telescope, grid-time
frame leakage across windows, panning-ramp and ramp-onset false cuts, and
endless splitting.
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from pyqenc.chunking_split import (
    MIN_CONTENT_SCORE,
    split_boundaries,
)

_PERIOD = 1001.0 / 24000
"""The 23.976 fps frame period — a non-integer ms grid (frame times are
ms-quantized), the shape that made grid-time anchoring leak frames."""

_FRAME_COUNT = st.integers(min_value=3, max_value=240)

_NOISE = st.floats(
    min_value=0.0, max_value=MIN_CONTENT_SCORE - 1.0,
    allow_nan=False, allow_infinity=False,
)
_SPIKE = st.floats(
    min_value=MIN_CONTENT_SCORE + 5.0, max_value=100.0,
    allow_nan=False, allow_infinity=False,
)


@st.composite
def _spiky_trace(draw: st.DrawFn) -> list[float]:
    """A noise floor under the raw guard plus occasional cut spikes."""
    n = draw(_FRAME_COUNT)
    trace = [draw(_NOISE) for _ in range(n)]
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        trace[draw(st.integers(min_value=0, max_value=n - 1))] = draw(_SPIKE)
    return trace


@st.composite
def _scenario(draw: st.DrawFn) -> tuple:
    """A spiky trace + a non-uniform ascending PTS grid + the ruler."""
    trace = draw(_spiky_trace())
    n = len(trace)
    steps = draw(st.lists(
        st.floats(min_value=1.0, max_value=200.0),
        min_size=n - 1, max_size=n - 1,
    ))
    pts: list[float] = [0.0]
    for step in steps:
        pts.append(pts[-1] + step)
    pts = [round(value * 1000) / 1000 for value in pts]  # ms-quantized
    min_scene_length = draw(st.integers(min_value=2, max_value=30))
    return trace, pts, min_scene_length


def _frames(result) -> list[int]:
    return [boundary.frame for boundary in result]


# ---------------------------------------------------------------------------
# Happy path — explicit, readable examples
# ---------------------------------------------------------------------------

class TestSplitBoundariesHappyPath:
    def test_single_frame_cut_fires_at_the_cut_frame(self):
        """A clean single-frame cut produces one boundary at that frame."""
        n = 2400
        cut_at = 1200
        trace = [2.0] * n
        trace[cut_at] = 70.0
        pts = [round(i * _PERIOD, 6) for i in range(n)]
        result = split_boundaries(trace, pts, min_scene_length=15)
        assert _frames(result) == [cut_at]
        assert result[0].timestamp_seconds == pts[cut_at]

    def test_extra_feature_fires_without_any_content_spike(self):
        """The slow shape: a codec-level step fires with no content cut."""
        n = 2400
        content = [2.0] * n                      # quiet content trace
        codec = [5.0] * 1200 + [40.0] * (n - 1200)  # a difficulty step
        pts = [round(i * _PERIOD, 6) for i in range(n)]
        result = split_boundaries(
            content, pts, min_scene_length=15, extra_traces=[codec],
        )
        assert _frames(result) == [1200]


# ---------------------------------------------------------------------------
# Properties — each named after a real failure
# ---------------------------------------------------------------------------

class TestSplitBoundariesProperties:
    @given(scenario=_scenario())
    def test_boundaries_respect_min_scene_length(self, scenario):
        """Bug prevented: segments below the hard minimum — every accepted
        boundary keeps min_scene_length FRAMES from the stream start, from
        every other boundary, and the last segment clears it too (a source
        too short to host two minimum segments legitimately stays whole)."""
        trace, pts, min_len = scenario
        assume(len(trace) >= 2 * min_len)
        frames = _frames(split_boundaries(trace, pts, min_len))
        gaps = frames + [len(trace)]
        previous = 0
        for frame in gaps:
            assert frame - previous >= min_len, (previous, frame)
            previous = frame

    @given(scenario=_scenario())
    def test_boundaries_are_frames_telescoping_to_the_source(self, scenario):
        """Bug prevented: non-frame boundaries — records must be real frame
        indices, strictly ascending (consecutive differences are the chunk
        counts that telescope to the source total)."""
        trace, pts, min_len = scenario
        frames = _frames(split_boundaries(trace, pts, min_len))
        assert frames == sorted(set(frames))
        assert all(0 <= frame < len(trace) for frame in frames)
        assert 0 not in frames, "the stream start belongs to the caller"

    @given(scenario=_scenario())
    def test_timestamps_are_exact_pts_anchored(self, scenario):
        """Bug prevented: grid-time anchoring — on ms-quantized sources an
        idealized frame×period timestamp slips the boundary frame into a
        neighbor's window and breaks frame preservation; every stored
        timestamp must equal the frame's real PTS, bit for bit."""
        trace, pts, min_len = scenario
        result = split_boundaries(trace, pts, min_len)
        for boundary in result:
            assert boundary.timestamp_seconds == pts[boundary.frame]

    @given(scenario=_scenario())
    @settings(deadline=2000)
    def test_descent_terminates_on_any_input(self, scenario):
        """Bug prevented: endless splitting — the descent must terminate on
        any input (every split strictly shortens both children; a chunk
        with no qualifying candidate is terminal by construction)."""
        trace, pts, min_len = scenario
        result = split_boundaries(trace, pts, min_len)
        assert isinstance(result, list)

    @given(scenario=_scenario())
    def test_output_is_deterministic(self, scenario):
        """Bug prevented: order-dependent tie handling — the same inputs
        must always produce the same boundary set."""
        trace, pts, min_len = scenario
        first = split_boundaries(trace, pts, min_len)
        second = split_boundaries(trace, pts, min_len)
        assert _frames(first) == _frames(second)

    def test_panning_ramp_yields_no_boundary(self):
        """Bug prevented: panning false cuts — a steady ramp self-cancels
        in the causal Hampel (each frame sits close to its predecessors),
        and the centered-average jump collapses; no boundary may fire."""
        n = 2400
        trace = [min(50.0, 2.0 + i * 0.02) for i in range(n)]
        pts = [round(i * _PERIOD, 6) for i in range(n)]
        assert split_boundaries(trace, pts, min_scene_length=15) == []

    def test_ramp_onset_from_quiet_yields_no_boundary(self):
        """Bug prevented: ramp-onset false cuts — a gradual climb from a
        flat baseline collapses the local scale so Hampel z explodes, but
        the centered-average jump stays small; the guards must reject it
        (the exact false-positive class of the visual inspection)."""
        n = 2400
        trace = [2.0] * 1200 + [2.0 + i * 0.02 for i in range(n - 1200)]
        pts = [round(i * _PERIOD, 6) for i in range(n)]
        assert split_boundaries(trace, pts, min_scene_length=15) == []

    def test_quiet_noise_yields_no_boundary(self):
        """Bug prevented: noise carving — a homogeneous trace under the raw
        content guard has no candidates at all; nothing may fire."""
        n = 2400
        trace = [3.0] * n
        pts = [round(i * _PERIOD, 6) for i in range(n)]
        assert split_boundaries(trace, pts, min_scene_length=15) == []


# ---------------------------------------------------------------------------
# Degenerate inputs — graceful, not fatal
# ---------------------------------------------------------------------------

class TestSplitBoundariesDegenerate:
    def test_too_short_trace_yields_no_boundaries(self):
        """A trace that cannot host two minimum segments stays whole."""
        pts = [round(i * _PERIOD, 6) for i in range(20)]
        trace = [2.0] * 19 + [70.0]
        assert split_boundaries(trace, pts, min_scene_length=15) == []

    def test_mismatched_pts_yields_no_boundaries(self):
        """A trace and PTS axis of different lengths is a caller bug —
        empty result, never silent misalignment."""
        pts = [round(i * _PERIOD, 6) for i in range(10)]
        assert split_boundaries([2.0] * 11, pts, min_scene_length=2) == []
