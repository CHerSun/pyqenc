# Steering doc for clarification on which commands agent should be using

## Code Analysis Tool Priority

- When analyzing or navigating Python code (find references, go to definition, get symbols, rename, diagnostics, etc.) — **always prefer MCP tools first** (e.g. `ty-via-mcp`).
- Only fall back to text-based tools (`grep`, `sed`, `awk`, `grep_search`, etc.) when the MCP tool cannot accomplish the task (e.g. non-Python files, MCP unavailable, or task is inherently text-based).

- To check with ruff use `uv run ruff ...`.
- To run tests use `uv run python -m pytest ...`.
- To run the project use `uv run pyqenc` with required arguments.
- Don't use pipes when running pipeline - this ruins alive_progress bar display for the end-user.
- Use `steering/environment.md` for local environment details, like workdir, sample target, etc.
- When writing tests - public (external) behavior or expected behavior must be tested, not internal implementation. For each test there must be a bug we are trying to eliminate (write the bug conditions inside the function).