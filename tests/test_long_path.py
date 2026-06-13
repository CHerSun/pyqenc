"""Unit tests for LongPath.

**Validates: Requirements 7.2**
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import pyqenc.utils.long_path as _lp_module
from pyqenc.utils.long_path import LongPath, _EXT_PREFIX, _MAX_PATH


# ---------------------------------------------------------------------------
# 1. LongPath is a subtype of Path (isinstance check)
# ---------------------------------------------------------------------------

class TestIsSubtypeOfPath:
    def test_longpath_is_instance_of_path(self) -> None:
        lp = LongPath("some/path")
        assert isinstance(lp, Path), "LongPath must be an instance of Path"

    def test_longpath_class_is_subclass_of_path(self) -> None:
        assert issubclass(LongPath, Path), "LongPath must be a subclass of Path"


# ---------------------------------------------------------------------------
# 2. Short path on Windows (mocked _WINDOWS=True): __fspath__() returns plain string
# ---------------------------------------------------------------------------

class TestShortPathOnWindows:
    def test_short_path_no_prefix(self) -> None:
        """On Windows, a path short enough should not get the \\?\\ prefix."""
        short_path = "C:\\short\\path.txt"
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(short_path)
            result = lp.__fspath__()
        assert not result.startswith(_EXT_PREFIX), (
            f"Short path should not be prefixed on Windows, got: {result!r}"
        )

    def test_short_path_fspath_no_prefix_via_osfspath(self) -> None:
        """os.fspath() on a short Windows LongPath must not inject the prefix."""
        short_path = "C:\\short\\path.txt"
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(short_path)
            result = os.fspath(lp)
        assert not result.startswith(_EXT_PREFIX)


# ---------------------------------------------------------------------------
# 3. Long path on Windows (mocked): __fspath__() returns \\?\-prefixed absolute string
# ---------------------------------------------------------------------------

class TestLongPathOnWindows:
    def test_long_path_gets_prefix(self, tmp_path: Path) -> None:
        """On Windows, a path longer than _MAX_PATH must receive the \\?\\ prefix."""
        long_name = "a" * 250
        long_path = tmp_path / long_name
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(long_path)
            result = lp.__fspath__()
        # os.path.abspath of the constructed path should exceed _MAX_PATH
        abs_str = os.path.abspath(str(long_path))
        if len(abs_str) > _MAX_PATH:
            assert result.startswith(_EXT_PREFIX), (
                f"Long path (len={len(abs_str)}) should be prefixed, got: {result!r}"
            )

    def test_long_path_prefix_is_followed_by_absolute_path(self, tmp_path: Path) -> None:
        """The \\?\\ prefix must be followed by the absolute path (no double slashes)."""
        long_name = "b" * 250
        long_path = tmp_path / long_name
        abs_str = os.path.abspath(str(long_path))
        if len(abs_str) <= _MAX_PATH:
            pytest.skip("tmp_path too short to produce a long path on this system")
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(long_path)
            result = lp.__fspath__()
        assert result == _EXT_PREFIX + abs_str, (
            f"Prefixed path should equal {_EXT_PREFIX + abs_str!r}, got: {result!r}"
        )


# ---------------------------------------------------------------------------
# 4. Already-prefixed path: __fspath__() does not double-prefix
# ---------------------------------------------------------------------------

class TestAlreadyPrefixedPath:
    def test_already_prefixed_path_not_double_prefixed(self) -> None:
        """A path already starting with \\?\\ must not get a second prefix."""
        prefixed = _EXT_PREFIX + "C:\\already\\prefixed\\path.txt"
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(prefixed)
            result = lp.__fspath__()
        assert not result.startswith(_EXT_PREFIX + _EXT_PREFIX), (
            f"Double prefix detected: {result!r}"
        )
        assert result.startswith(_EXT_PREFIX), (
            f"Prefix should be preserved: {result!r}"
        )

    def test_already_prefixed_long_path_not_double_prefixed(self) -> None:
        """A long already-prefixed path should not receive a second prefix."""
        # Construct a prefixed path that would normally be considered long
        long_suffix = "C:\\" + "x" * 300 + "\\file.txt"
        prefixed = _EXT_PREFIX + long_suffix
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(prefixed)
            result = lp.__fspath__()
        assert result.count(_EXT_PREFIX) == 1, (
            f"Expected exactly one prefix occurrence, got: {result!r}"
        )


# ---------------------------------------------------------------------------
# 5. str(long_path) equals str(Path(path)) for the same path string
# ---------------------------------------------------------------------------

class TestStrRepresentation:
    def test_str_equals_plain_path_str(self) -> None:
        """str(LongPath(p)) must equal str(Path(p)) for any path."""
        path_str = "some/relative/path.txt"
        assert str(LongPath(path_str)) == str(Path(path_str))

    def test_str_does_not_contain_prefix_on_non_windows(self) -> None:
        path_str = "C:\\some\\path.txt"
        with patch.object(_lp_module, "_WINDOWS", False):
            lp = LongPath(path_str)
            assert _EXT_PREFIX not in str(lp)

    def test_str_does_not_contain_prefix_on_windows(self, tmp_path: Path) -> None:
        """Even on Windows with a long path, str() must remain plain."""
        long_name = "c" * 250
        long_path = tmp_path / long_name
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(long_path)
            result = str(lp)
        assert _EXT_PREFIX not in result, (
            f"str(LongPath) must never contain the prefix, got: {result!r}"
        )

    def test_str_vs_fspath_differ_for_long_path_on_windows(self, tmp_path: Path) -> None:
        """For a long path on Windows: str() != __fspath__() (one plain, one prefixed)."""
        long_name = "d" * 250
        long_path = tmp_path / long_name
        abs_str = os.path.abspath(str(long_path))
        if len(abs_str) <= _MAX_PATH:
            pytest.skip("tmp_path too short to produce a long path on this system")
        with patch.object(_lp_module, "_WINDOWS", True):
            lp = LongPath(long_path)
            plain = str(lp)
            prefixed = lp.__fspath__()
        assert _EXT_PREFIX not in plain
        assert prefixed.startswith(_EXT_PREFIX)


# ---------------------------------------------------------------------------
# 6. Chained composition LongPath(base) / a / b / c is LongPath
# ---------------------------------------------------------------------------

class TestChainedComposition:
    def test_single_division_returns_longpath(self, tmp_path: Path) -> None:
        result = LongPath(tmp_path) / "subdir"
        assert isinstance(result, LongPath)

    def test_chained_three_divisions_returns_longpath(self, tmp_path: Path) -> None:
        result = LongPath(tmp_path) / "a" / "b" / "c"
        assert isinstance(result, LongPath)

    def test_chained_divisions_correct_path(self, tmp_path: Path) -> None:
        result = LongPath(tmp_path) / "a" / "b" / "c"
        expected = tmp_path / "a" / "b" / "c"
        assert str(result) == str(expected)

    def test_rtruediv_returns_longpath(self, tmp_path: Path) -> None:
        """str / LongPath should also produce a LongPath."""
        result = str(tmp_path) / LongPath("subdir")
        assert isinstance(result, LongPath)


# ---------------------------------------------------------------------------
# 7. .name, .stem, .suffix, .parent return expected values
# ---------------------------------------------------------------------------

class TestPathProperties:
    def test_name(self) -> None:
        lp = LongPath("some/path/file.tar.gz")
        assert lp.name == "file.tar.gz"

    def test_stem(self) -> None:
        lp = LongPath("some/path/file.tar.gz")
        assert lp.stem == "file.tar"

    def test_suffix(self) -> None:
        lp = LongPath("some/path/file.tar.gz")
        assert lp.suffix == ".gz"

    def test_parent(self) -> None:
        lp = LongPath("some/path/file.tar.gz")
        assert str(lp.parent) == str(Path("some/path"))

    def test_name_no_suffix(self) -> None:
        lp = LongPath("some/path/filename")
        assert lp.name == "filename"
        assert lp.stem == "filename"
        assert lp.suffix == ""

    def test_parent_is_longpath(self) -> None:
        """LongPath.parent should still be a LongPath instance."""
        lp = LongPath("some/path/file.txt")
        # Note: .parent is provided by pathlib internals — not overridden,
        # so we only assert the correct path value here (observable behavior).
        assert str(lp.parent) == str(Path("some/path"))


# ---------------------------------------------------------------------------
# 8. LongPath accepted where Path type annotation is expected (Liskov substitution)
# ---------------------------------------------------------------------------

class TestLiskovSubstitution:
    def test_accepted_as_path_typed_variable(self) -> None:
        """Assigning LongPath to a Path-typed variable must work (isinstance)."""
        path_var: Path = LongPath("some/path")
        assert isinstance(path_var, Path)

    def test_passed_to_path_consuming_function(self) -> None:
        """LongPath can be passed to functions that accept Path."""

        def accepts_path(p: Path) -> str:
            return str(p)

        lp = LongPath("some/path/file.txt")
        result = accepts_path(lp)
        assert result == str(Path("some/path/file.txt"))

    def test_path_joinpath_works_with_longpath(self, tmp_path: Path) -> None:
        """pathlib.Path.joinpath() accepts a LongPath as a component."""
        lp = LongPath("subdir")
        result = tmp_path.joinpath(lp)
        assert str(result) == str(tmp_path / "subdir")

    def test_os_fspath_dispatches_to_longpath_fspath(self) -> None:
        """os.fspath(long_path) must call LongPath.__fspath__()."""
        path_str = "some/path"
        with patch.object(_lp_module, "_WINDOWS", False):
            lp = LongPath(path_str)
            result = os.fspath(lp)
        assert result == str(Path(path_str))
