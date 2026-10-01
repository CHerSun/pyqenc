"""Per-frame PTS timestamp file (``timestamps.txt``) parsing.

``ExtractionPhase`` writes the source's per-frame presentation timestamps in
mkvextract ``timecodes_v2`` format: a ``# timestamp format v2`` header line
followed by one millisecond value per frame, ascending. mkvextract appends one
trailing entry after the last frame — the stream end timestamp, written at
full (fractional-ms) precision — and the project's own ffprobe fallback writer
follows the same N-frames-plus-trailing shape. The frame count is therefore
the number of frame lines, with a fractional trailing entry recognised as the
end-of-stream marker rather than a frame.

A total involves no windowing, so the format's millisecond rounding cannot
cause boundary-attribution errors — this is the exact, free source frame
count the frame-preservation invariant builds on (spec
``2026-09-25 file-stream-model``).
"""
# CHerSun 2026

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMESTAMP_FORMAT_HEADER = "# timestamp format v2"
"""The timecodes_v2 header line mkvextract (and our ffprobe fallback) writes."""


def parse_timestamps(path: Path) -> list[float]:
    """Parse a ``timestamps.txt`` into its ascending millisecond values.

    Values parse as floats: frame lines are millisecond integers, but the
    trailing end-of-stream entry is written at full precision
    (e.g. ``397313.708333``) and must survive parsing.

    Args:
        path: Path to the timestamp file.

    Returns:
        The per-frame PTS values in milliseconds (plus the trailing
        end-of-stream entry when present), as written (ascending).

    Raises:
        ValueError: When the file is empty, lacks the header, or contains an
                    unparseable data line.
        OSError: When the file cannot be read.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    data_lines = [line.strip() for line in lines if line.strip()]
    if not data_lines:
        raise ValueError(f"Timestamp file is empty: {path}")
    if data_lines[0] != _TIMESTAMP_FORMAT_HEADER:
        raise ValueError(
            f"Timestamp file {path} lacks the '{_TIMESTAMP_FORMAT_HEADER}' header"
        )

    values: list[float] = []
    for line in data_lines[1:]:
        try:
            values.append(float(line))
        except ValueError as exc:
            raise ValueError(f"Unparseable timestamp line in {path}: {line!r}") from exc
    return values


def count_frames(path: Path) -> int | None:
    """Return the total frame count recorded in a ``timestamps.txt``.

    The count is the number of frame lines — a trailing entry with a
    fractional-millisecond value is mkvextract's end-of-stream marker, not a
    frame. This is the exact, free source frame count; ``None`` means the
    file is absent or unreadable — callers fall back to a null-count pass.

    Args:
        path: Path to the timestamp file.

    Returns:
        The total frame count, or ``None`` when unavailable.
    """
    try:
        values = parse_timestamps(path)
    except (OSError, ValueError) as exc:
        logger.debug("Could not count frames from %s: %s", path, exc)
        return None
    if values and values[-1] != int(values[-1]):
        return len(values) - 1  # trailing end-of-stream marker, not a frame
    return len(values)
