# Implementation Plan

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-09
- Completed: 2026-06-13

## Overview

Fix Windows long path failures by:
1. Creating `pyqenc/utils/long_path.py` with a `LongPath(Path)` subclass that overrides `__fspath__()` to inject the `\\?\` prefix on Windows for paths longer than 260 characters.
2. Applying `LongPath` at `work_dir` construction in `cli.py` and `api.py` so all downstream artifact paths inherit the behavior automatically via the `/` operator.
3. Removing the partial whack-a-mole fix in `win_path.py` and cleaning up all its call sites in `measure.py`.
4. Updating `state.py` to deserialize output paths as `LongPath` so resumed pipelines are also protected.
5. Writing property-based, unit, integration, and regression tests to verify all correctness properties.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1"]
    },
    {
      "wave": 2,
      "tasks": ["2", "3"]
    },
    {
      "wave": 3,
      "tasks": ["4", "5"]
    },
    {
      "wave": 4,
      "tasks": ["6", "7"]
    },
    {
      "wave": 5,
      "tasks": ["8", "9"]
    }
  ]
}
```

## Tasks

- [x] 1. Create `pyqenc/utils/long_path.py`

  - Define module-level constants `_WINDOWS: bool`, `_EXT_PREFIX: str`, `_MAX_PATH: int = 260` (no magic strings or numbers inline)
  - Implement `LongPath(type(Path()))` subclass with:
    - `__fspath__(self) -> str` — returns `\\?\`-prefixed resolved absolute path on Windows when `len > _MAX_PATH` and prefix not already present; returns `str(self)` on non-Windows; idempotent guard via `.startswith(_EXT_PREFIX)`
    - `__str__(self) -> str` — delegates to `super().__str__()`, never returns `\\?\` prefix
    - `__truediv__(self, key: str | Path) -> LongPath` — wraps `super().__truediv__(key)` result in `LongPath()`
    - `__rtruediv__(self, key: str | Path) -> LongPath` — wraps `super().__rtruediv__(key)` result in `LongPath()`
  - Add class docstring and per-method docstrings per coding standards
  - _Requirements: 1.1–1.9_

- [x] 2. Write tests for `LongPath`

  - [x] 2.1 Property-based tests — `tests/test_long_path_properties.py`

    - Import Hypothesis `given`, `settings`, `st`; monkeypatch `pyqenc.utils.long_path._WINDOWS` for platform simulation
    - Property 1: `__fspath__()` injects prefix iff Windows and `len(resolved) > 260` — `st.text` + monkeypatch, min 200 examples
    - Property 2: `str(LongPath(p))` never contains `\\?\` — any platform, any path, min 200 examples
    - Property 3: `LongPath(base) / child` is always `isinstance(result, LongPath)` — min 200 examples
    - Property 4: Idempotence — `LongPath(lp.__fspath__()).__fspath__()` equals `lp.__fspath__()` — min 200 examples
    - Property 5: Non-Windows identity — `LongPath(p).__fspath__() == str(Path(p))` when `_WINDOWS = False` — min 200 examples
    - _Requirements: 7.1_

  - [x] 2.2 Unit tests — `tests/test_long_path.py`

    - `LongPath` is subtype of `Path` (`isinstance` check)
    - Short path on Windows (mocked `_WINDOWS=True`): `__fspath__()` returns plain string
    - Long path on Windows (mocked): `__fspath__()` returns `\\?\`-prefixed absolute string
    - Already-prefixed path: `__fspath__()` does not double-prefix
    - `str(long_path)` equals `str(Path(path))` for the same path string
    - Chained composition `LongPath(base) / a / b / c` is `LongPath`
    - `.name`, `.stem`, `.suffix`, `.parent` return expected values
    - `LongPath` accepted where `Path` type annotation is expected (Liskov substitution)
    - _Requirements: 7.2_

  - [x] 2.3 Windows-only integration tests — `tests/integration/test_long_path_integration.py`

    - Skip on non-Windows via `pytest.mark.skipif(sys.platform != "win32", reason="Windows only")`
    - Create real directory with >260-char absolute path using `\\?\` manually, verify `LongPath.exists()` returns `True`
    - Create file at >260-char path, verify `LongPath.unlink()` deletes it without error
    - Create `.tmp` file at >260-char path, verify `LongPath.replace(final)` renames correctly
    - Verify `LongPath.mkdir(parents=True, exist_ok=True)` creates a >260-char directory hierarchy
    - _Requirements: 7.3_

  - [x] 2.4 Regression test — `tests/test_measure_long_path.py`

    - Mock `run_ffmpeg_async` to create a `.tmp` PNG at a >260-char path (simulating ffmpeg output)
    - Verify `output_path.exists()` (using `LongPath.exists()`) detects the file
    - Verify `tmp_path.replace(final_path)` completes without error
    - Verify `win_path` module is not imported anywhere in the codebase (use importlib or grep scan)
    - _Requirements: 4.5, 7.4_

- [x] 3. Integrate `LongPath` at entry points (`cli.py`, `api.py`)

  - [x] 3.1 Update `pyqenc/cli.py`

    - Add `from pyqenc.utils.long_path import LongPath`
    - In `_add_base_arguments`, change the `--work-dir` argument to `type=LongPath` so the parsed value is already a `LongPath`
    - _Requirements: 3.1_

  - [x] 3.2 Update `pyqenc/api.py`

    - Add `from pyqenc.utils.long_path import LongPath`
    - At the top of each public function (`run_pipeline`, `extract_streams`, `chunk_video`, `encode_chunks`, `process_audio`, `merge_final`, `measure_quality`), add `work_dir = LongPath(work_dir)` before `work_dir.mkdir(...)` or any path composition
    - _Requirements: 3.2_

- [x] 4. Clean up `pyqenc/phases/measure.py`

  - Remove `from pyqenc.utils.win_path import lp_exists, lp_rename, lp_unlink` import
  - In `_capture_single_frame`: replace `lp_exists(output_path)` → `output_path.exists()`
  - In `make_screenshots` Strategy C loop: replace `lp_rename(tmp_path, final_path)` → `tmp_path.replace(final_path)` and `lp_unlink(tmp_path, missing_ok=True)` → `tmp_path.unlink(missing_ok=True)`
  - In `_rename_raw_screenshots`: replace `lp_rename` → `.replace()` and `lp_unlink` → `.unlink(missing_ok=True)` at all call sites
  - Confirm no other references to `win_path` remain in `measure.py`
  - _Requirements: 4.2–4.5_

- [x] 5. Update `pyqenc/state.py` — long-path-safe YAML deserialization

  - Add `from pyqenc.utils.long_path import LongPath`
  - In `MergeStrategySummary.from_yaml_dict`: change `output_path = Path(data["output_path"])` → `output_path = LongPath(data["output_path"])`
  - _Requirements: 5.1_

- [x] 6. Delete `pyqenc/utils/win_path.py`

  - Confirm no imports of `win_path` remain anywhere in the codebase (`grep -r "win_path" pyqenc/` returns no results after measure.py cleanup)
  - Delete the file
  - _Requirements: 4.1_

- [x] 7. Run tests and verify correctness

  - Run full test suite: `uv run python -m pytest tests/test_long_path.py tests/test_long_path_properties.py tests/test_measure_long_path.py -v`
  - Run integration tests on Windows if available: `uv run python -m pytest tests/integration/test_long_path_integration.py -v`
  - Run linter: `uv run ruff check pyqenc/utils/long_path.py pyqenc/cli.py pyqenc/api.py pyqenc/phases/measure.py pyqenc/state.py`
  - Confirm `grep -r "win_path" pyqenc/` returns no output
  - _Requirements: all_

- [x] 8. Review this spec against other specs and add cross-spec summaries

  - Read timestamps and contents of relevant sibling specs to identify relationships (particularly `ffmpeg-unified-runner`, `pipeline-maturity-refactor`, any spec referencing `win_path.py` or path handling)
  - Update the cross-spec summary table in `design.md` with any newly discovered superseded or affected specs
  - Add a matching summary note to any affected sibling spec that references `win_path.py` or platform-specific path handling
  - _Requirements: agent-specs steering rule_

- [x] 9. Mark spec completed

  - Add `- Completed: <ISO date>` to the header list in `requirements.md`, `design.md`, `bugfix.md`, and this `tasks.md`
  - _Requirements: agent-specs steering rule_

## Notes

- `LongPath` is introduced exactly once — at `work_dir` construction. All artifact paths downstream inherit the type via `__truediv__` automatically; no per-phase or per-callsite changes are needed in job, extraction, chunking, encoding, optimization, merge, audio, or recovery phases.
- `str(path)` is always plain (no `\\?\` prefix) — this is the correct form for ffmpeg subprocess command lists per the ffmpeg-unified-runner spec and coding standards.
- `os.fspath(path)` / `__fspath__()` is the form that injects the prefix — used transparently by all Python file I/O and `shutil.*` calls.
- Integration tests (task 2.3) require a Windows machine and will be skipped automatically on CI running Linux/macOS.
- Property-based tests mock `_WINDOWS` via `monkeypatch` so they run correctly on all platforms including CI.
