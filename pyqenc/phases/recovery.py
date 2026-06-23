"""Shared low-level recovery helpers for the pyqenc pipeline.

Contains helpers shared across multiple phase modules and dataclasses used
by ``split_chunks`` (called from ``ChunkingPhase._execute_chunking``).

Per-phase recovery logic lives in the respective phase objects:
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

from pyqenc.models import ChunkMetadata, SceneBoundary
from pyqenc.state import ArtifactState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses used by split_chunks (called from ChunkingPhase._execute_chunking)
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
    """Result of chunking phase recovery (used by ``split_chunks``).

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
