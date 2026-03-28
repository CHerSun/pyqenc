"""Shared low-level recovery helpers for the pyqenc pipeline.

This module retains only the helpers that are shared across multiple modules
and the dataclasses used by legacy phase functions (``chunk_video``,
``split_chunks``, etc.) that have not yet been migrated to the phase-object
model.

Per-phase recovery logic has been moved into the respective phase objects:
- ``ExtractionPhase._recover()``  in ``pyqenc/phases/extraction.py``
- ``ChunkingPhase._recover()``    in ``pyqenc/phases/chunking.py``
- ``EncodingPhase._recover()``    in ``pyqenc/phases/encoding.py``
  (via ``_recover_encoding_attempts``)
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pyqenc.constants import (
    CHUNK_NAME_PATTERN,
    RANGE_SEPARATOR,
    TEMP_SUFFIX,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import ChunkMetadata, SceneBoundary
from pyqenc.state import ArtifactState, ChunkSidecar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared low-level helpers (kept for use by legacy functions and phase objects)
# ---------------------------------------------------------------------------

def _cleanup_tmp_files(directory: Path) -> None:
    """Delete any leftover ``.tmp`` files in *directory*.

    Called at the start of each phase to remove partial files from a
    previously interrupted run.  Logs a warning for each file removed.

    Args:
        directory: Directory to scan for ``.tmp`` files.
    """
    if not directory.exists():
        return
    for tmp_file in directory.glob(f"*{TEMP_SUFFIX}"):
        try:
            tmp_file.unlink()
            logger.warning(
                "Removed leftover temp file from previous interrupted run: %s",
                tmp_file,
            )
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", tmp_file, exc)


def _parse_chunk_timestamps(chunk_id: str) -> tuple[float, float]:
    """Parse start and end timestamps (in seconds) from a chunk_id stem.

    Chunk IDs have the form ``HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm`` where ``꞉`` is
    ``TIME_SEPARATOR_SAFE`` and ``․`` is ``TIME_SEPARATOR_MS``.

    Args:
        chunk_id: Timestamp-range chunk identifier.

    Returns:
        ``(start_seconds, end_seconds)`` tuple.

    Raises:
        ValueError: If the chunk_id does not match the expected format.
    """
    parts = chunk_id.split(RANGE_SEPARATOR, 1)
    if len(parts) != 2:
        raise ValueError(f"Expected exactly one '{RANGE_SEPARATOR}' in chunk_id: {chunk_id!r}")

    def _ts_to_seconds(ts: str) -> float:
        hms = ts.split(TIME_SEPARATOR_SAFE)
        if len(hms) != 3:
            raise ValueError(f"Expected HH꞉MM꞉SS in timestamp: {ts!r}")
        h, m, s_ms = hms
        s_ms = s_ms.replace(TIME_SEPARATOR_MS, ".")
        return int(h) * 3600 + int(m) * 60 + float(s_ms)

    return _ts_to_seconds(parts[0]), _ts_to_seconds(parts[1])


# ---------------------------------------------------------------------------
# Dataclasses used by legacy chunking functions (chunk_video, split_chunks)
# ---------------------------------------------------------------------------

@dataclass
class ChunkRecovery:
    """Recovery state for a single chunk artifact.

    Attributes:
        chunk_id:  Chunk identifier (timestamp-range stem).
        path:      Path to the chunk ``.mkv`` file.
        state:     ``ABSENT`` / ``ARTIFACT_ONLY`` / ``COMPLETE``.
        metadata:  ``ChunkMetadata`` loaded from sidecar (``COMPLETE`` only).
    """

    chunk_id: str
    path:     Path
    state:    ArtifactState
    metadata: ChunkMetadata | None = None


@dataclass
class ChunkingRecovery:
    """Result of chunking phase recovery (used by legacy ``split_chunks``).

    Attributes:
        scenes:   Scene boundaries loaded from ``chunking.yaml``.
        chunks:   Per-chunk recovery state, keyed by ``chunk_id``.
        pending:  Chunk IDs that still need work.
        did_work: Set to ``True`` by the phase after it performs actual work.
    """

    scenes:   list[SceneBoundary]      = field(default_factory=list)
    chunks:   dict[str, ChunkRecovery] = field(default_factory=dict)
    pending:  list[str]                = field(default_factory=list)
    did_work: bool                     = False
