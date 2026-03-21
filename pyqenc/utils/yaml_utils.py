"""Atomic YAML write utility for the pyqenc pipeline.

All YAML writes in the pipeline go through ``write_yaml_atomic`` to ensure
that a crash during writing never leaves a partial file on disk.  The caller
passes the final target path and a data dict; temp-file management is handled
internally using the ``.tmp``-then-rename protocol.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from pyqenc.constants import TEMP_SUFFIX

logger = logging.getLogger(__name__)


def write_yaml_atomic(path: Path, data: dict) -> None:
    """Write *data* as YAML to *path* using the ``.tmp``-then-rename protocol.

    Writes to a sibling temp file ``<path.stem>.tmp`` first, then renames it
    to *path* on success.  If any exception occurs the temp file is deleted so
    no partial file is left on disk.

    Args:
        path: Final destination path for the YAML file.
        data: Data to serialise as YAML.

    Raises:
        OSError: If the write or rename fails for reasons other than a
                 cross-device move (which is handled transparently via
                 copy-then-delete).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f"{path.stem}{TEMP_SUFFIX}"

    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh, allow_unicode=True, sort_keys=False)
        tmp_path.replace(path)
        logger.debug("Wrote YAML atomically: %s", path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
