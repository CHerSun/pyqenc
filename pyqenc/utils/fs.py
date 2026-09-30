"""Filesystem safety helpers for the pyqenc pipeline.

Every phase writes its outputs through the ``.tmp``-then-rename protocol, so
an interrupted run can leave ``.tmp`` crash remnants behind.  These helpers
own the startup cleanup of those remnants (the single shared implementation
every phase recovery calls) and the stat-with-fallback size probe.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from pathlib import Path

from pyqenc.constants import TEMP_SUFFIX

logger = logging.getLogger(__name__)


def remove_stale_tmp_files(directory: Path) -> None:
    """Remove leftover ``.tmp`` files under *directory* (recursive).

    Called at phase recovery, before artifacts are classified — a ``.tmp``
    crash remnant must never be mistaken for (or block) a real artifact.
    Removal failures are logged as warnings and never raised: cleanup is
    hygiene, not a run-critical step.

    Args:
        directory: Directory to scan recursively for ``*<TEMP_SUFFIX>``.
    """
    if not directory.exists():
        return
    for tmp in directory.rglob(f"*{TEMP_SUFFIX}"):
        try:
            tmp.unlink()
            logger.warning("Removed leftover temp file: %s", tmp)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", tmp, exc)


def remove_stale_tmp_file(path: Path) -> None:
    """Remove a single known leftover ``.tmp`` file *path* when present.

    The single-file variant of :func:`remove_stale_tmp_files` for sidecar
    files written via ``.tmp``-then-rename (``<name>.yaml.tmp`` siblings live
    at a fixed, pre-computable location).

    Args:
        path: The exact ``.tmp`` path to remove when it exists.
    """
    if not path.exists():
        return
    try:
        path.unlink()
        logger.warning("Removed leftover temp file: %s", path.name)
    except OSError as exc:
        logger.warning("Could not remove temp file %s: %s", path, exc)


def safe_stat_size(path: Path) -> int | None:
    """The file's size in bytes, or ``None`` when the stat fails.

    A pure probe with no logging side-effect — callers decide whether a
    ``None`` size deserves a warning.

    Args:
        path: The file to stat.

    Returns:
        The size in bytes, or ``None`` on :class:`OSError`.
    """
    try:
        return path.stat().st_size
    except OSError:
        return None
