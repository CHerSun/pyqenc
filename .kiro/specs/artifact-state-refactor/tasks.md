# Implementation Plan — Artifact State Refactor

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-09
- Completed: 2026-09-10

## Overview

This plan splits `ArtifactState` into a completeness-only three-value enum (`ABSENT`, `PARTIAL`, `COMPLETE`) and moves selection into a new `Artifact.wanted: bool = True` field. It removes `STALE` (replaced by `wanted=False` + correct completeness), renames `ARTIFACT_ONLY` → `PARTIAL`, drops the `all_tracks` parameter and the `stale` count from downstream APIs, and derives the extraction stream table entirely from the internal artifact list.

Tasks are sequenced foundational-first: the enum change (`state.py`) and the `Artifact.wanted` field (`phase.py`) are the base everything else builds on, followed by the `log_recovery_line()` signature change, then per-phase updates (extraction is the most complex), then the pure renames in chunking/merge/encoding, then tests, then the cross-spec review.

Implementation language: Python 3.13 (the design specifies concrete Python throughout). Tests use pytest with Hypothesis for property-based tests and verify observable behavior only, never internal state.

## Tasks

- [x] 1. Establish the completeness/selection foundation
  - [x] 1.1 Split `ArtifactState` into a completeness-only enum and rename `ARTIFACT_ONLY` → `PARTIAL` project-wide
    - In `pyqenc/state.py`, remove the `STALE` enum value, leaving exactly `ABSENT`, `PARTIAL`, `COMPLETE`
    - Rename the `ARTIFACT_ONLY` enum member to `PARTIAL` PROJECT-WIDE using the rope refactoring MCP `rename_symbol` tool — this renames the identifier and updates all references and imports across the codebase (phases, `recovery.py`, `optimization.py`, tests) in one operation; do NOT hand-edit each callsite
    - After the rope rename, manually update the enum VALUE string from `"artifact_only"` to `"partial"` (rope renames the identifier, not the string value), and remove the DUPLICATED `ARTIFACT_ONLY = "artifact_only"` line that currently appears twice in `pyqenc/state.py`
    - Update the `state.py` module docstring and the `ArtifactState` class docstring to the three-value completeness model (`ABSENT`/`PARTIAL`/`COMPLETE`), removing the `STALE` and `ARTIFACT_ONLY` descriptions and documenting `PARTIAL` as the protected-investment / not-ready state
    - Run `uv run ruff check pyqenc/state.py --fix`
    - NOTE: because the `ARTIFACT_ONLY` → `PARTIAL` identifier rename is performed here project-wide via the rope MCP, later phase tasks do NOT repeat it; they only handle non-rename work plus docstring/comment cleanup
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.9_

  - [x] 1.2 Unify `log_recovery_line()` to derive counts from the internal artifact list
    - In `pyqenc/utils/log_format.py`, change `log_recovery_line()` to take `(log, artifacts: list[Artifact], unit="artifact")` — accepting the phase's INTERNAL artifact list (including `wanted=False` entries), removing the old `complete`/`pending`/`stale` int parameters
    - Derive all counts internally: `total = len(artifacts)`; `unwanted` = count `not a.wanted`; `complete`/`partial`/`absent` = counts of `a.wanted and a.state == COMPLETE|PARTIAL|ABSENT`; always show all five even when zero
    - Emit the line `Recovery: {total} total, {unwanted} unwanted — {complete} complete, {partial} partial, {absent} absent — {suffix}` (suffix `resuming` if any complete else `full run needed`) and RETURN the same string so callers set `PhaseResult.message` from it
    - Run `uv run ruff check pyqenc/utils/log_format.py --fix`
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 2. Add `wanted` selection field to `Artifact` and update `PhaseResult`
  - [x] 2.1 Add `wanted: bool = True` to the `Artifact` base dataclass in `pyqenc/phase.py`
    - Update the `Artifact` class docstring to document `wanted`: its derived nature (from stream filters + pipeline mode for extraction, scene detection for chunking), its orthogonality to completeness, `True`/`False` semantics, and that unwanted artifacts are retained in place and are not deletion candidates
    - Confirm the default `True` keeps all existing construction sites unchanged
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 2.2 Update `PhaseResult` docstring and verify `pending` reads `PARTIAL`
    - Update the `PhaseResult` docstring so `artifacts` states it contains only `wanted=True` entries and that `pending`/`complete` derive from it without extra filtering
    - Verify the `pending` property reads `a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)` — the `ARTIFACT_ONLY` → `PARTIAL` identifier was already renamed project-wide by the rope MCP in task 1.1, so this is a confirmation/cleanup step, not a manual rename; leave `complete` and `is_complete` logic unchanged
    - Run `uv run ruff check pyqenc/phase.py --fix`
    - _Requirements: 4.1, 4.2, 4.3, 6.6, 1.3_

  - [ ]* 2.3 Write property test for wanted-only exposure and pending/complete subset
    - **Property 1: wanted=False artifacts do not appear in PhaseResult.artifacts**
    - **Property 2: pending and complete are subsets of wanted artifacts**
    - Build a `PhaseResult` from a Hypothesis-generated mix of wanted/unwanted artifacts; assert no `wanted=False` entry appears in `artifacts`, and that `pending ∪ complete ⊆ artifacts` with every `artifacts` entry `wanted=True`
    - **Validates: Requirements 4.1, 4.2**

- [x] 3. Refactor `ExtractionPhase` recovery, stream table, and counts
  - [x] 3.1 Rework `_recover()` to enumerate all tracks and assign `wanted` + completeness
    - Iterate every track from ffprobe in index order (not just filter-selected tracks); set `wanted = track in selected_tracks`, and force `wanted=False` for video/timestamp artifacts when `video_required=False` — the old filter-change special case that assigned `STALE` via `expected_names` is deleted, since filter changes now surface naturally as `wanted=False` tracks
    - Assign the correct subclass (`VideoArtifact`/`AudioArtifact`/`OtherArtifact`) with `state=COMPLETE` when the component is present in the single on-disk listing else `ABSENT`; add the `TimestampArtifact` row driven by the same rules
    - _Requirements: 3.1, 3.2, 3.5, 3.6, 3.7, 3.8, 5.5, 1.7, 1.8_

  - [x] 3.2 Change `_log_stream_table()` signature and derive columns from artifacts
    - Change the signature to `_log_stream_table(artifacts: list[ExtractionArtifact])`, removing the `all_tracks` parameter
    - Derive the "Want" column from `artifact.wanted` (`True` → `✔`, `False` → `✘`) and the "Present" column from `artifact.state` (completeness) ONLY, independent of `wanted` (`COMPLETE` → `✔`; `ABSENT` or `PARTIAL` → `✘`); the two columns are orthogonal and neither influences the other. Iterate in artifact order; include `TimestampArtifact` rows naturally
    - Update the callsite to pass only the internal artifact list
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 5.7_

  - [x] 3.3 Update `_execute_extraction()` absent-names filter and `run()` recovery counts
    - Change `absent_names` to `{a.path.name for a in artifacts if a.wanted and a.state == ArtifactState.ABSENT}` so unwanted artifacts are never extracted and are preserved in the final list unchanged
    - Filter the internal list to `wanted=True` before constructing the `ExtractionPhaseResult`
    - REMOVE the `_recovery_message()` function; call the unified `log_recovery_line(logger, internal_artifacts)` with the INTERNAL (unfiltered) list and set `PhaseResult.message` from its returned string; do not compute complete/pending counts locally
    - Run `uv run ruff check pyqenc/phases/extraction.py --fix`
    - _Requirements: 3.6, 4.1, 6.1, 6.3, 6.5_

  - [ ]* 3.4 Write property tests for extraction stream table and filter selection
    - **Property 3: ExtractionPhase stream table row count equals total track count**
    - **Property 4: Filter-excluded tracks produce wanted=False artifacts**
    - Using Hypothesis-generated track sets and include/exclude filters, assert the logged row count equals the ffprobe stream count (plus timestamps row when `video_required=True`), and that filter-excluded tracks appear internally with `wanted=False` but never in `PhaseResult.artifacts`
    - **Validates: Requirements 5.1, 5.5, 3.1, 3.2, 4.1**

  - [ ]* 3.5 Write unit test for the STALE → wanted=False migration in extraction
    - Bug guarded: a filter-changed file that used to be marked `STALE` must now be observable as `wanted=False, state=COMPLETE` and must be excluded from `PhaseResult.artifacts` while remaining on disk
    - _Requirements: 3.1, 3.2, 3.5_

- [x] 4. Refactor `AudioPhase` recovery and counts
  - [x] 4.1 Replace `STALE` assignments and update recovery/counts in `pyqenc/phases/audio.py`
    - In `_recover()`, mark codec/bitrate-changed delivery files and non-terminal (surplus) delivery files as `state=COMPLETE, wanted=False`
    - Change the "marking all artifacts STALE" log message to reflect `wanted=False`; clean up any `ARTIFACT_ONLY` mentions in docstrings/comments (identifier rename handled centrally in task 1.1)
    - REMOVE the `_recovery_message()` function; call the unified `log_recovery_line(logger, internal_artifacts)` with the internal list and set `PhaseResult.message` from its returned string; do not compute counts locally
    - Run `uv run ruff check pyqenc/phases/audio.py --fix`
    - _Requirements: 3.3, 3.4, 3.5, 6.1, 6.3, 6.5_

  - [ ]* 4.2 Write property test for codec-changed audio marking
    - **Property 6: Codec-changed audio files are wanted=False, state=COMPLETE**
    - With Hypothesis-generated prior/current codec-bitrate combinations that differ, assert every existing delivery file appears internally with `wanted=False` and `state=COMPLETE` and is absent from `PhaseResult.artifacts`
    - **Validates: Requirements 3.3**

- [x] 5. Unify recovery reporting and clean up docstrings in chunking, merge, and encoding
  - [x] 5.1 Unify recovery reporting and clean up docstrings in `pyqenc/phases/chunking.py`
    - REMOVE the `_recovery_message()` function; call the unified `log_recovery_line(logger, internal_artifacts)` with the internal list and set `PhaseResult.message` from its returned string; drop any local complete/pending count computation and any `stale=` argument
    - Clean up any remaining `ARTIFACT_ONLY` mentions in docstrings/comments, replacing with `PARTIAL` (the identifier rename is handled centrally in task 1.1; rope does not touch comments/docstrings)
    - Run `uv run ruff check pyqenc/phases/chunking.py --fix`
    - _Requirements: 1.3, 3.5, 6.1, 6.3, 6.5_

  - [x] 5.2 Unify recovery reporting and clean up docstrings in `pyqenc/phases/merge.py`
    - REMOVE the `_recovery_message()` function; call the unified `log_recovery_line(logger, internal_artifacts)` with the internal list and set `PhaseResult.message` from its returned string; drop any local complete/pending count computation and any `stale=` argument
    - Clean up any remaining `ARTIFACT_ONLY` mentions in docstrings/comments, replacing with `PARTIAL` (the identifier rename is handled centrally in task 1.1; rope does not touch comments/docstrings)
    - Run `uv run ruff check pyqenc/phases/merge.py --fix`
    - _Requirements: 1.3, 3.5, 6.1, 6.3, 6.5_

  - [x] 5.3 Unify recovery reporting and clean up docstrings in `pyqenc/phases/encoding.py`
    - REMOVE the `_recovery_message()` function; call the unified `log_recovery_line(logger, internal_artifacts)` with the internal list and set `PhaseResult.message` from its returned string; drop any local complete/pending count computation and any `stale=` argument
    - Clean up any remaining `ARTIFACT_ONLY` mentions in docstrings/comments, replacing with `PARTIAL` (the identifier rename is handled centrally in task 1.1; rope does not touch comments/docstrings)
    - Run `uv run ruff check pyqenc/phases/encoding.py --fix`
    - _Requirements: 1.3, 3.5, 6.1, 6.3, 6.5_

- [x] 6. Checkpoint - Ensure all tests pass
  - Run `uv run python -m pytest tests/unit/ -x -q` and ensure the suite is green; ask the user if questions arise.

- [x] 7. Update existing tests and add cross-cutting migration tests
  - [x] 7.1 Update test logic for the STALE removal and old-name mentions
    - The `ARTIFACT_ONLY` IDENTIFIER in test files (`tests/unit/test_phase_result.py`, `tests/unit/test_recovery_state.py`, and any others) is already renamed to `ArtifactState.PARTIAL` by the central rope rename in task 1.1 — do NOT hand-edit those identifiers here
    - This task covers updating test LOGIC/assertions for the STALE removal: `STALE` has no direct rename, so assertions that expected `ArtifactState.STALE` must move to `wanted=False` (with the correct completeness state); each updated assertion must target observable behavior, not internal state
    - Fix any remaining test docstring/comment mentions of the old `STALE`/`ARTIFACT_ONLY` names
    - _Requirements: 1.2, 1.3, 3.5_

  - [ ]* 7.2 Write property test asserting STALE is never assigned
    - **Property 5: STALE is never assigned after the refactor**
    - Assert `ArtifactState` has no `STALE` member and that no artifact produced across phase recovery carries a `STALE` state
    - **Validates: Requirements 1.2, 3.3, 3.4**

  - [ ]* 7.3 Write property test asserting log_recovery_line receives no stale argument
    - **Property 7: log_recovery_line never receives a stale argument**
    - Assert the `log_recovery_line()` signature no longer accepts `stale` (calling with `stale=` raises `TypeError`), guarding against a callsite regression
    - **Validates: Requirements 6.2, 6.3**

  - [ ]* 7.4 Write unit test for the ARTIFACT_ONLY → PARTIAL rename behavior
    - Bug guarded: a recovery path that previously produced `ARTIFACT_ONLY` (primary file present, sidecar missing) must now produce `PARTIAL` and still be counted in `pending` with identical observable behavior
    - _Requirements: 1.3, 4.2_

  - [ ]* 7.5 Write property test for unified recovery-line counts
    - Assert that for any Hypothesis-generated internal artifact list, `log_recovery_line()` returns/logs a message whose total = len(list), unwanted = count of `wanted=False`, and complete/partial/absent equal the wanted-only completeness counts, with all five always present
    - _Requirements: 6.1, 6.2_

- [x] 8. Final checkpoint - Ensure all tests pass and lint is clean
  - Run `uv run python -m pytest tests/unit/ -x -q` and `uv run ruff check pyqenc/`; ask the user if questions arise.

- [x] 9. Cross-spec review and completion
  - Review this spec against related specs (`probe-phase-refactor`, `config-refactor`, `phase-recovery-refactor`, `phase-object-model`, `merge-phase-revamp`, and any others touching `ArtifactState`/`Artifact`/phase recovery), reconstructing the timeline from Created/Completed dates or file timestamps
  - Add a cross-spec summary to the top of this spec and to each affected spec noting what was superseded or changed between them
  - Update the `- Completed:` date in `requirements.md`, `design.md`, and this `tasks.md` to the completion date once all tasks above are done
  - _Requirements: (documentation task; no functional requirement)_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; they cover property-based (Hypothesis) and unit tests.
- Each task references the specific requirement clauses it satisfies for traceability.
- Property tests validate the 7 universal correctness properties from the design; unit tests cover the two migration-specific behaviors (STALE → wanted=False, ARTIFACT_ONLY → PARTIAL).
- The `ARTIFACT_ONLY` → `PARTIAL` identifier rename is performed project-wide via the rope refactoring MCP (`rename_symbol`) in task 1.1, which updates the enum member and all references/imports across phases, `recovery.py`, `optimization.py`, and tests in one operation; only the value-string (`"artifact_only"` → `"partial"`), docstring, and comment edits are done manually.
- All tests verify observable behavior only, never internal state, per project coding standards.
- Test command: `uv run python -m pytest tests/unit/ -x -q`. Lint: `uv run ruff check <file> --fix`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "4.1", "5.1", "5.2", "5.3"] },
    { "id": 2, "tasks": ["2.2", "3.1", "4.2"] },
    { "id": 3, "tasks": ["2.3", "3.2"] },
    { "id": 4, "tasks": ["3.3"] },
    { "id": 5, "tasks": ["3.4", "3.5", "7.1"] },
    { "id": 6, "tasks": ["7.2", "7.3", "7.4", "7.5"] }
  ]
}
```
