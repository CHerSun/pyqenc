"""Per-frame PTS timestamp file (``timestamps.txt``) parsing.

``ExtractionPhase`` writes the source's per-frame presentation timestamps in
mkvextract ``timecodes_v2`` format: a ``# timestamp format v2`` header line
followed by one integer millisecond value per frame, ascending. The total
data-line count is the exact source frame count — the primary source of the
frame-preservation invariant (spec ``2026-09-25 file-stream-model``, Req 9.2).
A total involves no windowing, so the format's millisecond rounding cannot
cause boundary-attribution errors.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMESTAMP_FORMAT_HEADER = "# timestamp format v2"
"""The timecodes_v2 header line mkvextract (and our ffprobe fallback) writes."""


def parse_timestamps(path: Path) -> list[int]:
    """Parse a ``timestamps.txt`` into its ascending millisecond values.

    Args:
        path: Path to the timestamp file.

    Returns:
        The per-frame PTS values in milliseconds, as written (ascending).

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

    values: list[int] = []
    for line in data_lines[1:]:
        try:
            values.append(int(line))
        except ValueError as exc:
            raise ValueError(f"Unparseable timestamp line in {path}: {line!r}") from exc
    return values


def count_frames(path: Path) -> int | None:
    """Return the total frame count recorded in a ``timestamps.txt``.

    The count is the number of data lines (the header is not a frame). This is
    the exact, free source frame count; ``None`` means the file is absent or
    unreadable — callers fall back to a null-count ffmpeg pass.

    Args:
        path: Path to the timestamp file.

    Returns:
        The total frame count, or ``None`` when unavailable.
    """
    try:
        return len(parse_timestamps(path))
    except (OSError, ValueError) as exc:
        logger.debug("Could not count frames from %s: %s", path, exc)
        return None
