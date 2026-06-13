from __future__ import annotations

import os
import sys
from pathlib import Path

_WINDOWS:    bool = sys.platform == "win32"
_EXT_PREFIX: str  = chr(92) * 2 + "?" + chr(92)   # \\?\  (4 chars)
_MAX_PATH:   int  = 260                             # Windows MAX_PATH limit


class LongPath(type(Path())):
    """A pathlib.Path subclass that transparently enables Windows extended-length paths.

    On Windows, ``os.fspath(long_path)`` (and therefore all Python file I/O,
    ``shutil.*``, etc.) returns the ``\\?\\``-prefixed absolute path string when
    the path length exceeds ``_MAX_PATH`` characters, bypassing the Win32 MAX_PATH
    limit.  On non-Windows platforms the behaviour is identical to plain ``Path``.

    Two string representations are intentionally different:

    - ``os.fspath(long_path)`` / ``long_path.__fspath__()``:
      returns the ``\\?\\``-prefixed absolute string on Windows for long paths.
      Used by Python's file I/O and ``shutil.*``.
    - ``str(long_path)``:
      returns the plain path string *without* any ``\\?\\`` prefix on all platforms.
      Use this when building ffmpeg subprocess command lists.

    Path composition (``/`` operator) is preserved: ``LongPath(base) / child``
    always returns a ``LongPath`` instance, not a plain ``Path``.

    Usage::

        work_dir = LongPath(args.work_dir)
        artifact = work_dir / "chunks" / "chunk_01.mkv"   # still LongPath
        artifact.mkdir(parents=True, exist_ok=True)        # uses __fspath__() — long-path safe
        cmd = ["ffmpeg", "-i", str(artifact), ...]         # uses __str__()   — no \\?\\ prefix
    """

    def __fspath__(self) -> str:
        """Return the filesystem path string, injecting the ``\\?\\`` prefix on Windows for long paths.

        On Windows: resolves to absolute path, prepends ``\\?\\`` when
        ``len(str(self)) > _MAX_PATH`` and the prefix is not already present.
        On non-Windows: returns ``str(self)`` unchanged (plain path, no prefix).

        Uses ``os.path.abspath`` (not ``self.resolve()``) to avoid recursive
        ``os.fspath()`` calls on Windows.

        Returns:
            Extended-length path string on Windows for long paths; plain string otherwise.
        """
        if not _WINDOWS:
            return str(self)
        s = os.path.abspath(str(self))
        if s.startswith(_EXT_PREFIX):
            return s
        if len(s) > _MAX_PATH:
            return _EXT_PREFIX + s
        return s

    def __str__(self) -> str:
        """Return the plain path string without any ``\\?\\`` prefix.

        Always returns the plain path regardless of length or platform.
        Use this when passing paths to ffmpeg or any other subprocess that
        does not understand the Windows extended-length prefix.

        Returns:
            Plain path string, never prefixed with ``\\?\\``.
        """
        return super().__str__()

    def __truediv__(self, key: str | Path) -> "LongPath":
        """Extend path with ``/`` operator, preserving ``LongPath`` type.

        Args:
            key: Path component to append.

        Returns:
            New ``LongPath`` instance with the component appended.
        """
        return LongPath(super().__truediv__(key))

    def __rtruediv__(self, key: str | Path) -> "LongPath":
        """Support ``str / LongPath`` composition, preserving ``LongPath`` type.

        Args:
            key: Left-hand path component.

        Returns:
            New ``LongPath`` instance.
        """
        return LongPath(super().__rtruediv__(key))
