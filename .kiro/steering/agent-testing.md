# Agent Testing Rules — Quality Assurance

Mandatory reading when writing, modifying, or assessing tests. Referenced
from `coding-standards.md` (## Tests).

## Quality Assurance & Testing Rules

You are strictly forbidden from writing "pinning" or "characterization"
tests that freeze implementation details, internal states, or private
methods. Instead, you must follow a black-box, behavior-driven testing
strategy.

### 1. Test the "What", Not the "How"
* **Target Public Surfaces Only:** Write tests exclusively against public
  APIs, public functions, component interfaces, or user workflows.
* **No Internal Mocking:** Do not mock internal module behaviors, private
  functions, or local variables. If you change the internal logic but the
  output remains the same, the tests must still pass.
* **Avoid Brittle Selectors:** When writing UI/E2E tests, use accessible
  roles (e.g., `getByRole('button', { name: /save/i })`) or
  data-attributes (`data-testid`). Never rely on fragile CSS class
  hierarchies or strict DOM nesting.

### 2. Mandatory Test Coverage Types
For every feature or modification, you must provide:
* **Happy Path:** 1-2 tests validating the standard, expected user outcome.
* **Edge Cases:** At least 2 tests exploring boundary conditions (e.g.,
  empty strings, null values, maximum limits, out-of-bounds inputs).
* **Adversarial / Error Handling:** At least 1 test ensuring the system
  fails gracefully, catches exceptions, and returns user-friendly error
  states instead of crashing.

### 3. The "Red-Green-Refactor" Verification Loop
Before declaring a task finished, you must mentally or explicitly verify
your tests using these steps:
1. **Requirement Check:** Does this test reflect a real business
   constraint, or does it just mirror the code you just wrote?
2. **Failure Assertion:** If the underlying implementation logic changes
   or breaks, will this specific test fail? (If a test passes no matter
   what, delete it).
3. **Self-Documenting Tests:** Write clear, descriptive test names using
   the `should [do something] when [condition occurs]` pattern.

## pyqenc specifics

This is a CLI pipeline — no UI/DOM. The rules translate to this codebase
as follows (2026-10-10 ruling, enforced in review):

- **"Brittle selectors" here means file cosmetics:** never assert yaml
  text, field order, key sets, key presence/absence, or one-line-vs-nested
  serialized forms. For persistence code, test save→load value
  preservation (round-trips), load tolerance for legacy/natural alternate
  forms, and discriminated-union dispatch via `model_validate` +
  `isinstance`. Class-level field existence is guarded by `ty`, not by
  yaml-key assertions.
- **Public surfaces:** phase behavior through `Phase.run()` and the
  sidecar models' save/load contracts; pure functions directly (they may
  be module-internal by name — test the function's deterministic contract,
  never incidental internals around it); the API/CLI entry points.
- **No mocking of private internals:** patch at the process boundary
  (ffmpeg/mkvmerge runners, external shells) — patching private helpers
  couples the test to the "how".
- **Naming:** descriptive sentence names stating the guaranteed behavior
  (`test_anchor_change_does_not_remerge`); docstrings state the guarded
  behavior and its origin (bug/spec requirement), never narrate the test's
  mechanics.
