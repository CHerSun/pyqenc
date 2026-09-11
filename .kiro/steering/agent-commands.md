# Steering doc for clarification on which commands agent should be using

## Code Analysis Tool Priority

- When analyzing or navigating Python code (find references, go to definition, get symbols, rename, diagnostics, etc.) — **always prefer MCP tools first** (e.g. `ty-via-mcp`).
- Only fall back to text-based tools (`grep`, `sed`, `awk`, `grep_search`, etc.) when the MCP tool cannot accomplish the task (e.g. non-Python files, MCP unavailable, or task is inherently text-based).

## Refactoring Tool Priority

- Note to self: for structural code changes (renaming a symbol/module, moving a class or function, extracting a method, inlining a variable) prefer the refactoring MCP (e.g. rope) over manual multi-file text edits. In practice it gives more consistent, complete results — it updates the definition plus every reference and import in one operation, which manual edits tend to miss.
- The MCP renames identifiers only. It does NOT touch string literals (e.g. an enum's `value`), docstrings, or comments — do those manually as a follow-up.
- It also cannot perform semantic changes that are not pure renames/moves (e.g. splitting one concept into two, changing logic). Those stay manual.
- Verify with a project-wide search after the MCP refactor to catch any leftover string/docstring/comment mentions, then run ruff and the tests.

- To check with ruff use `uv run ruff ...`.
- To run tests use `uv run python -m pytest ...`.
- To run the project use `uv run pyqenc` with required arguments.
- Don't use pipes when running pipeline - this ruins alive_progress bar display for the end-user.
- Use `steering/environment.md` for local environment details, like workdir, sample target, etc.
- When writing tests - public (external) behavior or expected behavior must be tested, not internal implementation. For each test there must be a bug we are trying to eliminate (write the bug conditions inside the function).
