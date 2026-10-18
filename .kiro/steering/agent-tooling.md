# Agent Tooling Priorities

Which tools to reach for when analyzing and refactoring code.

## Code analysis

- When analyzing or navigating Python code (find references, go to definition, get symbols, rename, diagnostics, etc.) — **always prefer MCP tools first** (e.g. `ty-via-mcp`, `codebase-memory-mcp`).
- Only fall back to text-based tools (`grep`, `sed`, `awk`, `grep_search`, etc.) when the MCP tool cannot accomplish the task.

## Refactoring

- For structural code changes (renaming a symbol/module, moving a class or function, extracting a method, inlining a variable) prefer the refactoring MCP (e.g. rope) over manual multi-file text edits. In practice it gives more consistent, complete results — it updates the definition plus every reference and import in one operation, which manual edits tend to miss.
- The MCP renames identifiers only. It does NOT touch string literals (e.g. an enum's `value`), docstrings, or comments — do those manually as a follow-up.
- It also cannot perform semantic changes that are not pure renames/moves (e.g., splitting one concept into two, changing logic). Those stay manual.
- Verify with a project-wide search after the MCP refactor to catch any leftover string/docstring/comment mentions, then run the gates and tests (see [`commands-and-gates.md`](commands-and-gates.md)).
