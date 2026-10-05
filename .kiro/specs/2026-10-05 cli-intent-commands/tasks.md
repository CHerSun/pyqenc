# Implementation Plan — CLI Intent Commands

<!-- markdownlint-disable MD024 -->

- Spec: CLI Intent Commands — implementation plan for the 2026-10-05 spec
- Created: 2026-10-05
- Completed: 2026-10-05

## Overview

Staged declarations-first so the graph is honest before anything derives from
it: the DEPENDS_ON audit lands first (Req 13 — the registry switch is gated on
it), then the framework changes (closure-derived registry, derived video-need,
multi-terminal runner), then the api re-pointing, then the `extract`
materialization (the largest new surface, phase mechanics before CLI), then
the CLI set change + condensation in one motion, then docs and e2e. Until
Task 7 the CLI surface is unchanged, so every intermediate task leaves
existing commands byte-identical in behavior. Requirement ids reference
`requirements.md`.

## Notes

- After each code task run `uv run ruff check .`, `uvx ty check`, and the
  relevant `uv run python -m pytest`.
- Existing-command invariance is the standing regression bar through Task 6:
  `auto`'s product, logs, and reuse behavior must not change until the set
  change deliberately re-shapes them. RESEQUENCED 2026-10-05 (Task 1 note was
  self-contradictory): the MergePhase Audio-dep drop lands in TASK 3's commit
  together with the multi-terminal runner and `run_pipeline`'s re-point to
  `(AudioPhase, MergePhase)` — dropping it earlier would silently remove
  audio from `auto` for two tasks. Task 1 records the over-declaration and
  fixes any OTHER gaps the audit finds.
- E2E on real media only with the speed rule: `--strategies "h265*+ultrafast"`.
- Keyword-only construction for every new model/dataclass field; no aliased
  duplicate imports; no test-only production code.
- §93 PhaseDependencies is NOT this window's work (unified-summaries window
  owns it): Task 1 reads `_dep_result` sites for the audit but rewrites none.
- §98 (api intent-named surface) decides at Task 4: default is to keep the
  current names re-pointed onto `targets=` tuples; rename in-window only if
  the user opts in during review of that task.
- The materialize flag's final name settles in Task 5 (`materialize_av` is
  the working name); it is run-context threaded like `video_required`, never
  an `AppConfig` field.

## Tasks

- [x] 1. DEPENDS_ON audit + dependency table (Req 13, Req 7)
  - Per phase file: enumerate every `_dep_result(X)` / registry fetch and
    compare against the declared `DEPENDS_ON` tuple; record undeclared reads
    and over-declarations; fix the tuples
  - `MergePhase.DEPENDS_ON` drops `AudioPhase` (Req 7) — the one deliberate
    over-declaration; move its scheduling rationale comment to the spec
    history / `auto`'s terminal ordering docs
  - Record the per-phase dependency table (declared deps; consumed results
    and settings) in `docs/architecture.md` — the §54 deliverable
  - Tests: pin `MergePhase.DEPENDS_ON` exact tuple; existing suite must stay
    green (declaration change affects only the merge-terminal walk's phase
    set — audio still executes under today's `run_pipeline` target until
    Task 3 re-points it)

- [x] 2. Closure-derived registry + derived video-need (Req 6, Req 8)
  - New `_dependency_closure(terminals) -> tuple[type[Phase], ...]` next to
    the `PhaseRegistry` alias: depth-first over static `DEPENDS_ON`,
    deterministic topological order with declaration order as tie-break,
    loud error on cycles; `VIDEO_CHAIN` constant lives beside it
  - `_build_registry` constructs exactly the closure — static hand-ordered
    list and the `video_required` Probe-omission branch deleted
  - `video_required` derived as `closure ∩ VIDEO_CHAIN ≠ ∅` at the `_drive`
    boundary, threaded to `ExtractionPhase` through the existing ctor
    channel (timestamps gating + video-row want unchanged in behavior)
  - Tests: closure membership per terminal set; order stability; cycle
    failure; derived video-need matrix (audio → False; video/auto → True;
    extract → False); registry contents for each intent shape

- [x] 3. Multi-terminal runner (Req 5, Req 2, Req 3, Req 4)
  - `Runner` and `_drive` accept an ordered `targets: tuple[type[Phase], ...]`
    (single target = one-element tuple); walks in order over the one registry;
    shared deps hit the per-instance memoization guard on later walks
  - `RunResult.success` = every terminal's result `is_complete`; aggregate
    surface unchanged; dry-run walks all terminals
  - Tests: two-terminal walk executes a shared dependency exactly once
    (Job/Extraction counters); terminal ordering pinned (audio result
    available before encoding starts); single-terminal runs byte-identical

- [x] 4. api re-pointing + plan-free extract_streams (Req 1, Req 4; §98 call)
  - Wrappers pass `targets=` tuples: `run_pipeline → (AudioPhase,
    MergePhase)`, `merge_final → (MergePhase,)`, `encode_chunks →
    (EncodingPhase,)`, `chunk_video → (ChunkingPhase,)`, `extract_streams →
    (ExtractionPhase,)`, `process_audio → (AudioPhase,)`
  - `extract_streams` drops its `plan` parameter (joins `process_audio`'s
    shape); no wrapper sets `video_required` by hand anymore
  - §98 DECIDED 2026-10-05 (implementation-time, default applied): keep the
    six current names re-pointed onto `targets=` tuples this window; the
    intent-named collapse stays TODO §98 for its own pass. `pyqenc/__init__.py`
    re-exports unchanged.
  - Tests: wrapper→targets tuples pinned; the audio-shaped namespace never
    resolves a plan (existing monkeypatch pin stays green)

- [x] 5. Extract materialization — phase mechanics (Req 9.1, 9.2, 9.3, 9.6)
  - Run-scoped materialize flag threaded to `ExtractionPhase` ctor-style
    alongside `video_required`; in materialize mode the selected video and
    audio streams enter the artifact ledger as material rows with real
    destinations under `extracted/` (presence-based completeness)
  - Track extraction via one mkvextract batch first, ffmpeg fallback on
    failure (§53 policy; reuse the `bee491a` per-kind fallback matrix for
    attachments); chapters/timestamps behavior unchanged
  - File naming: extraction-owned composition, `safe_name()` on disk,
    display names in the table (two-name doctrine, no new accessors)
  - `extraction.yaml` records materialized AV rows mode-honestly; stale
    cleanup scopes to entries the current mode wants — materialized AV is
    preserved by later processing runs, never consumed as inputs
  - Tests: want-table rows in materialize mode (AV material, filter-driven
    `wanted`); processing-mode preservation (rerun does not delete);
    naming doctrine pins; forced mkvextract failure exercises the fallback

- [x] 6. Extract command UX — dry-run sizes, no cleanup, no plan (Req 9.4, 9.5)
  - Dedicated `_cmd_extract` handler: source + base + pipeline args minus
    `--cleanup` + filter args; no quality/plan arguments
  - Dry-run: stream table with per-stream destination, expected size, planned
    total (sizes from the enumeration JSON already collected — no extra
    probing); writes nothing; `-y` materializes then prints the results table
  - IMPLEMENTED (amended): per-stream exact sizes need a packet-level scan —
    skipped; the phase logs the plan (per-stream destinations, count, the
    source-size upper bound) on both dry-runs and execute runs (Req 9.4
    amended accordingly in requirements.md)
  - Tests: dry-run writes nothing and lists everything selected; filter
    exclusion respected in the listing; no `--cleanup`/plan args on the parser

- [x] 7. CLI set change + condensation (Req 1, Req 10, Req 2–4 surfaces)
  - `_SubcommandSpec` (name, help, runner, arg_groups, flavor) +
    `_cmd_pipeline(args, spec)` + parser-creation loop for
    `auto`/`video`/`audio`; `extract` uses its dedicated handler; `config`
    and `measure` untouched
  - Remove `chunk`/`encode`/`merge` parsers and handlers; `pyqenc/__init__.py`
    re-exports unchanged names
  - Tests: parser smoke (six commands present, three absent); per-command
    help and arg surfaces; `auto` flavor (files + table) vs plain flavors

- [x] 8. Docs (Req 12)
  - `docs/cli-reference.md` rewritten for six commands
  - `docs/architecture.md`: registry/execution-model description
    (closure-derived, derived video-need, multi-terminal), extraction
    want-table row (materialization), `api.py` surface listing, CLI diagram;
    dependency table landed in Task 1
  - README: command mentions + workdir-tree `extracted/` note (video/audio
    via `extract`)
  - `docs/audio-processing.md` "extracted audio tracks" wording
    disambiguated; `docs/Pipeline flow overview.mmd` extraction outputs
    extended; `docs/quality-targeting.md` + `CONTRIBUTING.md` verified,
    updated only where stale

- [x] 9. E2E on real media (all Reqs)
  - Sample built at `samples/sample-lion-fullhd.mkv` (recipe recorded in
    `samples/SAMPLES.md`; 35 s real content + aac/flac/sub/attachment/
    chapters) — also un-skips the previously-skipped legacy e2e tests.
  - Legacy e2e rot repaired: two forever-skipped tests asserted
    `success is False` for dry-run previews with pending work — stale
    semantics contradicting the runner contract; now assert preview success
    + `phases_needing_work`.
  - FOUND + FIXED (5339b61): FilterInstance params landed as plain BaseModel
    after any model_dump→validate round-trip — latent, exposed by the first
    audio e2e under pydantic 2.13 (plain-BaseModel attribute access now
    raises); both config shapes now always re-validate through the type's
    params model.
  - All five scenarios green: auto (execute + reuse replay), video (no
    audio work), audio (timestamps/probe untouched), two-pass composition
    (auto performs no work), extract (every kind + codec-verified + rerun
    byte-stable). Speed rule applied (`h265+ultrafast`).

- [x] 10. Closeout
  - Cross-spec review per agent-specs: summarize supersessions/changes at the
    top of this spec and the affected older specs (`2026-09-25
    file-stream-model`, `2026-09-28 artifact-model` virtual-stream notes)
  - Verify TODO.md holds no dangling references to removed entries
  - Mark `- Completed:` in all three documents
