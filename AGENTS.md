# AGENTS.md

Where to look. Rules first; when anything conflicts, steering docs win.
Read related docs if needed for the task.

## Rules — `.kiro/steering/`

Product:

- `project-goals.md` — project philosophy: source fidelity, ffmpeg usage

Process:

- `agent-workflow.md` — approach approval, versioning/commits/branching, git, TODO-backlog rules, the tests gate
- `agent-specs.md` — spec authoring: EARS notation, folder lifecycle
- `agent-testing.md` — test rules (PyTest + Hypothesis, black-box); mandatory read before touching tests

Code:

- `code-style.md` — Python style, typing, docstrings, naming (two-name doctrine; reuse `docs/glossary.md` vocabulary — new terms need approval, same gate as dependencies), constants, logging
- `type-contracts.md` — optionality/None policy, contracts vs external validation (assert discipline)
- `api-architecture.md` — public API surface, CLI-first, async policy, config/plan doctrine
- `paths-and-subprocesses.md` — LongPath rules, atomic `.tmp` writes, subprocess command building, ffmpeg runner
- `pipeline-phases.md` — phase contract: sidecar/artifact ownership, artifact states, recovery, invalidation, cleanup

Verification & tooling:

- `agent-tooling.md` — MCP-first code analysis, rope for refactors
- `commands-and-gates.md` — run/test commands, the three green gates (ruff + ty + pytest)
- `dependencies.md` — approved packages/external programs; additions need explicit approval

Local:

- `environment.md` — this machine's specifics (paths, shell quirks, tools on PATH); template: `environment.sample.md`

## Reference — `docs/`

- `glossary.md` — THE vocabulary registry (one term, one meaning, reused verbatim)
- `architecture.md` — current architecture + design decisions (current state only; history lives in specs)
- `quality-targeting.md`, `audio-processing.md`, `cli-reference.md` — domain guides
- `.kiro/specs/<date> <name>/` — implemented specs: the historical record of every design ruling

## Work state

- `TODO.md` — the backlog (keep the `Last known = N` tail marker updated)
- `tests/README.md` — test organization

## Working

- Static gates via `uvx ruff check` / `uvx ty check`; tests via `uv run python -m pytest`; run via `uv run pyqenc` (details: `commands-and-gates.md`)
- Project state - alpha. No backward compatibility plumbing, work-dirs are run-once — persisted-format changes are clean renames
