"""Regression tests for the measure.py long-path migration.

Verifies that LongPath operations used in the measure phase (exists, replace)
work correctly for paths exceeding 260 characters, and that the old win_path
module is no longer imported anywhere in the codebase after cleanup.

**Validates: Requirements 4.5, 7.4**
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pyqenc.constants import TEMP_SUFFIX
from pyqenc.utils.ffmpeg_runner import FFmpegRunResult
from pyqenc.utils.long_path import LongPath


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_long_output_path(tmp_path: Path, suffix: str = ".png") -> LongPath:
    """Return a LongPath at a path guaranteed to exceed 260 characters.

    Uses ``tmp_path / ("a" * 240) / "frame.png"`` so that the total absolute
    path length is well above 260 chars on any reasonable system.
    """
    long_dir = LongPath(tmp_path) / ("a" * 240)
    long_dir.mkdir(parents=True, exist_ok=True)
    return long_dir / f"frame{suffix}"


def _assert_long_path(path: Path) -> None:
    """Assert that the given path string exceeds 260 characters."""
    path_len = len(str(path))
    assert path_len > 260, (
        f"Expected path longer than 260 chars for a meaningful test, "
        f"got {path_len} chars: {str(path)!r}"
    )


# ---------------------------------------------------------------------------
# Test 1: LongPath.exists() detects the .tmp file written by ffmpeg mock
# ---------------------------------------------------------------------------

class TestOutputPathExistsViaLongPath:
    """Mock run_ffmpeg_async to create a .tmp file and verify LongPath.exists().

    Simulates what _capture_single_frame does: ffmpeg is called with a .tmp
    output path (the runner's .tmp-then-rename protocol), and after the call
    the code checks whether the file exists using LongPath.exists().

    **Validates: Requirements 4.5, 7.4**
    """

    def test_output_path_exists_via_longpath(self, tmp_path: Path) -> None:
        """LongPath.exists() must return True for a >260-char .tmp file after mock writes it."""
        # Construct the .tmp path exactly as _capture_single_frame does:
        # output_path is the final PNG; the runner creates <stem>.tmp beside it.
        final_path = _make_long_output_path(tmp_path, suffix=".png")
        tmp_output  = LongPath(final_path.parent) / f"{final_path.stem}{TEMP_SUFFIX}"

        _assert_long_path(final_path)

        async def _mock_run_ffmpeg_async(
            cmd: list,
            output_file: object,
            **kwargs: object,
        ) -> FFmpegRunResult:
            """Simulate ffmpeg writing the .tmp output file."""
            # The runner passes the .tmp path as the last element of cmd.
            # We just create it directly, as ffmpeg would on success.
            tmp_output.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG magic bytes
            return FFmpegRunResult(returncode=0, success=True)

        # Patch run_ffmpeg_async where measure.py imports it from
        with patch(
            "pyqenc.phases.measure.run_ffmpeg_async",
            new=AsyncMock(side_effect=_mock_run_ffmpeg_async),
        ):
            # Simulate the check measure.py performs after calling run_ffmpeg_async
            asyncio.run(_mock_run_ffmpeg_async([], None))

        # The key assertion: LongPath.exists() must find the file
        assert LongPath(tmp_output).exists(), (
            f"LongPath.exists() returned False for a >260-char .tmp file that was written.\n"
            f"Path length: {len(str(tmp_output))}\n"
            f"Path: {str(tmp_output)!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: LongPath.replace() renames .tmp → final without error
# ---------------------------------------------------------------------------

class TestReplaceWorksViaLongPath:
    """Verify that tmp_path.replace(final_path) succeeds at >260-char paths.

    This mirrors the rename step in both _capture_single_frame (Strategy C)
    and _rename_raw_screenshots — the post-ffmpeg .tmp-then-rename protocol.

    **Validates: Requirements 4.5, 7.4**
    """

    def test_replace_works_via_longpath(self, tmp_path: Path) -> None:
        """LongPath.replace(final) must succeed for paths exceeding 260 characters."""
        final_path = _make_long_output_path(tmp_path, suffix=".png")
        tmp_file   = LongPath(final_path.parent) / f"{final_path.stem}{TEMP_SUFFIX}"

        _assert_long_path(final_path)
        _assert_long_path(tmp_file)

        # Write the .tmp file (as the mock ffmpeg would have done)
        tmp_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        assert tmp_file.exists(),       "Pre-condition: .tmp file must exist before replace()"
        assert not final_path.exists(), "Pre-condition: final file must NOT yet exist"

        # This is the operation that previously failed without LongPath (via lp_rename)
        tmp_file.replace(final_path)

        assert not tmp_file.exists(), (
            "Source .tmp file must be gone after LongPath.replace()"
        )
        assert final_path.exists(), (
            f"Destination file must exist after LongPath.replace().\n"
            f"Final path length: {len(str(final_path))}\n"
            f"Final path: {str(final_path)!r}"
        )

    def test_replaced_file_has_correct_content(self, tmp_path: Path) -> None:
        """Content is preserved across LongPath.replace() at >260-char paths."""
        final_path = _make_long_output_path(tmp_path, suffix=".png")
        tmp_file   = LongPath(final_path.parent) / f"{final_path.stem}{TEMP_SUFFIX}"

        _assert_long_path(final_path)

        content = b"\x89PNG\r\n\x1a\n" + b"x" * 128
        tmp_file.write_bytes(content)

        tmp_file.replace(final_path)

        assert final_path.read_bytes() == content, (
            "File content must be preserved after LongPath.replace()"
        )


# ---------------------------------------------------------------------------
# Test 3: win_path module is not imported anywhere in the codebase
# NOTE: This test will FAIL until task 6 (deletion of win_path.py) is done.
#       That is expected — it acts as a green-light indicator for cleanup completion.
# ---------------------------------------------------------------------------

WIN_PATH_IMPORT_PATTERN: re.Pattern[str] = re.compile(
    r"\bfrom\s+pyqenc\.utils\.win_path\b|\bimport\s+pyqenc\.utils\.win_path\b"
)
"""Regex that matches any import of the win_path module in Python source files."""


class TestWinPathNotImportedAnywhere:
    """Scan pyqenc/ for any remaining win_path imports.

    This test is intentionally written to document the post-cleanup state.
    It FAILS until task 6 completes (win_path.py deleted and all imports removed).

    **Validates: Requirements 4.5, 7.4**
    """

    def test_win_path_not_imported_anywhere(self) -> None:
        """No Python file in pyqenc/ should import from pyqenc.utils.win_path."""
        pyqenc_root = Path(__file__).parent.parent / "pyqenc"
        assert pyqenc_root.is_dir(), f"pyqenc/ source root not found at {pyqenc_root}"

        offending_files: list[str] = []
        for py_file in sorted(pyqenc_root.rglob("*.py")):
            source = py_file.read_text(encoding="utf-8", errors="replace")
            if WIN_PATH_IMPORT_PATTERN.search(source):
                offending_files.append(str(py_file.relative_to(pyqenc_root.parent)))

        assert not offending_files, (
            "The following files still import from pyqenc.utils.win_path "
            "(expected to be removed in task 6):\n"
            + "\n".join(f"  {f}" for f in offending_files)
        )
