# Requirements Document

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-09
- Completed: 2026-06-13

## Introduction

On Windows, `pathlib.Path` file operations (`exists()`, `replace()`, `unlink()`, `mkdir()`, `open()`, `shutil.*`) silently fail or raise `OSError` / `FileNotFoundError` when a path exceeds 260 characters (the Win32 `MAX_PATH` limit). This happens even when the Windows system has long path support enabled in the registry, because `pathlib.Path.__fspath__()` returns a plain path string, and Win32 APIs only bypass the 260-character limit when the path is prefixed with `\\?\` (the extended-length path prefix). Python does not add this prefix automatically.

The most visible symptom is screenshot capture silently losing output files: ffmpeg writes them successfully at the OS level, but `Path.exists()` returns `False`, so the pipeline discards them. Any other file I/O in the pipeline is equally affected for users with long video filenames or deep directory structures.

A partial whack-a-mole fix currently exists in `pyqenc/utils/win_path.py` (`lp_exists`, `lp_rename`, `lp_unlink`) and is applied only in `measure.py`. The correct fix is a `LongPath` subclass of `pathlib.Path` that overrides `__fspath__()` to inject the `\\?\` prefix on Windows, applied universally at `work_dir` construction so all derived paths inherit the behavior automatically.

## Glossary

- **`MAX_PATH`**: The Windows Win32 limit of 260 characters for path strings when paths are not in extended-length form.
- **Extended-length path prefix (`\\?\`)**: The 4-character prefix that instructs Win32 APIs to bypass the `MAX_PATH` limit and accept paths up to 32,767 characters.
- **`LongPath`**: The new `pathlib.Path` subclass defined in `pyqenc/utils/long_path.py` that overrides `__fspath__()` to inject `\\?\` on Windows for long paths.
- **`__fspath__()`**: The dunder method called by `os.fspath()`, used by all Python file I/O and `shutil.*` functions to obtain the OS-level path string.
- **`str(path)`**: The string representation of a path, used when constructing subprocess command lists. ffmpeg does not accept `\\?\`-prefixed paths, so `str(path)` must never contain the prefix.
- **`win_path.py`**: The partial fix module (`lp_exists`, `lp_rename`, `lp_unlink`) being removed as part of this work.
- **entry point**: The location in `cli.py` or `api.py` where `work_dir` is constructed. Applying `LongPath` here causes all downstream path composition via `/` to propagate `LongPath` automatically.

---

## Requirements

### Requirement 1: `LongPath` class — transparent extended-length path support

**User Story:** As a developer, I want a `LongPath` subclass of `pathlib.Path` that transparently injects the `\\?\` prefix on Windows for long paths, so that all Python file I/O and `shutil.*` operations work correctly without per-callsite changes.

#### Acceptance Criteria

1. WHERE `pyqenc/utils/long_path.py` is the module defining `LongPath`, THE system SHALL provide a `LongPath` class that is a subclass of `pathlib.Path` and passes `isinstance(long_path, Path)` checks.

2. WHEN `os.fspath(long_path)` or `long_path.__fspath__()` is called on a `LongPath` on Windows AND the resolved absolute path length exceeds 260 characters THEN the system SHALL return the absolute path string prefixed with `\\?\`, enabling all Python file I/O and `shutil.*` calls to bypass the Win32 `MAX_PATH` limit.

3. WHEN `os.fspath(long_path)` or `long_path.__fspath__()` is called on a `LongPath` on Windows AND the path length is 260 characters or fewer THEN the system SHALL return the plain path string without the `\\?\` prefix.

4. WHEN `os.fspath(long_path)` or `long_path.__fspath__()` is called on a `LongPath` on a non-Windows platform THEN the system SHALL return the plain path string unchanged with no prefix injected, so behavior is identical to plain `Path` on Linux and macOS.

5. WHEN `str(long_path)` is evaluated on any platform and for any path length THEN the system SHALL return the plain path string without the `\\?\` prefix, because ffmpeg subprocess commands require plain paths.

6. WHEN `LongPath(base) / child` is evaluated THEN the system SHALL return a `LongPath` instance, not a plain `Path`, preserving long-path capability across all downstream path composition.

7. WHEN `str / LongPath` or `str / LongPath` composition is evaluated via `__rtruediv__` THEN the system SHALL return a `LongPath` instance, not a plain `Path`.

8. WHEN `__fspath__()` is called on a `LongPath` that was constructed from a string already containing the `\\?\` prefix THEN the system SHALL return the string unchanged without prepending a second `\\?\` prefix.

9. WHEN `__fspath__()` is called on a relative `LongPath` on Windows THEN the system SHALL resolve it to an absolute path before checking length and injecting the prefix, so callers do not need to pre-resolve paths.

### Requirement 2: All `pathlib.Path` method calls work correctly for long paths on Windows

**User Story:** As a user running pyqenc on Windows with paths longer than 260 characters, I want all file operations to succeed, so that screenshots, artifacts, and pipeline output are not silently lost or corrupted.

#### Acceptance Criteria

1. WHEN `LongPath.exists()` is called on a path exceeding 260 characters on Windows AND the file exists on disk THEN the system SHALL return `True`.

2. WHEN `LongPath.replace(target)` is called on a path exceeding 260 characters on Windows THEN the system SHALL atomically rename the file without raising `FileNotFoundError` or `OSError`.

3. WHEN `LongPath.unlink()` or `LongPath.unlink(missing_ok=True)` is called on a path exceeding 260 characters on Windows THEN the system SHALL delete the file without raising `FileNotFoundError`.

4. WHEN `LongPath.mkdir(parents=True, exist_ok=True)` is called on a path exceeding 260 characters on Windows THEN the system SHALL create the directory hierarchy without raising `OSError`.

5. WHEN `LongPath.open()` or `open(long_path)` is called on a path exceeding 260 characters on Windows THEN the system SHALL open the file without raising `FileNotFoundError` or `OSError`.

6. WHEN `shutil.copy2()`, `shutil.rmtree()`, or any other `shutil.*` function receives a `LongPath` argument on Windows with a path exceeding 260 characters THEN the system SHALL complete the operation successfully, because `shutil` calls `os.fspath()` which calls `__fspath__()`.

### Requirement 3: Entry point integration — `work_dir` construction

**User Story:** As a developer, I want `work_dir` to be constructed as a `LongPath` at all CLI and API entry points, so that all artifact paths derived from it automatically inherit long-path support without any per-phase code changes.

#### Acceptance Criteria

1. WHEN `pyqenc` is invoked via `cli.py` THEN the system SHALL construct `work_dir` as a `LongPath` (e.g. via `type=LongPath` on the `--work-dir` argument parser) before any phase execution.

2. WHEN `api.py` public functions (`run_pipeline`, `extract_streams`, `chunk_video`, `encode_chunks`, `process_audio`, `merge_final`, `measure_quality`) are called THEN the system SHALL wrap `work_dir` as `LongPath(work_dir)` at the top of each function before `work_dir.mkdir()` or any path composition.

3. WHEN `work_dir` is a `LongPath` AND any phase constructs an artifact path via `work_dir / "subdir" / "file.mkv"` THEN the resulting path SHALL be a `LongPath` instance, requiring no changes in any phase module.

### Requirement 4: Remove `win_path.py` partial fix and clean up `measure.py`

**User Story:** As a developer, I want the whack-a-mole `win_path.py` helpers removed and `measure.py` updated to use plain `LongPath` method calls, so that there is one clean fix that covers all paths rather than scattered per-callsite workarounds.

#### Acceptance Criteria

1. WHEN the codebase is updated THEN the system SHALL delete `pyqenc/utils/win_path.py` entirely, removing `_ext`, `lp_exists`, `lp_rename`, and `lp_unlink`.

2. WHEN `measure.py` is updated THEN the system SHALL remove the import of `lp_exists`, `lp_rename`, `lp_unlink` from `win_path`.

3. WHEN `_capture_single_frame` in `measure.py` checks for output file existence THEN the system SHALL use `output_path.exists()` instead of `lp_exists(output_path)`.

4. WHEN `make_screenshots` (Strategy C loop) and `_rename_raw_screenshots` in `measure.py` perform `.tmp`-then-rename operations THEN the system SHALL use `tmp_path.replace(final_path)` instead of `lp_rename(tmp_path, final_path)` and `tmp_path.unlink(missing_ok=True)` instead of `lp_unlink(tmp_path, missing_ok=True)`.

5. WHEN the updated `measure.py` runs on Windows with a `work_dir` path exceeding 260 characters THEN screenshot capture SHALL succeed: ffmpeg writes the `.tmp` file, `output_path.exists()` returns `True`, `.replace()` renames it, and no file is silently lost.

### Requirement 5: `state.py` — long-path-safe deserialization of output paths

**User Story:** As a developer, I want pipeline state deserialized from YAML to use `LongPath` for output paths, so that a resumed pipeline run on Windows with long paths does not silently fail when operating on previously saved artifact paths.

#### Acceptance Criteria

1. WHEN `MergeStrategySummary.from_yaml_dict` in `state.py` constructs `output_path` THEN the system SHALL use `LongPath(data["output_path"])` instead of `Path(data["output_path"])`, so that resumed pipelines retain long-path support for merge output artifacts.

### Requirement 6: Regression prevention

**User Story:** As a developer, I want the `LongPath` change to be a transparent drop-in with zero behavioral change for short paths on Windows and for all paths on other platforms, so that the fix cannot introduce regressions.

#### Acceptance Criteria

1. WHEN a file path is 260 characters or fewer on Windows THEN the system SHALL operate identically to the current behavior — `LongPath.__fspath__()` returns the plain path string with no prefix and no performance overhead.

2. WHEN the codebase runs on Linux or macOS THEN the system SHALL operate identically to the current behavior — `LongPath.__fspath__()` returns the plain string, no prefix is ever injected.

3. WHEN ffmpeg subprocess commands are constructed using `str(path)` for any `LongPath` THEN the system SHALL supply plain path strings without the `\\?\` prefix, because ffmpeg does not accept extended-length paths.

4. WHEN `LongPath` is used in place of `Path` at any construction site THEN all existing `pathlib.Path` methods, properties, and operators (`.name`, `.stem`, `.suffix`, `.parent`, `.resolve()`, etc.) SHALL continue to work identically — `LongPath` is a transparent drop-in replacement.

### Requirement 7: Property-based and unit tests

**User Story:** As a developer, I want a comprehensive test suite covering all `LongPath` correctness properties, so that the implementation is verifiably correct and regressions are caught automatically.

#### Acceptance Criteria

1. THE system SHALL provide property-based tests (using Hypothesis) in `tests/test_long_path_properties.py` that verify:
   - `__fspath__()` injects the `\\?\` prefix if and only if the platform is Windows and the path length exceeds 260 characters (Property 1)
   - `str(LongPath(p))` never contains the `\\?\` prefix for any path on any platform (Property 2)
   - `LongPath(base) / child` always returns an instance of `LongPath` (Property 3)
   - Double-prefixing never occurs — calling `__fspath__()` on an already-prefixed path is idempotent (Property 4)
   - On non-Windows platforms `LongPath.__fspath__()` equals `str(Path(p))` for all inputs (Property 5)

2. THE system SHALL provide example-based unit tests in `tests/test_long_path.py` covering:
   - `LongPath` is a subtype of `Path` (`isinstance` check)
   - Short path on Windows: `__fspath__()` returns plain string
   - Long path on Windows: `__fspath__()` returns `\\?\`-prefixed absolute string
   - `str(long_path)` equals `str(Path(path))` for any path
   - Chained composition `LongPath(base) / child / grandchild` returns `LongPath`
   - `.name`, `.stem`, `.suffix`, `.parent` return expected values
   - `LongPath` is accepted wherever `Path` is (Liskov substitution)

3. THE system SHALL provide Windows-only integration tests in `tests/integration/test_long_path_integration.py` (skipped on non-Windows via `pytest.mark.skipif`) that verify real filesystem operations at paths longer than 260 characters: `exists()`, `unlink()`, `replace()`, and `mkdir(parents=True)` all succeed.

4. THE system SHALL provide a regression test in `tests/test_measure_long_path.py` that mocks `run_ffmpeg_async` to create a `.tmp` file at a >260-char path, verifies `output_path.exists()` detects it, verifies `tmp_path.replace(final_path)` succeeds, and confirms `win_path` helpers are no longer imported anywhere in the codebase.
