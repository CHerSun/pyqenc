# Project Coding Standards

- Targeting Python>=3.13 syntax.
- For volatile things - try (not check).
- All functions, classes and class members MUST BE type-hinted.
- Type-hinting using newer rules: `int|None` instead of `Optional[int]`, newer generic classes without imports from `typing` where possible.
- Use `alive-progress` for progress display to the end user for long tasks.
- For subprocess cmd execution use type hint `list[str|os.PathLike]` and supply `Path` typed variables directly (without converting to `str`).
- Progress and current status display must be detailed and self-explanatory, lively, with the end-user in mind.
- `Path` from `pathlib` is mandatory for cross-platform path handling. NO `str` for paths (only during `Path` construction or string manipulation).
- When directly opening a file for reading or writing - use `Path`. This does NOT apply to libraries that handle their own file I/O (JSON, PNG, etc.).
- NO MAGIC NUMBERS or MAGIC STRINGS allowed. Use named constants or enum values. `"psnr"` is NOT allowed; `MetricType.PSNR.value` is.
- CLI is the mandatory starting point, but the final target is a client-server solution. The API MUST NOT be tailored only towards CLI.
- CLI script entry point must be defined in `pyproject.toml` so the end-user can call the program directly without `python ...`.
- Use `async` where it keeps the UI responsive or avoids blocking on I/O. There is NO goal to be 100% async.
- Clean, self-explanatory code is preferable over patterns-for-patterns'-sake.
- Public API and functions must have explanatory docstrings with required details. Only truly necessary functions should be public — clean, intent-driven API surface.
- Non-public functions must be prefixed with `_`, or `__` for internal implementation details.
- Use vertical alignment between arguments/parameters where it improves readability.
- Detailed logging is a MUST, separated by levels:
  - `debug` — hidden by default, implementation details
  - `info` — end-user notifications, progress milestones
  - `warning` — non-critical issues that allow continuation
  - `error` — failures that prevent a specific operation but not the whole run
  - `critical` — failures that prevent the program from doing any useful work
- Follow DRY. If code is repeated 2-3+ times — make it reusable.
- Follow rule of three — if there are 3+ similar entities, define a common interface (`Protocol` or base class) to unify the API.

## ffmpeg Execution

All ffmpeg subprocess calls MUST go through the unified runner in `pyqenc/utils/ffmpeg_runner.py`. Never call `subprocess.run`, `asyncio.create_subprocess_exec`, or any other subprocess primitive directly for ffmpeg.

- In async contexts: `await run_ffmpeg_async(cmd, ...)`
- In sync contexts: `run_ffmpeg(cmd, ...)` — raises `RuntimeError` if called from a running event loop
- The runner automatically injects `-hide_banner -nostats -progress pipe:1`, reads stdout/stderr concurrently, parses structured progress blocks, and returns `FFmpegRunResult`
- Pass a `ProgressCallback` (`(frame: int, out_time_s: float) -> None`) for live progress updates
- Pass a `VideoMetadata` instance to have it populated in-place from ffmpeg output
- See `.kiro/specs/ffmpeg-unified-runner/` for full requirements and design rationale
