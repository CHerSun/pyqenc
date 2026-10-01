# Project Coding Standards

## Agent Workflow

- **Before making any code changes — discuss the proposed approach with the user and get explicit approval first.**

## Python Language & Style

- Targeting Python>=3.14 syntax. Interpreter pinned via `.python-version` (3.14.x line, not 3.15).
- Annotations rely on PEP 649 lazy evaluation (3.14 default): NEVER write `from __future__ import annotations` (a deprecated no-op on 3.14) and NEVER quote annotations (`"AppConfig"`) — bare names work for forward references and `TYPE_CHECKING`-only imports.
- For volatile things - try (not check).
- All functions, classes and class members MUST BE type-hinted.
- Type-hint using newer syntax: `int|None` instead of `Optional[int]`, newer generic classes without imports from `typing` where possible.
- Use top-level imports. In-place (local) imports are only acceptable when top-level imports are not possible, e.g. to break circular dependencies.
- Use vertical alignment between arguments/parameters where it improves readability.
- Follow DRY. If code is repeated 2-3+ times — make it reusable.
- Follow rule of three — if there are 3+ similar entities, define a common interface (`Protocol` or base class) to unify the API.
- **NEVER use `getattr`/`setattr` on class instances of known types.** When the type is known (e.g. a typed `PhaseResult` subclass), access its fields directly; when the static type is broader than the runtime one, bind once with `cast(KnownType, expr)` and use the fields. `getattr` with a default silently survives typos and renamed fields — a refactoring pain. The only acceptable dynamic access is a genuinely dynamic container (`dict`). Exception: probing an `argparse.Namespace` while assembling optional CLI overrides.
- Clean, self-explanatory code is preferable over patterns-for-patterns'-sake.
- Disowned functions are strongly discouraged. Mechanics should be owned by the related class, not written as standalone functions operating on external state. Example when disowned functions could be ok - to reach uniform logging between different phases.

## API & Architecture

- We do not keep legacy code for the sake of tests or backwards compatibility. Project is in pre-alpha state, there's no public API yet. Code cleanness is paramount over legacy compatibility.
- Public API and functions must have explanatory docstrings with required details. Only truly necessary functions should be public — clean, intent-driven API surface.
- Non-public functions must be prefixed with `_`, or `__` for internal implementation details.
- CLI is the mandatory starting point, but the final target is a client-server solution. The API MUST NOT be tailored only towards CLI.
- CLI script entry point must be defined in `pyproject.toml` so the end-user can call the program directly without `python ...`.
- Use `async` where it keeps the UI responsive or avoids blocking on I/O. There is NO goal to be 100% async.
- The default config object is the single source of truth for all config defaults. Everywhere else (function signatures, constructors, internal calls) values must be required explicitly — no default parameter values that could silently diverge from the canonical defaults.

## Naming (two-name doctrine)

- Every named element owns exactly two method accessors: `display_name()` (the single verbatim generator) and `safe_name()` (the sanitized filesystem form derived from it). No third accessors, no exemptions.
- **Filesystem work ALWAYS uses `safe_name()`** — building paths, comparing against on-disk names, anything that lands on or is read from disk. Everything else (printing, yaml payloads, dict keys) uses `display_name()`.
- The owning class is the only place a name is composed — no manual joins of name parts outside the owner; consumers take the composed name from the accessor.

## Paths, Files & Subprocesses

- `LongPath` from `pyqenc.utils.long_path` transparently handles Windows extended-length paths (>260 chars). NO `str` for paths.
- **Signatures declare `Path`** — the honest footprint (the body works with any `Path`). **Callers construct `LongPath` at the boundary** where a path first enters our code (CLI args, sidecar/model loads, test fixtures) and pass it through.
- Never re-instantiate `Path(...)`/`LongPath(...)` around an existing path inside logic — chain with `/` and path methods; `LongPath` overrides the composition operators, so the subtype is preserved without re-wrapping. (Pydantic model fields may still be annotated `LongPath` — its schema coerces.)
- Use `LongPath` everywhere a path is constructed, stored, or passed to Python file I/O (`open`, `mkdir`, `exists`, `replace`, `shutil.*`, etc.). This does NOT apply to libraries that handle their own file I/O (JSON, PNG, etc.).
- For any on-disk results use `.tmp`-then-rename protocol for atomicity and consistency enforcement.
- For subprocess cmd building use type hint `list[str|os.PathLike]` and supply `LongPath`/`Path` variables directly (without converting to `str`). The subprocess layer calls `os.fspath()` which injects the `\\?\` prefix when needed. Sub-string arguments that a tool parses itself (mkvmerge `@options.json`, mkvextract `0:timestamps.txt`, ffmpeg filter args) take the plain form via `str(path)` — these parsers are picky and must not receive an extended-length prefix. `str(path)` is otherwise allowed for printing/logging only, never for command building or file operations.

## Optionality (None policy)

- Prefer non-Optional: if a value is always present by construction, the type must say so (`work_dir: Path`, never `Path | None`). An Optional footprint consumed unconditionally is a lie the type checker will flag.
- Avoid `None` as a sentinel where an empty container expresses it. Empty containers are checked Python-style (`if not mapping:`); direct `.get()`/`in` are fine. Use `None` only where "absent" is semantically distinct from "empty".
- Presence guaranteed by our own construction is enforced with `assert` at the consuming site — not by widening footprints to Optional (see Contracts below).

## Constants & Magic Values

- NO MAGIC NUMBERS or MAGIC STRINGS allowed. Use named constants or enum values. `"psnr"` is NOT allowed; `MetricType.PSNR.value` is.
- Constants used multiple times must go into `constants.py`. `constants.py` must have no imports from the module (to avoid cycles).
- A constant (or function) with a single consuming class belongs to that class (attribute/method). If it is internal to the class, prefix it with `_`. If instance-independent, mark `staticmethod`/`classmethod`.

## Contracts vs External Validation

- `isinstance` / `is None` checks are for EXTERNAL optionality: CLI arguments not supplied, config-file fields absent, sidecar/media (ffprobe)-sourced data missing, or a genuine API Optional. These are normal conditionals.
- PROGRAMMATIC contracts — values our own construction guarantees (a dependency result after the dependency walk, a COMPLETE recovery row's winning file, a resolved config after `resolve()`), are enforced with `assert` (with a short why-message) or by tightening the footprint to the concrete type. Never with silent `if x is None` fallbacks, and not with exception raises for programmer errors.
- Mandatory contract methods are `@abstractmethod` — never a bare `raise NotImplementedError`.
- Model fields capturing external facts (ffprobe `start_time`, `pix_fmt`, …) are retained even when currently unread — data preservation is not dead code.

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
- See `.kiro/specs/2026-03-17 ffmpeg-unified-runner/` for full requirements and design rationale

## Pipeline Phase Contract

These rules govern how pipeline phases interact with each other and manage their own state.

- **Sidecar ownership.** Each phase owns its sidecar YAML file. It may persist results needed between reruns (detected crop, frame counts, etc.) and incoming settings whose change invalidates the phase's work (e.g. crop parameters for encoding — changing them requires a full re-encode). A phase is prohibited from reading or writing another phase's sidecar directly; it must go through the phase object's API.

- **Artifact ownership.** Each phase owns its inputs, intermediate results, and output artifacts. Other phases must obtain resulting artifacts only by calling the phase object — never by scanning the filesystem directly. For example: do not scan for successful encoding attempts; get them from the encoding phase along with their artifact status.

- **Artifact states.** A phase produces params and artifacts. Each artifact carries an explicit state: wanted & fully produced, wanted but failed/partial, or not wanted. The phase's public result must contain all wanted artifacts with their status — callers pick what they need directly and immediately see which artifacts succeeded or failed, without needing to reason about what was unwanted. Unwanted artifacts are internal phase mechanics and must never leak to callers.

- **Recovery protocol.** Each phase owns its own recovery from incoming parameters and on-disk data. Recovery must be exhaustive: recover every artifact the phase can account for — wanted and complete, wanted but partial, and unwanted but present on disk — each with its correct status. This gives the phase a complete picture of current state before deciding what work remains. For each artifact: attempt full recovery first; if that fails, check intermediate results to salvage partial work; only then plan the remaining work. Phase must use deterministic, reproducible naming so recovery is reliable.

- **Atomicity.** All results — intermediate, final, or otherwise — must follow the `.tmp`-then-rename protocol. There must never be a partial result without a `.tmp` extension on disk. This guarantees clean recovery and allows full trust in any non-`.tmp` artifact found on disk.

- **Invalidation.** Each phase is responsible for invalidating its own artifacts and intermediate results when incoming state changes between reruns. Parameters required for change detection must be persisted in the phase's sidecar.

- **Cleanup.** Each phase must respect the user-configured cleanup level and clean up its intermediate results accordingly.

- **Forced invalidation.** Each phase must respect forced invalidation requests from the user.
