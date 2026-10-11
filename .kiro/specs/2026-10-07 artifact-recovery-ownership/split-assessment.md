# Invalidation/recovery split — design-level assessment (D12, Req 57)

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-07
- Scope: the mandatory D12 verification, run against the **finalized requirements + design** (post stage-1 approval). A second, post-implementation run of this same checklist is a spec task (design §8 step 9).

## Verdict

**The split is clean and implementable.** Every phase expresses as: `_invalidate()` = key-triggered effects (disk-only) → `_recover()` = classification (+ consumption-triggered curation) → gate → execute. No phase needs recovery output to decide invalidation; no in-memory handoff exists anywhere.

## Per-phase check

| Phase | `_invalidate` (key-triggered, disk effects) | `_recover` (classification) | Clean? |
| --- | --- | --- | --- |
| Job | identity ≠ → fatal / permission (rewrite `job.yaml`); path-only → locator rewrite | File row COMPLETE iff sidecar current | ✓ |
| Extraction | identity ≠ → fatal / wipe `extracted/` + sidecar; **sidecar missing → conservative wipe** (vacuous on first run) | one listing → consume → states; leftovers retained | ✓ |
| Probe | identity ≠ → re-probe; crop equal → no-op; differs → re-resolve (all = rewrite own sidecar) | sidecar currency → row state | ✓ |
| Chunking | identity ≠ → fatal / wipe sidecar; params ≠ → wipe sidecar (re-detect happens via the gate) | boundaries → derived windows | ✓ |
| Optimization | identity / facet / fingerprints → fatal / wipe namespace (permission); mode / targets / q / sampling / chunks → wipe winners + sidecar rewrite (auto); sidecar missing → conservative wipe | pair ledger by static names; to-test projection; derive-only pending for a stale table | ✓ |
| Encoding | **empty** (the case study) | pair ledger consumption + residual curation (consumption-triggered effects) | ✓ |
| Audio | chain fingerprint ≠ / removed → delete that chain's outputs (**by composed names**); sidecar missing → wipe the audio dir | one listing → membership; surplus surfaced | ✓ |
| Merge | **identity only** (from any output's provenance; low-N read sanctioned) → fatal / permission wipe `merged/` | per-file acceptance (provenance compare → COMPLETE / re-merge / re-measure) | ✓ |

## The five nuances (resolved, documented)

1. **Consumption-triggered curation fires during recovery** (encoding: orphan dirs, foreign names). The split is therefore: `_invalidate` = key-triggered effects; `_recover` = classification **plus consumption-triggered effects**. The load-bearing invariant (disk-only, no in-memory coupling) holds; the effect vocabulary is shared (design §2a).
2. **Unknown-currency conditions simplify**: "own sidecar missing" ALONE triggers the conservative wipe — the wipe is vacuous when no artifacts exist, so invalidation never needs a listing or an "artifacts present?" probe. ("With artifacts present" in earlier wording was descriptive, not load-bearing.)
3. **O-6 test-chunk set-presence is input derivation, not invalidation**: it compares persisted ids vs the current chunk set (both in memory) and its outcome is a selection for execute — no disk effect, so it lives on the recovery/execute side with wanted derivation.
4. **Audio's chain deletions use composed names, not directory parsing**: the persisted chain map names the chains; expected output names compose from the dependency's stream set + the entity's composer; those exact names are deleted. No listing, no parsing in `_invalidate`.
5. **Derive-only pending is the one sanctioned axis crossover**: a currency condition (stale table/summary) expressed through the gate's pending, bounded (no disk effect, ledger truthful, Req 56). Documented as deliberate, not drift.

## Two-axis distinctness check

Presence states (ABSENT/PARTIAL/COMPLETE) describe **components on disk**; currency describes **parameters of record**. They meet only at the pending gate, which legitimately consumes both: presence-driven pending (any wanted row non-COMPLETE) and explicit derive-only pending (nuance 5). No phase expresses a currency condition as a completeness state or vice versa outside that documented crossover.

## Post-implementation re-run (spec task 10.1)

Run 2026-10-08 against the landed code, re-verified after the same-day audit fixes. **The split holds as implemented.**

- **(a) No `_invalidate` reads a directory listing for condition detection.** Grep-audit over every `_invalidate` body: condition detection reads only the phase's own sidecar (one `load_model`). Three listing-shaped sites are each sanctioned: merge's per-output provenance enumeration (the design's own per-phase row — "low-N read sanctioned", O(strategies) at the deliverable layer, and it belongs to the identity *condition*, which merge owns pre-recovery); optimization's `_wipe_encoded_dir` / encoding's curation (effect *mechanics* — enumerating what a wipe deletes, not detecting a condition); audio's `any(iterdir())` existence probe was REMOVED in the 2026-10-08 fixes (the conservative wipe now fires on the missing sidecar alone, nuance 2's letter).
- **(b) No state passes from `_invalidate` to `_recover` in memory.** Each `_recover` re-reads its sidecar / listing from disk. Two instance stashes exist, both outside the banned handoff: job's `_file` (the live probe result — recovery re-derives it defensively when unset) and optimization's `_current_probe`/`_chunks_fingerprint` (consumed by `_execute`'s saves, never by `_recover`).
- **(c) The two-axis vocabulary is in the code.** `Phase._invalidate`/`_recover` docstrings state the parameter-currency vs presence-completeness distinction and the disk-effects invariant; the arch doc carries the full "Invalidation and recovery ownership" section.
- **(d) Encoding's `_invalidate` is the case study** — present, documented as deliberately empty (the shared namespace's keys all live at optimization).
- **(e) The five nuances hold**: (1) encoding's curation fires in `_recover` as the sanctioned consumption-triggered effect; (2) the missing-sidecar wipes fire on the missing sidecar alone with no existence probe (audio fixed 2026-10-08; extraction's probe was already log-only); (3) O-6 set-presence is input derivation in `_resolve_test_chunks`, no disk effect; (4) audio's chain deletions compose exact names (`_composed_output_names` via the entity composer), no parsing; (5) derive-only pending remains the single documented axis crossover (optimization's stale-table branch).

Corrections landed from the 2026-10-08 external audit (all fixed same day): the probe sidecar serializer silently dropped the identity key and crop provenance (Req 23/31/33 — probe's fatal was dead code from disk); Req 5's loud collision guard was absent (now `claim_expected_name` at every consumption site + merge's duplicate-strategy check); merge's sidecar-less PARTIAL re-measured instead of re-merging (Req 49/50); optimization's per-strategy args wipe cascaded into the §99 all-winners wipe (Req 39's "only the changed strategies").
