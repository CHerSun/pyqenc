"""Calling-process priority control."""

# CHerSun 2026

import logging
import sys

import psutil

logger = logging.getLogger(__name__)


def lower_process_priority() -> None:
    """Set the calling process's priority to below normal.

    Encoding work must never starve the user's other work; child
    processes inherit the lowered priority. Failures are logged and
    never raised.
    """
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
        logger.debug("Process priority set to below normal")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to set process priority: {e}")
