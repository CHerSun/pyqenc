# Bugfix Requirements Document — Windows Long Path Support

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-08
- Completed: 2026-06-13

## Introduction

On Windows, all `pathlib.Path` file operations (`exists()`, `replace()`, `unlink()`, `mkdir()`, `open()`, `shutil.*`) silently fail when a path exceeds 260 characters (the Win32 `MAX_PATH` limit), even when the Windows system has long path support enabled in the registry/group policy.

The root cause is that `pathlib.Path.__fspath__()` returns a plain path string. Win32 APIs only bypass the 260-char limit when the path string is prefixed with `\\?\` (the extended-length path prefix). Python does not add this prefix automatically.

The impact is severe: screenshot capture silently loses output files (ffmpeg writes them successfully at the OS level, but Python's `exists()` returns `False`), and any other file I/O in the pipeline can silently fail for users with long video filenames or deep directory structures.

A whack-a-mole partial fix currently exists in `pyqenc/utils/win_path.py` (`lp_exists`, `lp_rename`, `lp_unlink`) and is applied only in `measure.py`. The correct fix is a `LongPath` subclass of `pathlib.Path` that overrides `__fspath__()` to inject the `\\?\` prefix on Windows, applied universally at all artifact path construction sites across the codebase.

---

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a file path exceeds 260 characters on Windows AND `Path.exists()` is called THEN the system returns `False` even though the file exists on disk

1.2 WHEN a file path exceeds 260 characters on Windows AND `Path.replace()` is called THEN the system raises `FileNotFoundError` or `OSError` even though both source and destination are valid

1.3 WHEN a file path exceeds 260 characters on Windows AND `Path.unlink()` is called THEN the system raises `FileNotFoundError` even though the file exists

1.4 WHEN a file path exceeds 260 characters on Windows AND `Path.mkdir()` is called THEN the system raises `OSError` and fails to create the directory

1.5 WHEN a file path exceeds 260 characters on Windows AND `Path.open()` or `open(path)` is called THEN the system raises `FileNotFoundError` or `OSError`

1.6 WHEN a file path exceeds 260 characters on Windows AND `shutil.copy2()`, `shutil.rmtree()`, or similar `shutil.*` functions are called THEN the system raises `OSError` or silently skips the operation

1.7 WHEN screenshot capture runs on Windows with a long working directory path AND ffmpeg successfully writes a `.tmp` screenshot file THEN `lp_exists()` (using `os.path.exists`) detects it but plain `Path.exists()` does not — the partial `win_path.py` fix masks the bug only in `measure.py`

1.8 WHEN a path construction chain uses `Path(base) / "child"` AND the base exceeds 260 characters on Windows THEN the resulting `Path` object loses any long-path workaround applied to the base

### Expected Behavior (Correct)

2.1 WHEN a file path exceeds 260 characters on Windows AND `LongPath.exists()` is called THEN the system SHALL return `True` if the file exists on disk

2.2 WHEN a file path exceeds 260 characters on Windows AND `LongPath.replace()` is called THEN the system SHALL atomically rename the file without raising any path-length-related error

2.3 WHEN a file path exceeds 260 characters on Windows AND `LongPath.unlink()` is called THEN the system SHALL delete the file without raising any path-length-related error

2.4 WHEN a file path exceeds 260 characters on Windows AND `LongPath.mkdir()` is called THEN the system SHALL create the directory without raising any path-length-related error

2.5 WHEN a file path exceeds 260 characters on Windows AND `LongPath.open()` or `open(long_path)` is called THEN the system SHALL open the file without raising any path-length-related error

2.6 WHEN a file path exceeds 260 characters on Windows AND `shutil.copy2()`, `shutil.rmtree()`, or similar `shutil.*` functions receive a `LongPath` THEN the system SHALL complete the operation successfully because `os.fspath()` calls `__fspath__()` which returns the `\\?\`-prefixed string

2.7 WHEN `LongPath(base) / "child"` is evaluated THEN the system SHALL return a `LongPath` instance (not a plain `Path`), preserving the long-path capability across path composition

2.8 WHEN `__fspath__()` is called on a `LongPath` on Windows THEN the system SHALL return the absolute path string prefixed with `\\?\` unless the prefix is already present

2.9 WHEN `__fspath__()` is called on a `LongPath` on Linux/macOS THEN the system SHALL return the plain path string unchanged (no prefix injected)

2.10 WHEN a `LongPath` is passed to ffmpeg subprocess commands via `str(path)` THEN the system SHALL return the plain path string without the `\\?\` prefix (ffmpeg does not accept the extended-length prefix)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a file path is 260 characters or fewer on Windows THEN the system SHALL CONTINUE TO operate identically to the current behavior with no performance or functional change

3.2 WHEN the codebase runs on Linux or macOS THEN the system SHALL CONTINUE TO operate identically to the current behavior — `LongPath.__fspath__()` returns the plain string, no prefix is injected

3.3 WHEN ffmpeg subprocess commands are constructed using `str(path)` for any path THEN the system SHALL CONTINUE TO receive plain path strings without the `\\?\` prefix

3.4 WHEN artifact paths are constructed via path composition (`base / "subdir" / "file.mkv"`) THEN the system SHALL CONTINUE TO produce the correct logical path, with the `LongPath` type preserved through the chain

3.5 WHEN `LongPath` is used in place of `Path` at construction sites THEN all existing `pathlib.Path` methods, properties, and operators SHALL CONTINUE TO work identically — `LongPath` is a transparent drop-in replacement

3.6 WHEN `pyqenc/utils/win_path.py` call sites in `measure.py` are removed and replaced with plain `LongPath` method calls THEN the measure phase SHALL CONTINUE TO capture screenshots, rename `.tmp` files, and clean up successfully

---

## Bug Condition

```python
def is_bug_condition(path: Path) -> bool:
    # Bug triggers when ALL of the following hold:
    return (
        sys.platform == "win32"
        and len(str(path)) > 260
        and type(path) is Path  # not LongPath
    )
```

```python
# Fix Checking — long path operations on Windows must succeed
for path, operation, expected in long_path_cases:
    assert is_bug_condition(path)
    result = operation(LongPath(path))  # LongPath injects \\?\ in __fspath__
    assert result == expected           # e.g. exists()==True, file renamed, etc.

# Preservation Checking — non-Windows and short paths are unaffected
for path, operation, expected in all_cases:
    if not is_bug_condition(path):
        assert operation(Path(path)) == operation(LongPath(path))
```
