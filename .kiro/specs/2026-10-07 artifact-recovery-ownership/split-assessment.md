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

## Post-implementation re-run (spec task)

Same checklist against code: (a) grep-audit that no `_invalidate` reads a directory listing; (b) no state passes from `_invalidate` to `_recover` in memory; (c) the two-axis vocabulary in docstrings/docs; (d) encoding's empty `_invalidate` as the case study; (e) the five nuances hold as implemented.
