# Cleanup Report — `cleanup` branch

Audit table for the 2026-09-30 cleanup pass. Every candidate from the investigation
sweeps gets a row: what it was, why it was flagged, the action taken (delete / inline /
keep / dedup / rewrite — with reason), and the post-check that verified it.
A kept-with-reason row is as valuable as a deleted row.

Scope & rules (approved plan):
- Functionality-preserving; the only intended behavior change is the fixed
  `pyqenc measure` kwarg crash.
- Tests do not count as live references — test-only production code is dead code.
  Sanctioned exception: `QualitySearch` V1/V2 stay (reference for the planned V4, TODO §51).
- Comments: short spec links allowed; essays, task notes and historical narration go.
- DRY: cheap now, costly logged in TODO.md for later.
- Protected symbols (never touched): `display_name`/`safe_name`, pydantic
  validators/serializers, `Phase` template methods & hooks, dunders, argparse
  callbacks, registry `.resolve()` methods.

## Baselines (Stage 0, 2026-09-30, HEAD 09d61ba)

| Check | Command | Baseline |
|---|---|---|
| ruff | `uv run ruff check` | 367 findings (defaults, no config existed; ruff 0.16.8 not pinned) |
| pytest | `uv run python -m pytest -q` | 11 failed / 721 passed / 9 skipped (2m16s) — the 11 known stale audio tests (TODO §43), no others |
| vulture | `uvx vulture pyqenc` | 88 findings (≈25 real, rest pydantic/framework noise) |
| ty | `uv run ty check` | 357 diagnostics (190 invalid-argument-type, 79 unresolved-attribute, …; best-effort scope) |
| size | `grep -c ""` per file | source ≈ 19.6k lines over 32 files |

Codebase-memory-mcp graph re-indexed after baselines (was stale: still showed
deleted `Phase._dep`).

## Stage 1 — ruff

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|
| (rows added during execution) | | | | | | |

## Stage 2 — dead code

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 3 — comments

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 4 — imports

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 5 — inlining

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 6 — DRY

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 7 — repairs

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Stage 8 — tests

| # | Item | Category | What / why candidate | Action taken | Reason | Post-check |
|---|---|---|---|---|---|---|

## Totals

(filled at the end)
