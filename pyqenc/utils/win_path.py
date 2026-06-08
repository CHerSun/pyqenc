"""Cross-platform helpers for file operations on paths that may exceed the
Windows 260-character MAX_PATH limit.

On Windows, ``pathlib.Path`` operations silently fail for paths longer than
260 chars because the underlying Win32 calls receive the plain path string
rather than the ``\\?\``-prefixed extended-length form.  ``os.*`` functions
accept the extended prefix and bypass the limit.

On Linux (and other non-Windows platforms) the helpers delegate directly to
``os.*`` without any prefix — the 260-char restriction does not exist there.

Usage::

    from pyqenc.utils.win_path import lp_exists, lp_rename, lp_unlink

    if lp_exists(tmp_path):
        lp_rename(tmp_path, final_path)
    else:
        lp_unlink(tmp_path, missing_ok=True)
"""
# CHerSun 2026

from __future__ import annotations

import os
import sys
from pathlib import Path

_WINDOWS = sys.platform == "win32"
"""``True`` if the current platform is Windows."""
_EXT_PREFIX = chr(92) * 2 + "?" + chr(92)
"""The ``\\\\?\\`` extended-length path prefix for Windows (4 chars, no f-string to avoid any escape ambiguity)."""


def _ext(path: Path) -> str:
    """Return the extended-length string form of *path* on Windows.

    Converts the path to an absolute string and prepends ``\\?\\`` if not
    already present.  On non-Windows platforms returns ``str(path)`` unchanged.

    Args:
        path: Absolute or relative ``Path`` to convert.

    Returns:
        String suitable for passing to ``os.*`` functions.
    """
    if not _WINDOWS:
        return str(path)
    s = str(path.resolve())
    if s.startswith(_EXT_PREFIX):
        return s
    return _EXT_PREFIX + s


def lp_exists(path: Path) -> bool:
    """Return ``True`` if *path* exists, bypassing MAX_PATH on Windows.

    Equivalent to ``path.exists()`` but works for paths longer than 260
    characters on Windows.

    Args:
        path: Path to check.

    Returns:
        ``True`` if the path exists, ``False`` otherwise.
    """
    return os.path.exists(_ext(path))


def lp_rename(src: Path, dst: Path) -> None:
    """Rename *src* to *dst*, bypassing MAX_PATH on Windows.

    Equivalent to ``src.replace(dst)`` but works for paths longer than 260
    characters on Windows.  Overwrites *dst* if it already exists (matches
    the behaviour of ``Path.replace``).

    Args:
        src: Source path.
        dst: Destination path.

    Raises:
        OSError: If the rename fails for any reason other than path length.
    """
    os.replace(_ext(src), _ext(dst))


def lp_unlink(path: Path, *, missing_ok: bool = False) -> None:
    """Delete *path*, bypassing MAX_PATH on Windows.

    Equivalent to ``path.unlink(missing_ok=missing_ok)`` but works for paths
    longer than 260 characters on Windows.

    Args:
        path:       Path to delete.
        missing_ok: If ``True``, suppress ``FileNotFoundError``.

    Raises:
        FileNotFoundError: If the path does not exist and *missing_ok* is
            ``False``.
        OSError: If deletion fails for any other reason.
    """
    try:
        os.unlink(_ext(path))
    except FileNotFoundError:
        if not missing_ok:
            raise
