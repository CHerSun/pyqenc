# Agent Workflow

Process rules: approval, versioning, commits, the backlog, and the tests gate.

## Approach approval

- **Before making any code changes — discuss the proposed approach with the user and get explicit approval first.**

## Versioning & Commits

- **Versioning**: [Semantic Versioning](https://semver.org/) — `MAJOR.MINOR.PATCH` in `pyqenc/__init__.py` (`__version__`); `pyproject.toml` reads it dynamically via `tool.setuptools.dynamic`, so bump it there only. Pre-alpha: breaking changes may still land as MINOR bumps.
- **Commits**: [Conventional Commits](https://www.conventionalcommits.org/) — `type(scope): summary`, lowercase imperative subject, body bullets list only the actual changes (no test results, findings, or verification history). Version bumps are included as a body bullet.
- Only on convergence with `main` branch. If doing a pull request or direct commit to main - bump the version.
- Separate branch for spec implementation with single pull request.
- Minor changes (non-spec) or hot-fixes could go to main directly.

## Work location

- All code remains in the project folder.

## Project backlog (`TODO.md`)

- Known issues and findings awaiting a design decision live in `TODO.md` in the project root.
- Entries there are marked "needs thinking" — they are observations with evidence, not work orders: discuss the approach with the user before implementing any of them.
- TODO.md holds open items only. Once a plan covering an item is finalized (or the item is fixed), remove the item outright — no "resolved" sections, no rewording into a historical record. Git history and specs are the record.
- Keep the file current: record new notable findings there.

## Tests

- Tests should never check internal state, only observable behavior.
- For each test there must be a bug we are trying to eliminate (write the bug conditions inside the test).
- **Mandatory read before writing, modifying, or assessing any test:** [`agent-testing.md`](agent-testing.md) — the QA & testing rules (PyTest + Hypothesis; property-based first; black-box public-surface testing, pinning/characterization tests and internal mocking forbidden; mandatory happy-path/properties/error-handling structure; the red-green-refactor verification loop; pyqenc-specific translation).
