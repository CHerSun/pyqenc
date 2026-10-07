# Logical vs code — per-phase behavior validation

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-06
- The primary validation view: one table per phase, **Logical** (first-principles requirement) beside **Code today** (observed current behavior, summarized — no refs inline), so contradictions read off the row directly. **✓** = code conforms; **GAP** = divergence, with the census ID. Code references sit below each table. Deep dives: `per-phase-audit.md`; cross-phase rules: `invalidation-matrix.md`.
- Rows cover three condition families: input-change invalidation, **missing own sidecar** (unknown currency), and recovery mechanics where behavior diverges from the target protocol.
- **Condition naming (canonical):** *"own persisted content identity ≠ live content identity"*. **Content identity** = the `size + sampled digest` entity. **Live** = Job's instance, computed once per run and carried by the job's `File` (phases read it via the job result; never re-hashed). **Persisted** = the phase's sidecar key. No path participates — path is a runtime locator (decision 11).

---

## Job

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| content identity changed (size/digest), no permission | fatal stop | fatal stop on path/size mismatch (digest not yet implemented); message points at `--force` | ✓ (behavior; digest pending) |
| path changed, content identity same | locator update: rewrite `job.yaml` (path kept for humans), no fatal, no invalidation | fatal mismatch demanding `--force`; with force, a full workdir wipe | GAP J-3 |
| content identity changed + `--force` | permission granted; each phase invalidates its own content-keyed artifacts (catastrophic branch) | a derived `force_wipe` flag; six phases wipe unconditionally when they see it; **probe ignores it**; the flag is in-memory only — a crash after the `job.yaml` rewrite loses it, and the next run sees a matching identity with no wipe | GAP J-1 (§39) |
| `--force`, source unchanged | no effect by itself; grants permission for other phases' destructive invalidations | no effect — and since `force_wipe` is set only on a source mismatch, the permission never reaches the probe-mismatch recovery the error messages promise | GAP J-2 |
| `job.yaml` missing | rebuild: fresh identity probe + rewrite | rebuild + rewrite | ✓ |
| cleanup / no-metrics | run parameters, no invalidation | stored on the result, drive behavior only | ✓ |

Refs: mismatch branches `job.py:190-214`; rewrite `:238-241`; `force_wipe` consumers `extraction.py:520, chunking.py:253, optimization.py:376, encoding.py:2167, audio.py:162, merge.py:266, metrics.py:545`; broken promises `optimization.py:385`, `encoding.py:2188`.

## Extraction

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| own persisted content identity ≠ live content identity | catastrophic: without permission, loud fatal; with permission, wipe `extracted/` + sidecar and re-extract | the condition is caught authoritatively only at **Job** (its comparison gates the whole run; with `--force`, the propagated wipe removes `extracted/` too). Extraction's OWN comparison is optimistic when reached (re-enumerate + name-trust old files), compares PATH (not content identity), and is nearly unreachable — Job fatals first without force, and the force path deletes the sidecar before the comparison runs. Every other phase has no key at all | GAP X-2 (own-key detection + catastrophic semantics land per the ruling) |
| include/exclude filter change | `wanted` re-derivation only | wanted recomputed; file states untouched | ✓ |
| intent mode (`video_required` / `materialize`) | `wanted` + expected component set only | row construction branches on both flags | ✓ |
| `extraction.yaml` missing, `extracted/` populated | conservative: wipe + re-extract (source currency unknown) | optimistic: re-enumerate; files matching the new names stay COMPLETE | GAP X-1 |

Refs: recovery `extraction.py:490-617`; sidecar load/persist `:691-751`; force wipe `:520-524`.

## Probe

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| `--crop` equals the committed crop | no-op — the workdir is already committed to this crop | unconditional invalidate + rewrite (cheap, frame count kept); the run reports work that wasn't needed | GAP P-2 |
| `--crop` differs from the committed crop | probe re-resolves its own artifact cheaply (crop = override, frame count kept), no permission needed at probe; the INVESTMENT consequence is downstream via the probe-facet key (`ProbeState.crop` on optimization/encoding/merge) — fatal without permission, wipe with it | probe re-resolves + rewrites cheaply ✓; downstream `ProbeState` key fires fatal ✓ — but the permission half is broken (J-2) | ✓ (shape; J-2 dependency) |
| crop provenance (manual vs detected) | human-facing field on `probe.yaml`; downstream facet comparisons use actual values only (frame count + crop) — the field never participates | not persisted today; and the current whole-model `ProbeState` equality (opt/enc/merge) WOULD pick the field up if added naively | APPROVED (with explicit facet-key comparisons as the carrier requirement) |
| own persisted content identity ≠ live content identity | catastrophic: fatal without permission; wipe + re-probe with it | no key at all — cannot detect; the old source's facet loads as current (today's `force_wipe` also bypasses probe entirely) | GAP P-1 |
| `probe.yaml` missing | re-probe | re-probe (pending) | ✓ |
| no video stream in source | fatal for the video chain | fatal (`RecoveryError`) | ✓ |

Refs: `probe.py:154-215` (recovery; the unconditional override branch `:194-206`), `:217-290` (execute; manual-crop + cached-frames `:247-252`), chunking crop-blindness `chunking.py:92-95`.

## Chunking

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| `scene_threshold` / `min_scene_length` change | re-detect, automatic (user-initiated config edit — targets-change precedent). Chunking owns no artifacts: the sidecar is overwritten in place — surplus is impossible at this phase; stale-by-consumption leftovers appear DOWNSTREAM (old-chunk-id winners at encoding) | detection params persisted nowhere; boundaries always reused | GAP C-1 |
| own persisted content identity ≠ live content identity | catastrophic: fatal without permission; wipe sidecar + re-detect with it | no key — boundaries of the OLD source reuse silently; today only the command-wipe path removes them | GAP C-2 |
| stream facets (duration/frames) change | derivation input, not a key — windows re-derive live; the identity key subsumes any stream comparison. Persistence unchanged: the source frame count stays a persisted, replay-expensive fact (fast-exit + facet key + preservation invariant) | live derivation at load | ✓ |
| `chunking.yaml` missing | re-detect | re-detect (pending; detection is deterministic) | ✓ |
| `--force` alone | nothing — permission only gates a fired condition | command-wipes `chunking.yaml` whenever the flag is set, even with nothing to invalidate | GAP (mechanism — subsumed by the permission ruling) |

Refs: `chunking.py:231-273`, sidecar model `stream_model.py:979-989`.

## Optimization

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| tolerance change | live re-selection, no invalidation | selection computed live at read, fast exit included (§99) | ✓ |
| probe facet change | catastrophic, owned HERE for the whole shared namespace (attempts + winners + both yamls; encoding adds nothing — its own fatal retires; the all-strategies skip path carries the facet key too) | fatal; the force branch wipes attempts + yaml — but `--force` never reaches it without a source mismatch; encoding separately duplicates the fatal | GAP J-2 (+ duplication retires with the ownership ruling) |
| search: targets change | wipe winners, replay from attempts | wipes `encoded/` + clears the cached table; replay via attempt cache-hits | ✓ |
| sampling change | re-measure | same wipe branch; re-measure happens at attempt pick-up | ✓ (fixed+cleanup IS loud-stopped by the guard, `optimization.py:253-263`; search+cleanup replay degradation is the accepted cleanup trade-off, not a missing guard) |
| fixed: q change | wipe winners, re-encode at the new q | unconditional winner wipe on every fixed run | ✓ (over-broad — see next row) |
| fixed: q unchanged | no invalidation — the pending gate decides: fast exit only when every wanted pair is COMPLETE, else resume processing (live re-selection on both paths) | wipes anyway; replays every pair through the search machinery | GAP O-2 |
| mode switch search→fixed | wipe winners | unconditional fixed-entry wipe | ✓ |
| mode switch fixed→search | wipe winners | nothing; fixed-q winners classify COMPLETE and the search never runs for them | GAP O-1 |
| strategy args change (codec config) | **catastrophic, per-strategy**: fatal without permission; with it, wipe that strategy's attempts + winners (key = per-strategy resolved-args fingerprint from the frozen plan) | not tracked — no strategy snapshot key | GAP O-5 (§68) |
| test-chunk set vs chunking output | reuse the persisted selection IFF the FULL set survives in the current chunking output (set-presence check); partial survival → fresh full pick (logged) — never silently shrink the test basis | intersection semantics: partial survival silently tests the surviving subset; only an empty intersection re-picks + warns | GAP O-6 |
| `optimization.yaml` missing, winners present | conservative wipe + replay | wipes `encoded/`, winners re-derive from the attempt workspace | ✓ (§99 landed) |

Refs: `optimization.py:194-228` (skip/fixed entry), `:230-271` (unconditional wipe), `:330-495` (recovery incl. §99 branch `:419-431`), `:388-417` (targets/sampling), `:497-514` (test chunks).

## Encoding

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| targets / sampling change | owned upstream at Optimization — one mechanic (sampling key on optimization's sidecar, both variants; effect = wipe winners; attempts re-measure at pick-up); encoding inherits automatically | upstream wipe → pairs ABSENT; pick-up re-measures stale attempts | ✓ |
| probe facet change | owned upstream at Optimization (the full catastrophic branch incl. attempts lives there); encoding has NO facet key and NO fatal of its own | encoding runs its OWN probe-mismatch fatal (`encoding.py:2184-2189`) — duplicated key, duplicated fatal, and the permission is inert (J-2) | GAP (ownership duplication; retires with the ruling — encoding's `_invalidate` becomes empty) |
| attempt metrics sampling stale | re-measure that attempt, no re-encode | pick-up checks the attempt sidecar's `sampling`; re-measures in place | ✓ |
| chunk set change (re-chunk) | attempts KEPT (substrate); stale winners = consumption leftovers → AUTO-DELETED (winner-layer policy — `encoded/` stays merge-ready + human-clean); owned by classification, not invalidation | invisible — orphan detection is per strategy directory only; stale per-name winners stay silently | GAP E-4 (fix sharpened: consume + delete, not just surface) |
| strategy added / removed from selection | added → pending; removed / unselected → winner dirs are leftovers → auto-deleted (attempts survive; merged outputs retain the history) | removed → orphan DIR rows retained in place (`wanted=False`) | GAP (policy change: winner-layer auto-curation replaces retention) |
| winner naming | static per-chunk names, no quality component | promotion keeps the attempt's q-bearing name; six consumer sites parse it back with the attempt regex | GAP E-1 (§101) |
| `encoding.yaml` missing, winners present | no invalidation (certified by optimization's keys); reconstruct aggregates or degrade display | pairs COMPLETE; limiter table silently skipped; frame-total re-assert skipped | ✓ (silent degrade; reconstruction is the enhancement) |

Refs: `encoding.py:2138-2213` (recovery), `:395-483` (pair classification + hand-parsed sidecar stems `:448-455`), `:742-834` (promotion, q-name at `:778`), `:982-1064` (cache-hit/re-measure), `:2215-2238` (fast exit).

## Audio

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| chain definition change | reproduce that chain's outputs | signature compare → exact chain-name delete → reproduce | ✓ |
| chain removed from config | cleanup delete | delete | ✓ |
| `select` tree change | `wanted` re-derivation only | recomputed live every run, never persisted | ✓ |
| `audio.yaml` missing, outputs present | conservative reproduce (currency unknown) | prior signatures empty → nothing invalidated; stale outputs accepted as COMPLETE | GAP A-1 (§9) |
| classification mechanics | one listing, consume names | per-output `.exists()` for every row; the directory is listed only afterwards for surplus | GAP A-2 (§103) |

Refs: `audio.py:122-173` (recovery), `:226-272` (invalidate+commit), `:298-353` (classify, exists at `:334`, listing at `:339`).

## Merge

| Input change | Logical | Code today | Verdict |
| --- | --- | --- | --- |
| search: targets change | recorded targets ≠ current ⇒ **NOT accepted ⇒ re-merge** (a targets change always re-searched winners upstream — the old file is never current); full-metrics retention still lands for the human record + summary replay | deletes per-output sidecars → PARTIAL → **re-measures the STALE concat** (skips re-concat) — measures the wrong file | GAP M-8 (effect amended 2026-10-07: acceptance-flag re-merge) |
| fixed: anchor change | recorded anchor basis ≠ current ⇒ re-merge (same transitivity — anchor change implies the table re-ran) | same re-measure branch | GAP M-8 |
| sampling change | re-measure | same branch | ✓ |
| probe facet change | **per-file**: every output's provenance (which carries the facet) mismatches → re-merge in place; NO phase wipe — deliverables retained until replaced (crash-safe); the only `merged/` wipe is the permission-gated identity wipe | rmtree `merged/` wholesale (merge.py:312-321) — deliverable-layer auto-deletion without permission; a crash after the wipe leaves the user with nothing | GAP M-9 |
| winner set change, keys unchanged (e.g. targets lowered → re-search → NEW crfs under identical static names) | the output must not read COMPLETE — per-output provenance vs live winners ⇒ PARTIAL ⇒ re-merge | nothing; the stale output is reused as COMPLETE | GAP M-2 (§86; fix = per-output typed provenance compare, not a digest) |
| pinned q change, uniform fixed | different output name | q-suffixed name; the old output becomes surplus | ✓ |
| pinned q change, non-uniform fixed (every strategy collapsed, different values — STILL a fixed run via the plan) | per-strategy suffix: `<stem> <strategy> <LABEL>=<q>.mkv` — the suffix is per-strategy by construction, uniformity not required | `_uniform_pinned_quality` gates ALL suffixes off unless every strategy pins the same value → no suffix, same name, stale reuse | GAP M-2b |
| `merge.yaml` missing, outputs present | trivial: the per-output records ARE the keys (merge.yaml = summary aggregate only); summary replay absent until the next processing pass | key comparisons silently skipped; outputs COMPLETE by presence; no summary replay | GAP M-7 (design makes it structural) |
| recovery classification | presence of components + per-output provenance compare vs live winners (per-file reads sanctioned — low-N terminal dir) | presence for the state; content reads load payload facts with NO identity value (pure waste on fast exit) | GAP M-1 (reframed: reads become purposeful by design; today's are waste) |
| fixed run without config targets | measure against the ruler (or honest messaging) | measurement skipped entirely while the fixed banner promises it as the final check | GAP M-6 |

Refs: `merge.py:238-415` (recovery; key branch `:279-321`; content loads `:348-372`), `:858-895` (uniform gate + suffix), `:431-447` (fast-exit replay), `:579` (measurement gate).

## Measure (standalone — outside the registry)

Not a `Phase`; reads `job.yaml`/`probe.yaml` directly (the sanctioned §62 exception — state it explicitly in the spec). Own per-target sidecar, presence-based reuse. No behavior table — out of the recovery-rework mechanics.

---

## Verdict summary

| Phase | ✓ rows | GAP rows |
| --- | --- | --- |
| Job | 3 | J-1, J-2, J-3 |
| Extraction | 2 | X-1, X-2 |
| Probe | 3 (+1 approved row) | P-1, P-2 |
| Chunking | 2 | C-1, C-2 (+ `--force`-alone mechanism, subsumed by the permission ruling) |
| Optimization | 6 | J-2, O-1, O-2, O-5, O-6 (+ content-identity key, per the per-phase-keys ruling) |
| Encoding | 3 | J-2 (retiring with ownership), E-1, E-4, E-8 (duplicated probe fatal — retires), E-9 (orphan winner dirs retained vs winner-layer auto-curation) |
| Audio | 3 | A-1, A-2 |
| Merge | 2 | M-1, M-2, M-2b, M-6, M-7, M-8, M-9 |

26 distinct gaps (J-2 spans three phases; E-8 retires with the ownership ruling). Structural closers already ruled: consumption + static names close E-1/E-4 and half of M-2; per-mode keys close O-1/O-2; permission+keys close J-1/J-2/P-1/X-2/C-2 and the per-phase identity rows; content-identity/locator split closes J-3; unknown-currency rows close X-1/A-1/M-7; shared-namespace ownership + layered retention close E-8/E-9; full-metrics retention + live verdicts close M-8; per-output provenance closes M-2 and M-9. Open mechanics: O-5 fingerprint shape (settled D4), O-6 (set-presence check), M-1/M-6 (mechanical; M-1 reframed — reads become purposeful by design).
