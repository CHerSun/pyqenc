"""Shared low-level recovery helpers for the pyqenc pipeline.

Per-phase recovery logic lives in the respective phase objects:
- ``ExtractionPhase._recover()``  in ``pyqenc/phases/extraction.py``
- ``ChunkingPhase._recover()``    in ``pyqenc/phases/chunking.py``
- ``EncodingPhase._recover()``    in ``pyqenc/phases/encoding.py``
  (via ``_recover_encoding_attempts``)

The split-chunk recovery family (``ChunkingRecovery`` / ``ChunkRecovery``)
was deleted with the chunk files — chunk windows are derived objects with no
per-chunk on-disk state to recover.
"""
# CHerSun 2026

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
