"""Property-based tests for LongPath.

# Feature: windows-long-path
**Validates: Requirements 7.1**
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import pyqenc.utils.long_path as _lp_module
from pyqenc.utils.long_path import LongPath, _EXT_PREFIX, _MAX_PATH


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Valid path strings: no null bytes, no pure whitespace
_path_str = st.text(min_size=1).filter(lambda s: "\x00" not in s and s.strip())

# Valid child components: safe characters only (no path separators, no special chars)
_child_str = st.from_regex(r"[a-zA-Z0-9_\-\.]+", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 1: __fspath__() injects prefix iff Windows and len(resolved) > 260
# Feature: windows-long-path, Property 1: fspath injects prefix iff Windows and long
# ---------------------------------------------------------------------------

class TestFspathInjectsPrefixIffWindowsAndLong:
    """Property 1: __fspath__() injects prefix iff Windows and path is long.

    **Validates: Requirements 7.1**
    """

    @given(is_windows=st.booleans())
    @settings(max_examples=200)
    def test_prefix_injected_only_on_windows_long_path(self, is_windows: bool) -> None:
        """__fspath__() injects \\?\\  prefix iff Windows and resolved path > 260 chars.

        # Feature: windows-long-path, Property 1: fspath injects prefix iff Windows and long
        **Validates: Requirements 7.1**
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Build a path long enough to exceed _MAX_PATH when resolved
            padding = "a" * 250
            long_base = Path(tmp_dir) / padding
            with patch.object(_lp_module, "_WINDOWS", is_windows):
                lp = LongPath(long_base)
                result = lp.__fspath__()
                resolved_str = str(lp.resolve())

            if is_windows:
                if len(resolved_str) > _MAX_PATH:
                    assert result.startswith(_EXT_PREFIX), (
                        f"Expected \\\\?\\ prefix on Windows with long path "
                        f"(len={len(resolved_str)}), got: {result!r}"
                    )
                else:
                    assert not result.startswith(_EXT_PREFIX), (
                        f"Expected no prefix on Windows with short path "
                        f"(len={len(resolved_str)}), got: {result!r}"
                    )
            else:
                assert not result.startswith(_EXT_PREFIX), (
                    f"Expected no prefix on non-Windows, got: {result!r}"
                )

    @given(path_str=_path_str)
    @settings(max_examples=200)
    def test_prefix_never_on_non_windows(self, path_str: str) -> None:
        """On non-Windows, __fspath__() never injects prefix regardless of path length.

        # Feature: windows-long-path, Property 1: fspath injects prefix iff Windows and long
        **Validates: Requirements 7.1**
        """
        with patch.object(_lp_module, "_WINDOWS", False):
            try:
                lp = LongPath(path_str)
                result = lp.__fspath__()
                assert not result.startswith(_EXT_PREFIX), (
                    f"Expected no prefix on non-Windows for {path_str!r}, got: {result!r}"
                )
            except (ValueError, OSError):
                # Invalid paths on certain platforms — skip
                pass


# ---------------------------------------------------------------------------
# Property 2: str() never contains \\?\
# Feature: windows-long-path, Property 2: str() never contains \\?\
# ---------------------------------------------------------------------------

class TestStrNeverContainsPrefix:
    """Property 2: str(LongPath(p)) never contains the \\?\\ prefix.

    **Validates: Requirements 7.1**
    """

    @given(path_str=_path_str, is_windows=st.booleans())
    @settings(max_examples=200)
    def test_str_never_contains_prefix(self, path_str: str, is_windows: bool) -> None:
        """str(LongPath(p)) never contains \\\\?\\ on any platform or any path length.

        # Feature: windows-long-path, Property 2: str() never contains \\?\
        **Validates: Requirements 7.1**
        """
        with patch.object(_lp_module, "_WINDOWS", is_windows):
            try:
                lp = LongPath(path_str)
                result = str(lp)
                assert _EXT_PREFIX not in result, (
                    f"str(LongPath({path_str!r})) must never contain {_EXT_PREFIX!r}, "
                    f"got: {result!r}"
                )
            except (ValueError, OSError):
                # Invalid path for the platform — skip
                pass

    @given(path_str=_path_str, is_windows=st.booleans())
    @settings(max_examples=200)
    def test_str_matches_plain_path_str(self, path_str: str, is_windows: bool) -> None:
        """str(LongPath(p)) equals str(Path(p)) for any path, any platform.

        # Feature: windows-long-path, Property 2: str() never contains \\?\
        **Validates: Requirements 7.1**
        """
        with patch.object(_lp_module, "_WINDOWS", is_windows):
            try:
                result   = str(LongPath(path_str))
                expected = str(Path(path_str))
                assert result == expected, (
                    f"str(LongPath({path_str!r})) == {result!r} != str(Path) == {expected!r}"
                )
            except (ValueError, OSError):
                pass


# ---------------------------------------------------------------------------
# Property 3: LongPath / child is always isinstance(result, LongPath)
# Feature: windows-long-path, Property 3: composition preserves LongPath type
# ---------------------------------------------------------------------------

class TestCompositionPreservesLongPathType:
    """Property 3: Path composition preserves LongPath type.

    **Validates: Requirements 7.1**
    """

    @given(base=_path_str, child=_child_str)
    @settings(max_examples=200)
    def test_truediv_returns_longpath(self, base: str, child: str) -> None:
        """LongPath(base) / child is always isinstance(result, LongPath).

        # Feature: windows-long-path, Property 3: composition preserves LongPath type
        **Validates: Requirements 7.1**
        """
        try:
            result = LongPath(base) / child
            assert isinstance(result, LongPath), (
                f"LongPath({base!r}) / {child!r} returned {type(result).__name__}, "
                f"expected LongPath"
            )
        except (ValueError, OSError):
            pass

    @given(base=_path_str, child=_child_str, grandchild=_child_str)
    @settings(max_examples=200)
    def test_chained_truediv_returns_longpath(
        self,
        base: str,
        child: str,
        grandchild: str,
    ) -> None:
        """Chained LongPath(base) / child / grandchild is always LongPath.

        # Feature: windows-long-path, Property 3: composition preserves LongPath type
        **Validates: Requirements 7.1**
        """
        try:
            result = LongPath(base) / child / grandchild
            assert isinstance(result, LongPath), (
                f"Chained composition returned {type(result).__name__}, expected LongPath"
            )
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# Property 4: Idempotence — double-prefixing never occurs
# Feature: windows-long-path, Property 4: idempotence — no double-prefix
# ---------------------------------------------------------------------------

class TestIdempotence:
    """Property 4: LongPath(lp.__fspath__()).__fspath__() equals lp.__fspath__().

    **Validates: Requirements 7.1**
    """

    @given(is_windows=st.booleans())
    @settings(max_examples=200)
    def test_fspath_is_idempotent_on_long_paths(self, is_windows: bool) -> None:
        """Applying __fspath__() twice yields the same result as applying it once.

        # Feature: windows-long-path, Property 4: idempotence — no double-prefix
        **Validates: Requirements 7.1**
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            padding = "a" * 250
            long_base = Path(tmp_dir) / padding
            with patch.object(_lp_module, "_WINDOWS", is_windows):
                lp     = LongPath(long_base)
                first  = lp.__fspath__()
                second = LongPath(first).__fspath__()

        assert first == second, (
            f"__fspath__() is not idempotent: first={first!r}, second={second!r}"
        )

    @given(path_str=_path_str, is_windows=st.booleans())
    @settings(max_examples=200)
    def test_fspath_idempotent_arbitrary_paths(
        self,
        path_str: str,
        is_windows: bool,
    ) -> None:
        """Idempotence holds for arbitrary path strings on any platform.

        # Feature: windows-long-path, Property 4: idempotence — no double-prefix
        **Validates: Requirements 7.1**
        """
        with patch.object(_lp_module, "_WINDOWS", is_windows):
            try:
                lp     = LongPath(path_str)
                first  = lp.__fspath__()
                second = LongPath(first).__fspath__()
                assert first == second, (
                    f"__fspath__() not idempotent for {path_str!r}: "
                    f"first={first!r}, second={second!r}"
                )
            except (ValueError, OSError):
                pass


# ---------------------------------------------------------------------------
# Property 5: Non-Windows identity — LongPath is a no-op off Windows
# Feature: windows-long-path, Property 5: non-Windows identity
# ---------------------------------------------------------------------------

class TestNonWindowsIdentity:
    """Property 5: LongPath(p).__fspath__() == str(Path(p)) when _WINDOWS = False.

    **Validates: Requirements 7.1**
    """

    @given(path_str=_path_str)
    @settings(max_examples=200)
    def test_non_windows_fspath_equals_plain_path(self, path_str: str) -> None:
        """On non-Windows, __fspath__() returns the same string as str(Path(p)).

        # Feature: windows-long-path, Property 5: non-Windows identity
        **Validates: Requirements 7.1**
        """
        with patch.object(_lp_module, "_WINDOWS", False):
            try:
                result   = LongPath(path_str).__fspath__()
                expected = str(Path(path_str))
                assert result == expected, (
                    f"Non-Windows __fspath__({path_str!r}) == {result!r}, "
                    f"expected {expected!r} (same as str(Path(...)))"
                )
            except (ValueError, OSError):
                pass

    @given(path_str=_path_str)
    @settings(max_examples=200)
    def test_non_windows_no_modification(self, path_str: str) -> None:
        """On non-Windows, LongPath is a complete no-op — no prefix, no transformation.

        # Feature: windows-long-path, Property 5: non-Windows identity
        **Validates: Requirements 7.1**
        """
        with patch.object(_lp_module, "_WINDOWS", False):
            try:
                result = LongPath(path_str).__fspath__()
                assert _EXT_PREFIX not in result, (
                    f"Non-Windows __fspath__ contains prefix: {result!r}"
                )
            except (ValueError, OSError):
                pass
