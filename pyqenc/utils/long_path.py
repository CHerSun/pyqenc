import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema

_WINDOWS:    bool = sys.platform == "win32"
_EXT_PREFIX: str  = chr(92) * 2 + "?" + chr(92)   # \\?\  (4 chars)
_MAX_PATH:   int  = 260                             # Windows MAX_PATH limit


def _coerce_long_path(value: Path) -> LongPath:
    """Return ``value`` as a :class:`LongPath` (pydantic after-validator)."""
    return value if isinstance(value, LongPath) else LongPath(os.fspath(value))


class LongPath(type(Path())): # Platform-specific path type
    """A pathlib.Path subclass that transparently enables Windows extended-length paths.

    On Windows, ``os.fspath(long_path)`` (and therefore all Python file I/O,
    ``shutil.*``, etc.) returns the ``\\?\\``-prefixed absolute path string when
    the path length exceeds ``_MAX_PATH`` characters, bypassing the Win32 MAX_PATH
    limit.  On non-Windows platforms the behaviour is identical to plain ``Path``.

    Two string representations are intentionally different:

    - ``os.fspath(long_path)`` / ``long_path.__fspath__()``:
      returns the ``\\?\\``-prefixed absolute string on Windows for long paths.
      Used by Python's file I/O, ``shutil.*``, and subprocess argv resolution.
    - ``str(long_path)``:
      returns the plain path string *without* any ``\\?\\`` prefix on all
      platforms. Use this for logging and printing only — never for command
      building or file operations.

    Path composition (``/`` operator) is preserved: ``LongPath(base) / child``
    always returns a ``LongPath`` instance, not a plain ``Path``.

    Usage::

        work_dir = LongPath(args.work_dir)
        artifact = work_dir / "chunks" / "chunk_01.mkv"   # still LongPath
        artifact.mkdir(parents=True, exist_ok=True)        # uses __fspath__() — long-path safe
        cmd: list[str | os.PathLike] = ["ffmpeg", "-i", artifact, ...]
                                                            # pass path-like directly — subprocess
                                                            # resolves via __fspath__()
        cmd = ["mkvmerge", "@" + os.fspath(options_file)]  # forced single-string argument: concat
                                                            # with os.fspath, never str()
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
        Use this for logging/printing only — commands take the path-like
        directly (subprocess resolves it via ``__fspath__()``), and forced
        single-string arguments concatenate with ``os.fspath``.

        Returns:
            Plain path string, never prefixed with ``\\?\\``.
        """
        return super().__str__()

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: object,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Pydantic schema: validate like :class:`pathlib.Path`, coerce to ``LongPath``.

        Declared once on the type so pydantic models can annotate fields as
        ``LongPath`` directly — plain ``Path``/``str`` inputs validate through
        the standard Path schema and come back as ``LongPath`` instances.
        YAML string serialization is layered on top via a ``PlainSerializer``
        in the annotating module, not here.
        """
        from pydantic_core import (
            core_schema,  # local — keeps the module stdlib-only at import time
        )

        path_schema = handler(Path)
        return core_schema.no_info_after_validator_function(_coerce_long_path, path_schema)

    def __truediv__(self, key: str | Path) -> LongPath:
        """Extend path with ``/`` operator, preserving ``LongPath`` type.

        Args:
            key: Path component to append.

        Returns:
            New ``LongPath`` instance with the component appended.
        """
        return LongPath(super().__truediv__(key))

    def __rtruediv__(self, key: str | Path) -> LongPath:
        """Support ``str / LongPath`` composition, preserving ``LongPath`` type.

        Args:
            key: Left-hand path component.

        Returns:
            New ``LongPath`` instance.
        """
        return LongPath(super().__rtruediv__(key))
