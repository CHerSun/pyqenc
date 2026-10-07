# Recovery rework — spec plan (for the gated `.kiro/specs/` authoring)

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-06
- Status: investigation complete; **spec requirements+design NOT yet authored** — this plan is the input to that (two-stage rule: requirements + design first for human review; tasks only after approval).
- Grounding: `per-phase-audit.md`, `sidecar-models.md`, `naming-and-consumption.md`, `invalidation-matrix.md` (same folder).

## 1. Scope (one motion, per the user queue)

1. **§101 static winner names** — winners statically named per chunk; naming (compose AND parse) owned by entity classes; no pattern-matching for identity anywhere.
2. **§100 artifact-owned recovery** — consumption protocol over one listing (per owned dir); single-item classification on payloads; phase keeps mass ops (listing, rows, aggregation, scheduling); leftovers = surplus; collision = loud.
3. **§11 invalidation audit** — the key-set doctrine (invalidation-matrix.md §4) applied per phase; logical matrix now exists; the spec turns the GAP census into keys.
4. **Fixed↔searched mode switch** — per-mode sidecar unions with their own keys; both switch directions structural.
5. **Winner provenance** — where crf/metrics live when names go static (facts on winner sidecars + replay aggregate on the phase sidecar).

Explicit non-goals: QualitySearch V4 (§51), unified summaries content (2026-10-03 spec), §102 scheduler, cleanup levels, measure-standalone redesign.

## 2. Workstreams (proposed spec structure)

| WS | Content | Grounding |
| --- | --- | --- |
| WS1 Naming | static winner names; winner/result-sidecar name families re-homed to `EncodedChunk`; audio chain-name parse re-home; merged-output family to `MergedVideo` (incl. per-strategy q suffix → closes M-2b) | naming-and-consumption.md §1-2 |
| WS2 Recovery protocol | consumption over listing; payload-owned classification hook; §103 hoist; extraction surplus surfacing; per-name orphan detection at encoding (E-4); merge recovery stops reading sidecar contents (M-1); recovery I/O budget as a requirement | naming-and-consumption.md §3, sidecar-models.md §5 |
| WS3 Sidecar models | per-mode discriminated unions for Optimization/Merge params (tag, not try-both); typed merged-output sidecar (drop `plot`/`anchor`); winner replay aggregate (crf/res map) on `encoding.yaml`; §62 re-home of sidecar models to phase modules; docstring fixes (E-6) | sidecar-models.md §2-4 |
| WS4 Invalidation keys | mode tag (O-1); fixed q map key (O-2); chunking params keys (C-1); audio conservative rule for missing sidecar (A-1/§9); merge winner-identity key (M-2/§86); probe force-wipe (P-1); §39 decision; §68/O-5 decision | invalidation-matrix.md |
| WS5 Uniform run flow | codify: fast exit = ledger + persisted aggregates + LIVE re-selection only; single-post-success save policies (per phase, incl. audio's commit-intent exception — state it); two-write encoding crash-safety pattern stays or simplifies | per-phase-audit.md dispatch sections |

## 3. Requirement candidates (EARS draft — to be refined into the spec's requirements.md)

Naming (WS1):

- Req 1 — The `EncodedChunk` entity shall compose the winner file name as a pure function of chunk identity and output resolution, with no quality component. *(form per Open decision 1)*
- Req 2 — The encoding phase shall promote the winning attempt to the statically-composed winner name at finalization.
- Req 3 — The `EncodedChunk` entity shall compose the winner result-sidecar name from the winner stem; the encoding phase shall not parse file names to locate a winner's sidecar.
- Req 4 — If two artifacts consume the same on-disk name within one listing, the recovery classification shall fail loudly at the collision site.
- Req 5 — The `AudioOutput` entity (or the chain module's name family) shall own both composition and parsing of the `chain=<name>` output name as an inverse pair.
- Req 6 — The `MergedVideo` entity shall compose its output name including the pinned-quality suffix whenever its strategy's effective quality range is a single point.

Recovery protocol (WS2):

- Req 7 — Each phase's recovery shall classify wanted artifacts by consuming payload-owned expected names out of a single directory listing per owned directory, without reading artifact-sidecar contents.
- Req 8 — Names present in a listing after every row consumed shall surface as present-but-unwanted ledger rows (`wanted=False`, retained in place).
- Req 9 — While no wanted artifact is ABSENT or PARTIAL, a phase shall build its result from the recovery ledger and its own params sidecar alone.
- Req 10 — The audio phase shall classify its expected outputs from the shared listing rather than per-row existence checks.
- Req 11 — Recovery of the merge phase shall not read per-output sidecar contents; measured facts shall load on the processing path at their consuming site.

Sidecar models (WS3):

- Req 12 — `optimization.yaml` and `merge.yaml` shall persist as per-mode discriminated-union models tagged `mode: fixed | search`, each variant carrying only its mode's key set.
- Req 13 — The fixed optimization variant shall persist the per-strategy pinned-quality map as its invalidation key.
- Req 14 — The search variants shall persist the quality-target set as their invalidation key; the fixed variants shall omit it.
- Req 15 — `encoding.yaml` shall persist a winner replay aggregate (per strategy: chunk → quality, resolution) rebuilt on every concluded pass and replayed on fully-reused runs.
- Req 16 — Per-mode sidecar loading shall dispatch on the persisted mode tag, not on trial parsing.
- Req 17 — The merged-output sidecar shall persist as a typed model carrying file facts and mode-conditional verdicts, without `plot` or `anchor` entries.

Invalidation (WS4):

- Req 18 — When the plan's mode differs from the persisted mode tag, the optimization phase shall invalidate all winners before classification.
- Req 19 — On a fixed run whose pinned-quality map equals the persisted key, the optimization phase shall fast-exit without wiping winners.
- Req 20 — The probe phase shall delete `probe.yaml` when the run carries `force_wipe`.
- Req 21 — The chunking sidecar shall persist the detection parameters, and a change in them shall invalidate the persisted boundaries.
- Req 22 — If the audio sidecar is absent while chain outputs exist, the audio phase shall treat those outputs as stale and reproduce them.
- Req 23 — A merged output whose consumed winner set differs from the identity recorded for it shall not classify as COMPLETE. *(mechanism per Open decision 5)*
- Req 24 — Recovery-time deletion of the attempt workspace (`encoding/`) shall occur only under `--force`. *(codify; §68 extension per Open decision 4)*

Source identity (Open decision 10 — approved):

- Req 25 — The Job phase shall compute the source identity at every run as the path, the file size, and a sampled content digest (head, tail, and two interior windows of approximately one megabyte each).
- Req 26 — If either side of a source-identity comparison lacks the sampled digest, the comparison shall treat the digest as unknown rather than as a mismatch.
- Req 27 — The spec's documentation of the source-identity key shall state that the digest guards against accidental replacement and in-window corruption only, not isolated mid-file bit rot or adversarial tampering.
- Req 28a — The Job sidecar shall persist the source path alongside the content identity (the human-facing record of what the workdir works with).
- Req 28b — Each phase sidecar shall persist the content identity (size + sampled digest) without the path; the path is a runtime locator carried by the live `File`.

Crop provenance (approved 2026-10-06):

- Req 32 — The probe sidecar shall record the crop's provenance (manual or detected) for human inspection.
- Req 33 — Downstream probe-facet comparisons shall compare the frame count and the crop values only; the provenance field shall never participate in any invalidation comparison.

Shared-namespace ownership + layered retention (ruled 2026-10-07):

- Req 34 — The optimization phase shall own every key-based invalidation over the shared attempt/winner namespace (attempts, winners, both phases' keys); the encoding phase shall perform none — its invalidation step is empty and its recovery is classification-only.
- Req 35 — The all-strategies skip path shall persist the facet and content-identity keys exactly as the optimize paths do.
- Req 36 — Encoding recovery shall auto-delete consumption leftovers in `encoded/` (winner-layer curation: the dir stays merge-ready and human-clean); attempts (investment layer) and merged/materialized artifacts (deliverable layer) shall never be auto-deleted — deletion only via permission or explicit cleanup (layered retention policy).

Merge verdicts + identity (ruled 2026-10-07):

- Req 37 — The merged-output sidecar shall persist the full measured metric set in both modes, plus the frame count, the measurement sampling basis, the winner-ID list, and the fixed-run quality facts.
- Req 38 — Merge verdicts shall be computed live from the persisted metrics against the current targets or ruler; the merged-output sidecar shall persist no verdict.
- Req 39 — When any recorded provenance key mismatches the current inputs (targets or anchor included), the merge phase shall re-merge the output from the current winners and measure the new file; it shall never re-measure a stale concat (a targets/anchor change always re-searched winners upstream — the old file is never current).
- Req 40 — Each merged-output sidecar shall persist its production provenance (strategy, the strategy's resolved-args fingerprint, mode and pinned q where fixed, targets/anchor basis, probe facet, sampling, and the winner-set identity as a count plus a hash over the sorted winner ids — compressed, no list, no crfs); merge recovery shall classify an output COMPLETE only when its provenance matches the current inputs. A missing or unverifiable winner-set hash shall be treated as a mismatch (re-merge).
- Req 41 — Recovery-sidecar reads are permitted only at O(strategies) scale (per-output records at merge); recovery of high-N directories (`encoding/`, `encoded/`) shall remain listing-only.

Unknown currency — missing own params sidecar (raised in logical validation, 2026-10-06; rule = invalidation-matrix.md §2):

- Req 28 — If a phase's own parameter sidecar is absent while its artifacts are present, the phase shall classify those artifacts' parameter currency as unknown rather than current.
- Req 29 — Where the artifacts re-derive from a deeper substrate (optimization winners from attempts, audio outputs from chain re-runs, extracted files from the source), the unknown-currency treatment shall be automatic conservative re-derivation or reproduction.
- Req 30 — Where the artifacts are expensive and not replayable (merged outputs), each artifact's own durable record shall carry the identity needed to reconstruct the phase keys, and a missing phase sidecar shall trigger reconstruction rather than re-measurement.
- Req 31 — The encoding phase shall carry no unique invalidation key: winner currency is certified by the optimization keys, and `encoding.yaml` holds replay aggregates only.

## 4. Open design decisions (settle BEFORE authoring requirements.md)

| # | Question | Options & lean |
| --- | --- | --- |
| 1 | Winner static name form | **SETTLED 2026-10-07 (user, final): `<chunk>.mkv`** — pure input-derived names restored. The res-drop and the reopened q-in-names discussion both closed: no aggregate, no parse index, no epoch — crf/res are FACTS on the winner sidecar, consumed on processing paths only. Winner sidecar = `<chunk>.yaml` (pure stem swap); attempt names in `encoding/` unchanged. |
| 2 | Winner crf at fast exit | **DISSOLVED 2026-10-07 (user, amended same day): crf is not carried at all.** Zero consumers of a payload-level crf remain (recovery or fresh): the CRF graph reads winner sidecars on the merge re-merge path only; the limiter scan reads sidecars it already opens; merge provenance uses the winner-set hash + params, never crfs; the fixed-merge q suffix derives from the plan, not artifact q. `EncodedChunk` DROPS the crf field entirely (user: no Optional ambiguity — a field whose presence affects nothing downstream is dead weight); the quality value lives on the winner sidecar only. |
| 3 | §39 forced-wipe idempotency | **CLOSED 2026-10-07 (user): dissolved by the permission model.** Phase-local content-identity keys re-detect a stale-source workdir on every run, so the in-memory `force_wipe` propagation (and its crash hole) no longer exists. Nothing to build; TODO §39 consumed. |
| 4 | §68 strategy-args invalidation | **SETTLED 2026-10-07 (user, refined): catastrophic, per-strategy; the fingerprint is a CLASS-OWNED comparator.** Fatal without permission; with it, wipe that strategy's attempts + winners. `Strategy` owns its identity projection — a `fingerprint` accessor (joining the name family: display/safe/fingerprint) that internally canonicalizes `model_dump()` minus a declared exclusion set (`codec.default_quality` — search start point, not product identity), with the exclusion set declared ON the model next to what it excludes. Consumers are dumb: persist `strategy.fingerprint` / compare `persisted == strategy.fingerprint` — opaque, no model-shape knowledge at any comparison site. Token form (design detail, lean): the canonical string itself (sorted keys, stable), human-diffable in sidecars; hashing optional if size ever matters. **Pydantic mechanics verified on 2.13.5:** `Field(compare=False)` = no-op on BaseModel equality (v1 leftover, removed in v3); `Field(exclude=True)` = strips from ALL serialization, breaks required-field round-trips. Because the token omits a required field it is OPAQUE — never re-validated into a `Strategy`. Exclusion set fails SAFE (future fields participate by default). The fingerprint rides each merged output's provenance. Config-coverage inventory: invalidation-matrix.md §6. |
| 7 | Production ownership boundary | **APPROVED 2026-10-07 (user): production is a question of the PHASE — especially for mass production (mass-extraction, parallel encode) — not of the artifact.** Entities own classification + naming; item machinery (`ChunkEncoder`, chain executor, extractors) does the work, driven by the phase; payloads stay frozen data. |
| 5 | §86 winner identity at merge | **SETTLED 2026-10-07 (user design, supersedes the digest):** per-output **typed provenance** on each merged-video sidecar — strategy, mode (+pinned q), the winner list `(chunk_id, crf)` in timeline order (direct values, human-readable, NO hash), sampling basis; recovery compares each output's provenance against the live winners (few outputs ⇒ per-file reads sanctioned). `merge.yaml` demotes to summary-replay aggregate (no identity map — second-hand records drift; single ownership). Kills the digest entirely. |
| 6 | Merged sidecar metrics breadth | **APPROVED 2026-10-07 (user):** FULL measured set in both modes, and the merged-output sidecar carries **no verdict at all** — verdicts are live at merge; targets/anchor mismatches ⇒ re-merge (M-8 amended), never re-measure (Req 37–39) |
| 8 | Chunking params keys | **APPROVED 2026-10-07 (user): IN this spec** — "part of logical invalidation and a definitive gap." `scene_threshold` + `min_scene_length` persist on `chunking.yaml` as invalidation keys; change ⇒ automatic re-detect (closes C-1). §47 (threshold-search feature) stays separate. |
| 9 | Spec name & sequencing | **SETTLED 2026-10-07 (user): folder `2026-10-07 artifact-recovery-ownership` (agent's call); the grounding docs moved INTO the spec folder as its basis. This spec lands FIRST (nothing required before it); §93's `_deps` typed accessor is INCLUDED here (agent's call — same files). Unified-summaries re-forks from this spec's merge afterwards.** |
| 10 | Source-identity key strengthening | **APPROVED 2026-10-06 (user):** add a **sampled content digest** (head + tail + 2 interior windows, a few MB total, stdlib blake2b-128) to the source-identity key, computed once per run at Job and compared everywhere the key is compared — later phases validate by "Job's live source ≠ this phase's preserved source (hash included)". Rejected alternatives: full-content hash (a full read of a multi-GB remux on every fast exit — the only source I/O left on reused runs); mtime (false positives — any metadata touch — trigger the CATASTROPHIC branch and demand `--force` wipes; for a catastrophic key, false positives are worse than false negatives). Missing digest on either side = unknown, not mismatch (pre-alpha workdirs rewrite on first run). Threat model stated explicitly in the spec: guards against ACCIDENT (wholesale same-size replacement — re-mux/re-download; in-window corruption); NOT isolated mid-file bit rot (coverage too thin to promise) and NOT adversarial tampering (fixed windows are trivially preserved by an attacker). |
| 11 | Path's role in source identity | **APPROVED 2026-10-06 (user, with refinement):** identity = **content** (size + sampled digest); path = **runtime locator**. A path change with matching content merely rewrites `job.yaml` — no fatal, no force, no invalidation; phase keys compare content identity only. **Refinement:** `job.yaml` KEEPS the path (the one human-facing sidecar — "what this workdir works with" + the standalone measure command's source discovery); phases persist a separate **content-identity entity only** (size + digest, no path) — minimal sidecars, human-value exceptions reserved for end-user-facing files. Live `File` objects may carry the digest for zero-plumbing flow; the persisted key is the minimal projection. Today's behavior (moved source → fatal → `--force` → full wipe) becomes GAP J-3. |
| 12 | Invalidation as a standalone mechanic (raised 2026-10-07) | **APPROVED 2026-10-07 (user): proceed with the formal `_invalidate()` / `_recover()` split — WITH a mandatory extra assessment step in the spec (a verification task, run once everything else is finalized): confirm the separation is clean, including a clear conceptual distinction of invalidation state vs recovery completeness state (the parameter-currency axis vs the presence axis — as crisply separated as ABSENT/PARTIAL/COMPLETE are).** Target shape: a template-sequenced `_invalidate()` step before classification-only `_recover()` — influence on outcomes preserved by ordering (invalidate → recover → gate → execute), not co-location. Load-bearing invariant: **invalidation effects are always DISK effects** — classification re-reads disk truth, no in-memory coupling between the hooks. Owners: sidecar variants own keys + `matches()`; phase owns condition→effect policy + permission; payloads + listing own classification; template owns sequencing. Known consequences: (a) optimization's in-memory table-clearing becomes a sidecar rewrite (disk effect); (b) the fixed+cleanup guard moves from `_skip_check` to plan-boundary construction validation, banner stays presentation. NOT: a separate phase/pass, post-recovery sequencing, or shared mutable state. Post-review inventory: exactly one in-memory case (optimization's table-clear), which converts to a disk write. |
| 13 | Fixed runs without config targets — merged measurement vs banner promise (M-6) | **SETTLED 2026-10-07 (user): separate MEASUREMENT from DECISION — measurement never needs a bar.** A fixed run is direct-quality controlled: no targets, no measuring target; the anchor is synthetic, never persisted, fully reselectable at runtime on both paths, and NOT a measurement trigger — presentation basis (deltas) only. Merged outputs are measured **unconditionally** (measurement needs file + reference + sampling, nothing else); the `if plan.targets:` gate dies. Verdicts are decisions computed live on measured facts (search: vs config targets; fixed: no verdict, anchor-relative presentation). Machinery consequence: measurement and verdict computation become separate steps (today `QualityEvaluator.evaluate_chunk(targets=...) -> targets_met` is the mixed concept; the search loop's verdict consumption stays — it is a DECISION made after measurement, not inside it). M-6 closes at the root; the banner's "final check" stays honest because measurement always runs. |

## 5. Sequencing and related TODO items

| TODO | Disposition under this spec |
| --- | --- |
| §100, §101 | CONSUMED (core) |
| §11 | CONSUMED (matrix rebuilt; remaining = per-phase key audits finalized in requirements) |
| §9 | CONSUMED (audio conservative rule, Req 22) |
| §86 | CONSUMED for winner identity (Req 23) + non-uniform fixed naming (Req 6); summary-trustworthiness follows from gate |
| §105 | CONSUMED (mode-tagged unions + fixed q key) |
| §103 | CONSUMED (Req 10) |
| §62 | CONSUMED (sidecar model re-home; measure-standalone exception stated) |
| §90 | SPLIT: the per-mode typing lands HERE (Req 12-14, 17); the summary-content narrowing (metrics breadth, `strategy_results.metrics`) stays with unified-quality-summaries — cross-spec note required in both |
| §92 | PARTIAL: `_scan_winner_sidecars` load-one/aggregate split falls out of WS1/WS2; the general long-function inventory stays §92 |
| §68 | DECISION REQUIRED (Open 4); wiring can land here or defer |
| §39 | DECISION REQUIRED (Open 3) |
| §102, §51, §83 | untouched (adjacent only) |
| §106 (encoding⇄optimization shared mechanics) | adjacent: the shared pair-ledger machinery re-homes under WS1/WS2; the import-cycle break itself stays §106 |
| §33/§50 (space estimate), §96 (merge UX) | untouched |

Memory-context sequencing note: unified-summaries is parked pending user approval; §93 (`_deps` view) was queued for THAT window's start, same files as this one — recommend §93 lands with this spec's window instead (or first), so both later windows build on the final access pattern.
