# Agent Testing Rules — QA (PyTest + Hypothesis)

Mandatory reading when writing, modifying, or assessing tests. Referenced
from `agent-workflow.md` (## Tests).

## QA & Testing Rules (PyTest + Hypothesis)

You must use PyTest for test structuring and Hypothesis for property-based
testing. Writing rigid "pinning" tests that lock down internal
implementation details or mock private methods is strictly forbidden.
Focus on black-box verification and system invariants.

### 1. Property-Based Testing (Hypothesis) First

* **Define Invariants:** Write Hypothesis tests to validate core properties that must always hold true, regardless of the input values (e.g., `encoder(decoder(x)) == x`, or `total_price >= 0`).
* **Use Proper Strategies:** Utilize built-in Hypothesis strategies (`st.integers()`, `st.text()`, `st.lists()`, `st.builds()`) to generate diverse input data. Do not hardcode arbitrary test values if they can be randomized within a valid domain.
* **Define Bounds Generously:** Use `.filter()` or `assume()` to filter out completely invalid states, but let Hypothesis explore edge cases (empty strings, huge numbers, special characters) to find unexpected crashes.

### 2. PyTest Best Practices

* **Test the Public Interface Only:** Write tests against public functions, modules, and API endpoints. Never import or test functions starting with a single underscore `_private_func`.
* **Pure Fixtures:** Use PyTest fixtures only for state setup (e.g., database sessions, client instances, clean temp directories via `tmp_path`). Do not use fixtures to mock internal logic of the module under test.
* **No Brittle Mocking:** Forbid `unittest.mock.patch` on internal functions of the target module. If an external service must be mocked, mock it at the network layer or infrastructure boundary (e.g., using `pytest-mock` on third-party SDK clients only).

### 3. Mandatory Test Suite Structure

For any new or modified feature, your test file must include:
* **`test_happy_path` (PyTest standard):** 1-2 explicit, readable examples using standard `@pytest.mark` or fixtures to demonstrate expected behavior for a standard developer reading the code.
* **`test_properties_...` (Hypothesis):** At least 1-2 property tests using `@given()` to actively search for edge-case crashes, overflows, or logic boundary violations.
* **`test_error_handling` (PyTest):** Tests using `with pytest.raises(...)` ensuring the code fails gracefully with precise, expected exceptions when given invalid inputs.

### 4. The "Red-Green-Refactor" Verification Loop

Before declaring a task finished, you must mentally or explicitly verify your tests using these steps:
1. **Requirement Check:** Does this test reflect a real business constraint, or does it just mirror the code you just wrote?
2. **Failure Assertion:** If the underlying implementation logic changes or breaks, will this specific test fail? (If a test passes no matter what, delete it).
3. **Self-Documenting Tests:** Write clear, descriptive test names stating the guaranteed behavior (the `should [do something] when [condition occurs]` shape or an equivalent descriptive sentence).

## pyqenc specifics

This is a CLI pipeline — no UI/DOM, no network services. The rules translate to this codebase as follows (2026-10-10 ruling):

- **"Brittle selectors" here means file cosmetics:** never assert yaml text, field order, key sets, key presence/absence, or one-line-vs-nested serialized forms. For persistence code, test save→load value preservation (round-trips), load tolerance for legacy/natural alternate forms, and discriminated-union dispatch via `model_validate` + `isinstance`. Class-level field existence is guarded by `ty`, not by yaml-key assertions.
- **Public surfaces:** the API entry points (`pyqenc.api`, the CLI), `Phase.run()` behavior, sidecar models' save/load contracts, and PUBLIC module functions. Underscore-prefixed helpers are not test surfaces — the §115 suite assessment reconciles existing tests that import or patch them.
- **Infrastructure boundary to mock:** the process boundary — ffmpeg / ffprobe / mkvmerge runners and external shells (`subprocess.run` at the runner seam). Never `patch` private methods or module-internal functions of the code under test.
- **Hypothesis is established here:** exemplars to follow — `tests/test_pts_preservation_properties.py`, `tests/test_app_config_properties.py`, `tests/test_vif_properties.py` (invariants over strategies; the frame-preservation invariant is the canonical system property).
- **Docstrings state the guarded behavior** and its origin (bug/spec requirement), never narrate the test's mechanics.
- **E2e speed rule:** e2e tests that include video encoding always add `--strategies "h265*+ultrafast"` for max speed encoding — the default slow/veryslow presets turn a quality search on a minutes-long clip into hours.
