# Agent Tooling Priorities

Which tools to reach for when analyzing and refactoring code.

## Code analysis

Pick the tool by question shape:

- **"Where is X implemented?" / "find code like this"** — the `semble` MCP first: `search` for a natural-language or code query (`content="docs"`, `"config"`, or `"all"` to include non-code files), `find_related` for code similar to a given file+line. It returns the exact file and line — navigate there and read the file; do not grep for the same content afterwards. CLI fallback when MCP is unavailable: `uvx semble search "query" <path> -k 3 --format text --max-snippet-lines 10`.
- **"Who calls/uses this symbol? What does it call? Outline this file."** — the `treesitter-mcp` MCP: `find_usages` returns the definition plus every reference with owning scopes in one answer, `call_graph` traces callee/caller edges with file+line, `code_map`/`type_map`/`view_code` give token-budgeted structural views, `preview_impact`/`affected_by_diff` show the blast radius of an edit. It is syntax-grade, not compiler-grade — a name match is a candidate, not a proof; confirm dynamic-dispatch indirection (`getattr`, plugin registries) manually.
- **"Everywhere token T appears"** (census across code, tests, docs, TODO.md, .kiro) — `rg` in the shell (`rg -c T --sort path` for the per-file count census). No index or server involved.
- **"Where did we discuss X?"** (meaning-based search over markdown: docs, specs, plans, TODO) — the `semantic-search` CLI (bborbe; sentence-transformers + FAISS; weighted per file name/frontmatter/H1; index cached under `%LOCALAPPDATA%\semantic-search\Cache`): `CONTENT_PATH=C:\Users\CHerSun\Dev\pyqenc semantic-search search "query" -n 5`. Slow start (~3 min model load per process — run it in the background, not inline) — reach for it only when rg and semble's docs mode can't pin the discussion down; its top-hit precision is the payoff. Installed via `uv tool install --index https://download.pytorch.org/whl/cpu --from git+https://github.com/bborbe/semantic-search semantic-search`; upgrade with `uv tool upgrade semantic-search`. Corpus scope is governed by `.semanticignore` (junk dirs excluded; `.zcode/plans` deliberately indexed).
- Diagnostics and type errors — the static gate (`uvx ty check`, see [`commands-and-gates.md`](commands-and-gates.md)).
- Fall back to broader text patterns (`rg`) only when the tools above cannot answer.

## Refactoring

- For structural code changes (renaming a symbol/module, moving a class or function, extracting a method, inlining a variable) prefer the refactoring MCP (e.g. rope) over manual multi-file text edits. In practice it gives more consistent, complete results — it updates the definition plus every reference and import in one operation, which manual edits tend to miss.
- The MCP renames identifiers only. It does NOT touch string literals (e.g. an enum's `value`), docstrings, or comments — do those manually as a follow-up.
- It also cannot perform semantic changes that are not pure renames/moves (e.g., splitting one concept into two, changing logic). Those stay manual.
- Verify with a project-wide search after the MCP refactor to catch any leftover string/docstring/comment mentions, then run the gates and tests (see [`commands-and-gates.md`](commands-and-gates.md)).
