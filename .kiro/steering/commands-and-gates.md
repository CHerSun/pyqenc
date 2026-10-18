# Commands & Verification Gates

How to run the project and what "green" means.

## Commands

- Static-check gates run via `uvx` (always the newest release — for static analysis newer means better rules and fixed bugs): `uvx ruff check` and `uvx ty check`. The locked dev deps exist for editor/MCP integration; refresh them occasionally with `uv lock --upgrade-package ruff ty` so `uv run` doesn't rot.
- To run tests use `uv run python -m pytest ...`.
- To run the project use `uv run pyqenc` with required arguments.
- Don't use pipes when running the pipeline — this ruins the alive_progress bar display for the end-user.
- Machine-specific run details (work dirs, sample videos, shell quirks, tools on PATH) live in [`environment.md`](environment.md).

## Gates

- A `.githooks/pre-commit` hook mechanically blocks commits on ruff/ty failures (enable per-clone: `git config core.hooksPath .githooks`; pytest is deliberately NOT in the hook — minutes-long, wrong timescale).
- Pylance/pyright is NOT a gate — ruff + ty are the static gates (plus pytest). Run pyright occasionally as a cross-check probe: `uvx pyright --pythonpath .venv/Scripts/python.exe` (the venv flag matters). Expect known non-actionable families: ~51 Path->LongPath pydantic-field strictness (pyright has no plugin API, ignores `__get_pydantic_core_schema__` coercion — conflicts with the models-coerce doctrine), pandas/numpy stub unions, `reportPrivateImportUsage` on matplotlib re-exports, negative-pydantic tests needing per-checker ignores, and a guard-correlated `total_frames_for_summary` in visualization (correct code, long-range correlation). Full gate-adoption recipe (LongPathYaml redefinition first) lives in TODO §80.
- **Green status requires ALL THREE gates passing:** `uvx ruff check` clean, `uvx ty check` clean, and pytest green. A ruff+pytest-only "green" is not green.
