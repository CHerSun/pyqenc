"""Atomic YAML write and typed model load/save utilities for the pipeline.

All YAML writes in the pipeline go through ``write_yaml_atomic`` to ensure
that a crash during writing never leaves a partial file on disk.  The caller
passes the final target path and a data dict; temp-file management is handled
internally using the ``.tmp``-then-rename protocol.

``load_model`` / ``save_model`` build on it with the sidecar-model scaffold
every phase state class reuses: absent file → ``None``, parse/validation
failure → warning + ``None`` (the phase rebuilds), save → atomic dump.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel

from pyqenc.constants import TEMP_SUFFIX

logger = logging.getLogger(__name__)


def load_model[ModelT: BaseModel](path: Path, model_cls: type[ModelT]) -> ModelT | None:
    """Load a YAML sidecar file as the pydantic model *model_cls*.

    The uniform sidecar-load contract: an absent file and any parse or
    validation failure both mean "rebuild" — the error is logged as a
    warning and ``None`` is returned, never raised.  An empty document
    loads as the model's defaults (``safe_load`` yields ``None``).

    Args:
        path:      Path to the YAML file.
        model_cls: The pydantic model class to validate the data against.

    Returns:
        The validated model, or ``None`` when absent or unparseable.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return model_cls.model_validate(data or {})
    except (OSError, ValueError, yaml.YAMLError) as e:
        logger.warning("Could not load %s: %s", path, e)
        return None


def save_model(path: Path, model: BaseModel) -> None:
    """Persist a pydantic sidecar *model* to *path* atomically.

    ``None`` fields are excluded from the dump; parents are created by
    :func:`write_yaml_atomic`.

    Args:
        path:  Destination YAML file path.
        model: The pydantic model to serialise.
    """
    write_yaml_atomic(path, model.model_dump(exclude_none=True))
    logger.debug("Saved %s", path.name)


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
