"""The splitter — causal-Hampel descent over feature traces (pure).

The validated boundary-selection method (chunking-rework research,
2026-11-22/23; the user's full-source visual inspection as ground truth):
a top-down descent that splits each chunk at its strongest raw
content-score frame whose local significance clears the bar — a causal
Hampel identifier (each frame judged against the frames strictly before
it, in units of that neighborhood's own robust scale) with two raw
content-score guards: an absolute floor and a jump-from-centered-average
requirement that reject panning drifts and ramp onsets. A chunk whose
every candidate fails is statistically uniform and stays whole — the
descent's natural stop; there are no forced splits.

Both modes share this one method. The fast mode feeds it the content
trace alone; the slow mode adds per-frame codec traces (psnr/ssim/
log-bits from the probe encode) — a candidate may fire on any feature's
scale-free Hampel z (the codec features carry no raw guards; their own
noise scale is the gate).

The module is pure: no I/O, no config. The chunking phase owns the
scoring providers, the PTS index, and persistence.
"""
# CHerSun 2026

from collections import deque
from collections.abc import Sequence

from pyqenc.constants import HAMPEL_LOOKBACK
from pyqenc.models import SceneBoundary

SENSITIVITY: float = 3.0
"""The significance bar (dimensionless z units, algorithm constant — not
config). A candidate fires when its Hampel z clears this on any feature.
The z is measured against a frame's own neighborhood, so a real cut in
a busy one (motion keeping every content score high) scores low; the
bar must stay within their reach."""

HAMPEL_SCALE: float = 1.4826
"""MAD-to-sigma conversion for Gaussian-like noise (1/Φ⁻¹(¾))."""

SCALE_FLOOR: float = 1e-9
"""Lower clamp on the local robust scale — a constant neighborhood makes
any nonzero deviation maximal rather than undefined."""

MIN_CONTENT_SCORE: float = 15.0
"""Raw content-score floor a content-feature candidate must clear. A
guard, not a ruler: it bounds which frames may even be considered, in the
scorer's own units."""

JUMP_HALF_WIDTH: int = 3
"""Half-width of the centered average for the jump guard (the current
frame ±3 — a 7-frame window)."""

MIN_JUMP: float = 10.0
"""Minimum |score − centered average| for a content-feature candidate —
the anti-panning guard. A true cut towers over both neighbors; a ramp's
neighbors are already climbing, so the centered average absorbs its peak
and the jump collapses."""


def split_boundaries(
    content_trace:    list[float],
    pts_seconds:      list[float],
    min_scene_length: int,
    extra_traces:     Sequence[list[float]] = (),
) -> list[SceneBoundary]:
    """Select scene boundaries by causal-Hampel descent over the traces.

    Top-down: each chunk is offered its frames in descending raw content
    score; the first candidate at least ``min_scene_length`` frames from
    both chunk edges that clears the raw content guards and reaches
    ``SENSITIVITY`` on any feature's Hampel z splits the chunk, and both
    children repeat. A chunk with no qualifying candidate is statistically
    uniform and stays whole at any length. Every boundary is the exact
    frame that fired, carrying that frame's real PTS.

    Args:
        content_trace:    Per-frame content scores — the primary feature:
                          the offer order and the raw guards are defined in
                          its units (frame 0 scores 0; the scorer has no
                          predecessor).
        pts_seconds:      Per-frame presentation timestamps in seconds,
                          ascending — boundary timestamps are taken from it
                          exactly, never from an idealized frame grid.
        min_scene_length: Hard minimum segment length in FRAMES — the only
                          length ruler.
        extra_traces:     Additional per-frame features (slow mode's
                          psnr/ssim/log-bits), aligned with the content
                          trace; a candidate may fire on their scale-free
                          Hampel z instead of the content feature's.

    Returns:
        Interior boundaries in frame order — the stream-start boundary
        (frame 0) is deliberately absent; the caller owns it.
    """
    n = len(content_trace)
    if n < 2 * min_scene_length or n != len(pts_seconds):
        return []

    significance = _combined_significance(content_trace, extra_traces)

    boundaries: list[int] = []
    pending: deque[tuple[int, int]] = deque([(0, n)])  # [start, end) frames
    while pending:
        start, end = pending.popleft()
        offset = _split_offset(
            content_trace[start:end], significance[start:end], min_scene_length,
        )
        if offset is None:
            continue
        frame = start + offset
        boundaries.append(frame)
        pending.append((start, frame))
        pending.append((frame, end))

    return [
        SceneBoundary(frame=frame, timestamp_seconds=pts_seconds[frame])
        for frame in sorted(boundaries)
    ]


# ---------------------------------------------------------------------------
# Local significance — the causal Hampel identifier and its guards
# ---------------------------------------------------------------------------

def causal_hampel_z(
    values:  list[float],
    lookback: int = HAMPEL_LOOKBACK,
) -> list[float]:
    """Per-frame Hampel z against the frames strictly before each frame.

    Direction-biased like the content scorer itself (which measures
    change against the previous frame): a single-frame cut is a huge
    outlier of its causal neighborhood, while a ramp with history
    self-cancels — the local median tracks the climb, each frame sits
    close to its predecessors. The scale is re-estimated per
    neighborhood: quiet regions auto-sensitize, busy ones
    auto-desensitize. Frames with fewer than two predecessors score 0.

    Args:
        values:   Per-frame values of one feature.
        lookback: Frames of causal neighborhood.

    Returns:
        The z trace, aligned with ``values`` by frame index.
    """
    z: list[float] = []
    for i in range(len(values)):
        window = values[max(0, i - lookback):i]
        if len(window) < 2:
            z.append(0.0)
            continue
        ordered = sorted(window)
        median = ordered[len(ordered) // 2]
        devs = sorted(abs(v - median) for v in window)
        mad = devs[len(devs) // 2]
        scale = max(HAMPEL_SCALE * mad, SCALE_FLOOR)
        z.append(abs(values[i] - median) / scale)
    return z


def centered_jump(values: list[float], half: int = JUMP_HALF_WIDTH) -> list[float]:
    """Per-frame |value − mean(centered window)| in the feature's raw units.

    Design note (validated alternative, kept as a comment): the simpler
    neighbor-gap guard — ``x − max(prev, next) > 10`` — produced an
    IDENTICAL boundary set on the visual-inspection validation source,
    with the raw content floor subsumed. Not adopted: a 2-neighbor gate
    can swallow fast blends (~3-frame transitions that should break),
    while this 7-frame centered average still sees them. Revisit if blend
    misses ever show up; swap in by replacing this function's zeroing
    condition.

    Args:
        values: Per-frame values.
        half:   Half-width; the window is ``i − half .. i + half``.

    Returns:
        The absolute departure from the centered average, aligned by frame.
    """
    jumps: list[float] = []
    for i in range(len(values)):
        window = values[max(0, i - half): i + half + 1]
        jumps.append(abs(values[i] - sum(window) / len(window)))
    return jumps


def _combined_significance(
    content_trace: list[float],
    extra_traces:  Sequence[list[float]],
) -> list[float]:
    """The per-frame significance the descent gates on.

    The content feature's Hampel z is zeroed unless the frame clears both
    raw guards (the absolute score floor and the centered-average jump);
    extra features contribute their ungated, scale-free z. A frame's
    significance is the max over features.

    Args:
        content_trace: The primary feature (its raw guards).
        extra_traces:  Additional features, aligned with the content trace.

    Returns:
        The combined significance trace.
    """
    z = causal_hampel_z(content_trace)
    jumps = centered_jump(content_trace)
    combined: list[float] = [
        z[i] if value >= MIN_CONTENT_SCORE and jumps[i] >= MIN_JUMP else 0.0
        for i, value in enumerate(content_trace)
    ]
    for trace in extra_traces:
        feature_z = causal_hampel_z(trace)
        combined = [max(a, b) for a, b in zip(combined, feature_z)]
    return combined


def _split_offset(
    chunk_content:    list[float],
    significance:     list[float],
    min_scene_length: int,
) -> int | None:
    """The first qualifying split offset of a chunk, or ``None``.

    Frames are offered in descending raw content score; the first one at
    least ``min_scene_length`` frames from both edges whose significance
    clears ``SENSITIVITY`` wins.

    Args:
        chunk_content:    The chunk's content scores.
        significance:     The chunk's combined significance (aligned).
        min_scene_length: Hard minimum segment length in frames.

    Returns:
        The winning 0-based offset into the chunk, or ``None`` when the
        chunk is statistically uniform.
    """
    length = len(chunk_content)
    order = sorted(range(length), key=lambda o: -chunk_content[o])
    for offset in order:
        if not (min_scene_length <= offset < length - min_scene_length):
            continue
        if significance[offset] >= SENSITIVITY:
            return offset
    return None
