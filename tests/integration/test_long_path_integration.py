"""Windows-only integration tests for LongPath.

These tests exercise real filesystem operations at paths exceeding 260 characters,
verifying that ``LongPath`` transparently bypasses the Win32 MAX_PATH limit.

All tests are skipped on non-Windows platforms.

**Validates: Requirements 7.3**
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

from pathlib import Path  # noqa: E402  (after platform guard for clarity)

from pyqenc.utils.long_path import LongPath, _EXT_PREFIX  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_long_abs_path(tmp_path: Path, name: str = "a" * 240, suffix: str = "") -> str:
    """Return an absolute path string guaranteed to exceed 260 chars.

    Uses a long *name* component appended to *tmp_path*.  The caller is
    responsible for actually creating the file/directory before asserting.
    """
    abs_str = str(tmp_path / (name + suffix))
    # Ensure the path is actually long (tmp_path is typically ~30-50 chars on Windows)
    assert len(abs_str) > 260, (
        f"Generated path is only {len(abs_str)} chars — increase 'name' length"
    )
    return abs_str


def _create_at_long_path(abs_path: str, *, is_dir: bool = False) -> None:
    """Create a file or directory at *abs_path* using the ``\\?\\`` prefix.

    Python's os / pathlib normally refuses to create paths longer than 260 chars
    unless the extended-length prefix is used.  This helper adds the prefix
    explicitly so the path physically exists on disk before LongPath is tested.
    """
    extended = _EXT_PREFIX + abs_path if not abs_path.startswith(_EXT_PREFIX) else abs_path
    if is_dir:
        import os
        os.makedirs(extended, exist_ok=True)
    else:
        import os
        os.makedirs(_EXT_PREFIX + str(Path(abs_path).parent), exist_ok=True)
        with open(extended, "w") as fh:
            fh.write("")


# ---------------------------------------------------------------------------
# Test 1: LongPath.exists() detects a real >260-char directory
# ---------------------------------------------------------------------------

class TestExistsOnLongDirectory:
    def test_exists_returns_true_for_long_directory(self, tmp_path: Path) -> None:
        """LongPath.exists() must return True for a directory created beyond 260 chars."""
        abs_path = _make_long_abs_path(tmp_path, name="d" * 240)
        _create_at_long_path(abs_path, is_dir=True)

        lp = LongPath(abs_path)
        assert lp.exists(), (
            f"LongPath.exists() returned False for an existing >260-char directory.\n"
            f"Path length: {len(abs_path)}, path: {abs_path!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: LongPath.unlink() deletes a real >260-char file
# ---------------------------------------------------------------------------

class TestUnlinkOnLongFile:
    def test_unlink_deletes_long_file(self, tmp_path: Path) -> None:
        """LongPath.unlink() must delete a file at a >260-char path without error."""
        abs_path = _make_long_abs_path(tmp_path, name="f" * 240, suffix=".txt")
        _create_at_long_path(abs_path)

        lp = LongPath(abs_path)
        assert lp.exists(), "Pre-condition: file must exist before unlink()"

        lp.unlink()

        assert not lp.exists(), (
            "LongPath.unlink() did not remove the file at a >260-char path."
        )


# ---------------------------------------------------------------------------
# Test 3: LongPath.replace() renames a .tmp file at >260-char path
# ---------------------------------------------------------------------------

class TestReplaceOnLongPath:
    def test_replace_renames_tmp_to_final(self, tmp_path: Path) -> None:
        """LongPath.replace(final) must rename a .tmp file at a >260-char path."""
        abs_tmp  = _make_long_abs_path(tmp_path, name="r" * 235, suffix=".tmp")
        abs_final = _make_long_abs_path(tmp_path, name="r" * 235, suffix=".png")
        _create_at_long_path(abs_tmp)

        lp_tmp   = LongPath(abs_tmp)
        lp_final = LongPath(abs_final)

        assert lp_tmp.exists(),   "Pre-condition: .tmp file must exist"
        assert not lp_final.exists(), "Pre-condition: final file must NOT yet exist"

        lp_tmp.replace(lp_final)

        assert not lp_tmp.exists(),  "Source .tmp file must be gone after replace()"
        assert lp_final.exists(),    "Destination file must exist after replace()"


# ---------------------------------------------------------------------------
# Test 4: LongPath.mkdir(parents=True, exist_ok=True) creates >260-char hierarchy
# ---------------------------------------------------------------------------

class TestMkdirOnLongPath:
    def test_mkdir_creates_long_directory_hierarchy(self, tmp_path: Path) -> None:
        """LongPath.mkdir(parents=True, exist_ok=True) must create a >260-char directory."""
        # Use a single deep flat name long enough to push total path past 260 chars.
        # tmp_path is typically ~90 chars on this machine; 200 'x' chars puts us well over.
        long_dir = LongPath(tmp_path) / ("x" * 200)
        abs_str = str(long_dir)
        assert len(abs_str) > 260, (
            f"Directory path is only {len(abs_str)} chars — increase name length"
        )

        long_dir.mkdir(parents=True, exist_ok=True)

        assert long_dir.exists(), (
            f"LongPath.mkdir() did not create the >260-char directory.\n"
            f"Path length: {len(abs_str)}, path: {abs_str!r}"
        )

    def test_mkdir_exist_ok_does_not_raise_if_already_exists(self, tmp_path: Path) -> None:
        """exist_ok=True must not raise even if the directory already exists."""
        long_dir = LongPath(tmp_path) / ("y" * 200)
        assert len(str(long_dir)) > 260, "Path too short — increase name length"

        long_dir.mkdir(parents=True, exist_ok=True)
        # Second call must not raise
        long_dir.mkdir(parents=True, exist_ok=True)
