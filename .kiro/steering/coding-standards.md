# Project Coding Standards

## Agent Workflow

- **Before making any code changes — discuss the proposed approach with the user and get explicit approval first.**

## Python Language & Style

- Targeting Python>=3.13 syntax.
- For volatile things - try (not check).
- All functions, classes and class members MUST BE type-hinted.
- Type-hint using newer syntax: `int|None` instead of `Optional[int]`, newer generic classes without imports from `typing` where possible.
- Use top-level imports. In-place (local) imports are only acceptable when top-level imports are not possible, e.g. to break circular dependencies.
- Use vertical alignment between arguments/parameters where it improves readability.
- Follow DRY. If code is repeated 2-3+ times — make it reusable.
- Follow rule of three — if there are 3+ similar entities, define a common interface (`Protocol` or base class) to unify the API.
- Clean, self-explanatory code is preferable over patterns-for-patterns'-sake.
- Disowned functions are strongly discouraged. Mechanics should be owned by the related class, not written as standalone functions operating on external state.

## API & Architecture

- We do not keep legacy code for the sake of tests or backwards compatibility. Project is in pre-alpha state, there's no public API yet. Code cleanness is paramount over legacy compatibility.
- Public API and functions must have explanatory docstrings with required details. Only truly necessary functions should be public — clean, intent-driven API surface.
- Non-public functions must be prefixed with `_`, or `__` for internal implementation details.
- CLI is the mandatory starting point, but the final target is a client-server solution. The API MUST NOT be tailored only towards CLI.
- CLI script entry point must be defined in `pyproject.toml` so the end-user can call the program directly without `python ...`.
- Use `async` where it keeps the UI responsive or avoids blocking on I/O. There is NO goal to be 100% async.
- The default config object is the single source of truth for all config defaults. Everywhere else (function signatures, constructors, internal calls) values must be required explicitly — no default parameter values that could silently diverge from the canonical defaults.

## Paths, Files & Subprocesses

- `LongPath` from `pyqenc.utils.long_path` is mandatory for all project file I/O — it subclasses `Path` and transparently handles Windows extended-length paths (>260 chars). NO `str` for paths.
- Use `LongPath` everywhere a path is constructed, stored, or passed to Python file I/O (`open`, `mkdir`, `exists`, `replace`, `shutil.*`, etc.). This does NOT apply to libraries that handle their own file I/O (JSON, PNG, etc.).
- For any on-disk results use `.tmp`-then-rename protocol for atomicity and consistency enforcement.
- For subprocess cmd building use type hint `list[str|os.PathLike]` and supply `LongPath` variables directly (without converting to `str`). The runner calls `os.fspath()` which injects the `\\?\` prefix when needed. Exception: when ffmpeg or another tool does not understand the `\\?\` prefix, pass `str(long_path)` explicitly — `LongPath.__str__()` always returns the plain path.

## Constants & Magic Values

- NO MAGIC NUMBERS or MAGIC STRINGS allowed. Use named constants or enum values. `"psnr"` is NOT allowed; `MetricType.PSNR.value` is.
- Constants used multiple times must go into `constants.py`. `constants.py` must have no imports from the module (to avoid cycles).

## Logging

Detailed logging is a MUST, separated by levels:

- `debug` — hidden by default, implementation details, internal steps.
- `info` — end-user notifications, progress milestones, starts of long-running processes.
- `warning` — non-critical issues that allow continuation
- `error` — failures that prevent a specific operation but not the whole run
- `critical` — failures that prevent the program from doing any useful work

Use our `ProgressBar` for progress display to the end user for long tasks.

## Tests

- Tests should never check internal state, only observable behavior.

## ffmpeg Execution

All ffmpeg subprocess calls MUST go through the unified runner in `pyqenc/utils/ffmpeg_runner.py`. Never call `subprocess.run`, `asyncio.create_subprocess_exec`, or any other subprocess primitive directly for ffmpeg.

- In async contexts: `await run_ffmpeg_async(cmd, ...)`
- In sync contexts: `run_ffmpeg(cmd, ...)` — raises `RuntimeError` if called from a running event loop
- The runner automatically injects `-hide_banner -nostats -progress pipe:1`, reads stdout/stderr concurrently, parses structured progress blocks, and returns `FFmpegRunResult`
- Pass a `ProgressCallback` (`(frame: int, out_time_s: float) -> None`) for live progress updates
- Pass a `VideoMetadata` instance to have it populated in-place from ffmpeg output
- See `.kiro/specs/ffmpeg-unified-runner/` for full requirements and design rationale

## Pipeline Phase Contract

These rules govern how pipeline phases interact with each other and manage their own state.

- **Sidecar ownership.** Each phase owns its sidecar YAML file. It may persist results needed between reruns (detected crop, frame counts, etc.) and incoming settings whose change invalidates the phase's work (e.g. crop parameters for encoding — changing them requires a full re-encode). A phase is prohibited from reading or writing another phase's sidecar directly; it must go through the phase object's API.

- **Artifact ownership.** Each phase owns its inputs, intermediate results, and output artifacts. Other phases must obtain resulting artifacts only by calling the phase object — never by scanning the filesystem directly. For example: do not scan for successful encoding attempts; get them from the encoding phase along with their artifact status.

- **Artifact states.** A phase produces params and artifacts. Each artifact carries an explicit state: wanted & fully produced, partial/incomplete, or not wanted. Only wanted & fully completed artifacts should be propagated outside the phase. Unwanted artifacts are internal phase mechanics used to decide scope of work and must not leak to callers.

- **Recovery protocol.** Each phase owns its own recovery from incoming parameters and on-disk data. For each artifact: attempt recovery first; if that fails, check intermediate results to salvage partial work; only then plan the remaining work. Phase must use deterministic, reproducible naming so recovery is reliable.

- **Atomicity.** All results — intermediate, final, or otherwise — must follow the `.tmp`-then-rename protocol. There must never be a partial result without a `.tmp` extension on disk. This guarantees clean recovery and allows full trust in any non-`.tmp` artifact found on disk.

- **Invalidation.** Each phase is responsible for invalidating its own artifacts and intermediate results when incoming state changes between reruns. Parameters required for change detection must be persisted in the phase's sidecar.

- **Cleanup.** Each phase must respect the user-configured cleanup level and clean up its intermediate results accordingly.

- **Forced invalidation.** Each phase must respect forced invalidation requests from the user.
