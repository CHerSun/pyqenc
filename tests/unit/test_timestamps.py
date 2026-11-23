"""Tests for the per-frame PTS index reader (``pyqenc.utils.timestamps``).

The index is the pipeline's frame axis and frame count: extraction writes
one line per frame and mkvextract appends one end-of-stream entry after
the last frame. Only that entry may be dropped when reading — a frame
line never is — and the count and the axis must always agree.
"""

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyqenc.utils.timestamps import count_frames, parse_frame_timestamps, parse_timestamps

_HEADER = "# timestamp format v2"
"""The header line mkvextract (and the ffprobe fallback) writes."""

_FRAMES = st.lists(st.integers(min_value=0, max_value=10**9), min_size=1, max_size=200).map(sorted)
"""Ascending per-frame millisecond lines."""

_END_OF_STREAM_MICRO_MS = st.integers(min_value=1, max_value=10**12).filter(
    lambda value: value % 1000 != 0,
)
"""mkvextract's end-of-stream entry in micro-milliseconds, never a whole
millisecond — the fractional part is what marks it."""

_SCENARIOS = st.tuples(_FRAMES, st.one_of(st.none(), _END_OF_STREAM_MICRO_MS))


def _write_index(path: Path, frames: list[int], end_of_stream: int | None) -> Path:
    """Write a ``timestamps.txt``: one millisecond line per frame, then
    optionally the end-of-stream entry."""
    lines = [_HEADER, *(str(ms) for ms in frames)]
    if end_of_stream is not None:
        lines.append(f"{end_of_stream / 1000:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestParseFrameTimestamps:
    def test_happy_path_drops_the_end_of_stream_entry(self, tmp_path: Path) -> None:
        """Bug prevented: the end-of-stream entry read as a frame — mkvextract's
        trailing entry is not a frame and must not appear in the axis (the
        axis would hold one frame more than the source has)."""
        index = _write_index(tmp_path / "timestamps.txt", [0, 42, 83], 125_500)
        assert parse_frame_timestamps(index) == [0.0, 42.0, 83.0]

    def test_happy_path_keeps_every_line_without_the_entry(self, tmp_path: Path) -> None:
        """The ffprobe fallback writes frame lines only: nothing may be dropped."""
        index = _write_index(tmp_path / "timestamps.txt", [0, 42, 83], None)
        assert parse_frame_timestamps(index) == [0.0, 42.0, 83.0]

    @given(scenario=_SCENARIOS)
    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_property_frame_lines_survive_and_the_count_agrees(
        self, tmp_path: Path, scenario: tuple[list[int], int | None],
    ) -> None:
        """Invariant: every frame line is kept, only the end-of-stream entry
        is dropped, and the frame count is the number of frame lines — so
        the frame axis and the frame count the pipeline derives from the
        same index can never disagree."""
        frames, end_of_stream = scenario
        index = _write_index(tmp_path / "timestamps.txt", frames, end_of_stream)
        assert parse_frame_timestamps(index) == [float(ms) for ms in frames]
        assert count_frames(index) == len(frames)
        assert len(parse_timestamps(index)) == len(frames) + (0 if end_of_stream is None else 1)


class TestUnusableIndex:
    @pytest.mark.parametrize(
        "text",
        ["", "no header\n42\n", f"{_HEADER}\nnot a number\n"],
        ids=["empty", "missing-header", "unparseable-line"],
    )
    def test_unusable_index_is_an_error(self, tmp_path: Path, text: str) -> None:
        """Bug prevented: an unusable index read as zero frames — an empty
        file, a missing header or a bad line is an error, never an empty
        axis (a zero frame count would silently pass every frame check)."""
        index = tmp_path / "timestamps.txt"
        index.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            parse_frame_timestamps(index)

    def test_count_of_an_absent_index_is_unknown(self, tmp_path: Path) -> None:
        """An absent index is unknown, not zero — the probe's null-count path."""
        assert count_frames(tmp_path / "absent.txt") is None
