# Paths, Files & Subprocesses

Everything touching the filesystem or spawning processes.

## Paths & Files

- `LongPath` from `pyqenc.utils.long_path` transparently handles Windows extended-length paths (>260 chars). NO `str` for paths.
- **Signatures declare `Path`** — the honest footprint (the body works with any `Path`). **Callers construct `LongPath` at the boundary** where a path first enters our code (CLI args, sidecar/model loads, test fixtures) and pass it through.
- Never re-instantiate `Path(...)`/`LongPath(...)` around an existing path inside logic — chain with `/` and path methods; `LongPath` overrides the composition operators, so the subtype is preserved without re-wrapping. (Pydantic model fields may still be annotated `LongPath` — its schema coerces.)
- Use `LongPath` everywhere a path is constructed, stored, or passed to Python file I/O (`open`, `mkdir`, `exists`, `replace`, `shutil.*`, etc.). This does NOT apply to libraries that handle their own file I/O (JSON, PNG, etc.).
- For any on-disk results use `.tmp`-then-rename protocol for atomicity and consistency enforcement.

## Subprocess command building

- For subprocess cmd building use type hint `list[str|os.PathLike]` and supply `LongPath`/`Path` variables directly (without converting to `str`). The subprocess layer calls `os.fspath()` which injects the `\\?\` prefix when needed. Sub-string arguments that a tool parses itself (mkvmerge `@options.json`, mkvextract `0:timestamps.txt`, ffmpeg filter args) take the plain form via `str(path)` — these parsers are picky and must not receive an extended-length prefix. `str(path)` is otherwise allowed for printing/logging only, never for command building or file operations.

## ffmpeg Execution

All ffmpeg subprocess calls MUST go through the unified runner in `pyqenc/utils/ffmpeg_runner.py`. Never call `subprocess.run`, `asyncio.create_subprocess_exec`, or any other subprocess primitive directly for ffmpeg.

- In async contexts: `await run_ffmpeg_async(cmd, ...)`
- In sync contexts: `run_ffmpeg(cmd, ...)` — raises `RuntimeError` if called from a running event loop
- The runner automatically injects `-hide_banner -nostats -progress pipe:1`, reads stdout/stderr concurrently, parses structured progress blocks, and returns `FFmpegRunResult`
- Pass a `ProgressCallback` (`(frame: int, out_time_s: float) -> None`) for live progress updates
- Pass a `VideoMetadata` instance to have it populated in-place from ffmpeg output
- See `.kiro/specs/2026-03-17 ffmpeg-unified-runner/` for full requirements and design rationale
