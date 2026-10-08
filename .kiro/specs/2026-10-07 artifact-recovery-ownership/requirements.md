# Artifact Recovery Ownership — Requirements

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-07
- Completed: 2026-10-08

Basis: the grounding documents in this folder (`logical-vs-code.md`, `per-phase-audit.md`, `sidecar-models.md`, `naming-and-consumption.md`, `invalidation-matrix.md`, `spec-plan.md` — every requirement below traces to a ruling recorded there during the 2026-10-06/07 validation sessions).

## Cross-Spec Notes

### What this spec supersedes/changes in prior specs

| Changed | Where | What changed |
|---|---|---|
| Attempt file name `<chunk>.<resolution>.q<crf>.mkv` (Req 15.5) and the post-encode resolution probe/rename (Req 14.2) | `2026-09-25 file-stream-model` | Attempts become `<chunk>.q<q>.mkv` (quality is the search's cache key; no resolution component — Req 9/9b here); `EncodedChunk` drops the `crf` payload field entirely (Req 4 here); the winner family is the static `<chunk>.mkv` + `<chunk>.yaml` composed by `EncodedChunk` (Req 1–2 here). |
| `probe.yaml` shape (Req 3.5 there) | `2026-09-25 file-stream-model` | Gains `crop_source: manual\|detected` (human-facing, never compared — Req 23 here) and the source-fingerprint identity key (Req 31 here). |
| `chunking.yaml` = boundaries only (Req 4.4 there) | `2026-09-25 file-stream-model` | Gains `scene_threshold` + `min_scene_length` as invalidation keys (Req 26 here). |
| Merge output name derived at one phase site (Req 15.8 there) | `2026-09-25 file-stream-model` | The family re-homes to the `MergedVideo` entity, including the per-strategy pinned-q suffix whenever the strategy's range is collapsed (Req 6 here; uniformity gate dropped). |
| Winner-layer wholesale invalidation at every fixed-mode start (Req 6 there) | `2026-10-02 fixed-quality` | Re-keyed: the fixed sidecar variant persists the pinned-quality map; an equal map performs no invalidation — the pending gate alone decides (O-2); mode switches re-key via the union tag (O-1). |
| Cleanup guard as a skip-time hard stop (Req 7 there) | `2026-10-02 fixed-quality` | Moves to plan-boundary construction validation (Req 55 here); the banner stays presentation. |
| Recovery classification by membership/regex indexes and per-row `.exists()` | `2026-09-28 artifact-model` (ledger mechanics, retained) + code drift | Becomes the consumption protocol: payload-owned expected names consumed out of ONE listing per owned directory; leftovers → layer policy; collisions loud (Req 12–15 here). Ledger completeness states, `wanted` derivation, and result contracts are unchanged. |
| `--force` → derived `force_wipe` propagated wipe orders | `2026-09-28 artifact-model` lineage / job.py code | `force_wipe` is deleted; `--force` becomes a permission riding the job result; every phase detects its own conditions from its own persisted keys (Req 33/35a here). |
| Mode-honest optional key fields on `optimization.yaml`/`merge.yaml` | `2026-10-02 fixed-quality` implementation | Per-mode discriminated-union models tagged `mode: fixed\|search` (Req 18 here). TODO §90's typing half lands here; the summary-content narrowing stays with `2026-10-03 unified-quality-summaries`. |
| Merged-output sidecar as an untyped dict with persisted verdicts | code (merge.py) | Typed facts+provenance model, no verdict (Req 27 here); verdicts computed live (Req 51); measurement unconditional (Req 52). |
| §99 conservative wipe on a missing optimization sidecar | optimization-live-selection fix (0.17.3, d552743) | Retained verbatim as optimization's unknown-currency rule (Req 47 here) and generalized per phase (extraction/audio wipe + re-derive; merge per-output records decide; encoding cross-certified). |

### Related, not superseded

- `2026-10-05 cli-intent-commands` — intent command set, closure-derived registry, and `extract` materialization are untouched; `extracted/` materialized containers join the deliverable layer (retained) without changing that spec's requirements.
- `2026-10-03 unified-quality-summaries` — builds on this spec's substrate (per-mode sidecars, `summary` aggregate key, fingerprints); its table/content work re-forks from this spec's merge.
- `2026-05-02 quality-search-v3` — search behavior is untouched; only attempt naming (`<chunk>.q<q>.mkv`, no resolution) and pick-up verification (completeness-by-sidecar) change underneath it.

## Purpose

Rebuild recovery, invalidation, and naming on one ownership model: **the filesystem is the state** (no mirrors), entities own their names and classification, phases own mass operations and condition→effect policy, the template owns sequencing. Closes TODO §100, §101, §11, §9, §39, §62, §68, §86, §103, §105 and the mode-switch gap; parts of §90/§92/§106 ride along.

## Non-goals

QualitySearch V4 (§51), unified summaries content (2026-10-03 spec — builds on this one), §102 scheduling, cleanup levels, chunk-threshold search (§47), standalone measure redesign.

## 1. Naming ownership

- Req 1 — The `EncodedChunk` entity shall compose the winner file name as a pure function of the chunk identity only (`<chunk_id>.mkv`), with no quality and no resolution component, and the winner result-sidecar name as the winner stem with a `.yaml` suffix.
- Req 2 — The encoding phase shall promote the winning attempt to the statically composed winner name at finalization, and no consumer shall locate a winner's sidecar by parsing file names.
- Req 3 — Winner `crf` and `resolution` shall be facts on the winner result sidecar; recovery-time classification and composition shall not read them (processing-path consumers — the re-merge CRF graph and the winner scan — may).
- Req 4 — The `EncodedChunk` payload shall not carry the quality value as a field; the winning quality is a fact of the winner sidecar only, and consumers that need it shall read it on their processing path.
- Req 5 — If two artifacts consume the same on-disk name within one listing, recovery shall fail loudly at the collision site.
- Req 6 — The `MergedVideo` entity shall compose its own output name, including the pinned-quality suffix `<LABEL>=<quantized q>` whenever its strategy's effective quality range is a single point, regardless of value uniformity across strategies.
- Req 7 — Search-mode merged outputs shall carry no quality or generation distinguisher in their file names; a name collision on an expected output shall trigger revalidation, never silent acceptance.
- Req 8 — One owner shall hold both composition and parsing of the audio chain-output name (`<stream safe name> chain=<name>.<ext>`) as a strict inverse pair.
- Req 9 — Attempt file names in `encoding/` shall be `<chunk_id>.q<quality>.mkv` (quality is the search's cache key; no resolution component), with the sidecar as the stem swap; `EncodedChunk` shall remain the sole owner of their composition, and attempt lookup shall be an exact-name compose with no globbing or name parsing.
- Req 9a — Attempt verification shall be a completeness check of a cache entry at execution time, not an invalidation: attempts take no part in recovery classification (the pair ledger classifies winners only) and no per-attempt invalidation pass exists; when the search proposes a quality for a pending pair, pick-up shall verify the addressed attempt complete by its sidecar (resolution, metrics, sampling basis, frame count present and current) and otherwise re-measure or re-encode. Phase-level conditions may delete attempts wholesale as effects (facet/fingerprint wipes) — never by inspecting them.
- Req 9b — The attempt sidecar shall gain `resolution` as a fact, and the post-encode resolution-correction rename shall not exist (the attempt's final name is fully known before encoding begins).
- Req 10 — A **fingerprint** shall be the standard identity mechanism: one value type `{size: int|None = None, token: str}` where `token` (REQUIRED — derivations are total; an unreadable source is a fatal at the computing site, never a carried `None`) is an opaque hash of the thing's canonical form, and `size` is an optional belt — a cheap pre-check magnitude (bytes for files, cardinality for sets) that participates when present on both sides (differing sizes = mismatch: the collision enforcement) and contributes nothing when absent (design absence for owners with no meaningful check; fast-fail before token comparison when present). The token alone is authoritative for identity. **Data (the fingerprint) is separate from means (the derivation)**: each owning entity declares its derivation and exposes `.fingerprint`; consumers persist and compare with no knowledge of either. `Fingerprint(None, None)` is unconstructible; unknown-not-mismatch applies to missing sidecar fields (Req 32), never to Nones inside the type; mismatch reports name the differing field and owner, not a diff.
- Req 10a — The `Strategy` entity shall expose a `fingerprint` derived as a hash of its canonical dump minus the declared exclusion set (`codec.default_quality`); the exclusion set is declared on the model.
- Req 10b — The `ResolvedChain` entity shall expose a `fingerprint` (hash of its canonical form — today's chain signature, now opaque); the audio sidecar and comparisons shall use it with equality only.
- Req 10c — Fingerprints apply where the identity is **opaque** (file content, resolved config objects, sets of ids); structured field-wise keys remain where fields are individually meaningful and diffable (the probe facet, quality targets, the pinned-quality map, sampling). The source identity (former `ContentIdentity`) IS a fingerprint (derivation: sampled windows over the file; `size` = file size); the winner-set identity IS a fingerprint (derivation: sorted chunk ids; `size` = count).
- Req 10d — Fingerprint tokens shall not be treated as user-facing data: they are idempotency markers; human-readable payloads shall not be persisted for them.
- Req 11 — The fingerprint token shall never be re-validated into the model it summarizes (a fingerprint may omit required fields by construction).
- Req 11a — The merged output's winner-set identity (count + hash over sorted winner ids) is the **winner-set fingerprint**; it is compared as a whole, and an unverifiable one is a mismatch.

## 2. Recovery protocol (consumption)

- Req 12 — Each artifact-directory-owning phase shall classify its wanted artifacts by consuming payload-owned expected names out of a single directory listing per owned directory, without reading artifact-sidecar contents, except where Req 14 applies.
- Req 13 — Names present in a listing after every row has consumed shall be handled by the layer policy of the owning directory: deleted automatically in the winner layer (`encoded/`), retained and surfaced in the deliverable layer (`merged/`, materialized `extracted/`).
- Req 14 — Recovery of high-N directories (`encoding/`, `encoded/`) shall remain listing-only; per-file sidecar reads at recovery are permitted only at O(strategies) scale (the merged outputs).
- Req 15 — The audio phase shall take its single directory listing once, before classification, and classify every expected (track, chain) output by membership of its expected name in that listing; the same listing shall feed the surplus scan. Per-row existence checks (`.exists()` per output) shall not remain (TODO §103).
- Req 16 — On a chunk-set change (re-chunk), the optimization phase shall wipe winners via its chunk-set fingerprint key (an invalidation-domain effect at `_invalidate`, ahead of recovery — Req 19); the encoding phase shall keep attempts in `encoding/` (attempts are never classified and never individually invalidated — only completeness-verified at pick-up) and its consumption shall curate only residual leftovers (orphan strategy dirs, foreign names) — never the chunk-set change itself.
- Req 17 — When strategies are removed from the plan or deselected, encoding recovery shall delete their winner directories as leftovers while their attempts remain in place.

## 3. Sidecar models and typing

- Req 18 — `optimization.yaml` shall persist as per-mode discriminated-union models tagged `mode: fixed | search`, loading dispatched on the tag. `merge.yaml` carries no union — its shape is Req 28's (summary-replay aggregate + basis marker), and the mode comparison lives per-output in the provenance record (Req 27). *(Scope clarified 2026-10-09: the union mention of `merge.yaml` here was superseded within this spec by the Req 27/28 merge rulings.)*
- Req 19 — The optimization search variant shall carry `quality_targets` as its invalidation key; the fixed variant shall carry the per-strategy pinned-quality map as its key and shall omit `quality_targets`; both variants shall carry, as common keys on the shared base, the per-strategy fingerprint map (config-args detection — mode-independent, and present even when the strategy table is empty), the **chunk-set fingerprint** (the current chunk id set — same derivation as merge's winner-set: hash over sorted ids, `size` = count; mismatch ⇒ wipe winners at invalidation, uniform with merge's judgment and ahead of any recovery/search), the metrics-sampling factor, the probe facet, and the content identity.
- Req 20 — The all-strategies skip path shall persist all invalidation keys (mode, targets-or-pinned-q map, fingerprint map, sampling, facet, content identity) exactly as the optimize paths do.
- Req 21 — Every phase sidecar model shall live in its owning phase's module; shared primitives only in `state.py`/`stream_model.py`.
- Req 22 — Sidecars shall persist the minimum key set needed for their comparisons; human-value fields are reserved for end-user-facing files (`job.yaml`, merged per-output sidecars).
- Req 23 — The probe sidecar shall record the crop's provenance (manual or detected) as a human-facing field, and downstream probe-facet comparisons shall compare the frame count and crop values only, never the provenance or any whole-model equality.
- Req 24 — `encoding.yaml` shall hold replay aggregates (winning-limiter table, winners frame totals) and no invalidation key; winner currency is certified by `optimization.yaml`.
- Req 25 — The winner result sidecar shall drop the `winning_attempt` field and gain `resolution` alongside `crf`, frame count, and the concluding `targets_met` verdict; its `metrics` shall carry the **targeted subset only** — full measured sets live on attempt sidecars (the re-judging substrate: cache-hits re-judge under new targets without re-encoding) and on merged-output sidecars (the re-judgeable end-user record); phase-sidecar summaries carry targeted metrics as well.
- Req 61 — Sidecar fields shall be named for what they identify, never for their mechanism (`chunks`, `strategies`, `targets`, `source`, `winners` — the typed models know these are fingerprints), and every phase sidecar's replay aggregate shall use the single key `summary` (its shape typed per sidecar class).
- Req 26 — The chunking sidecar shall persist `scene_threshold` and `min_scene_length` as invalidation keys beside the boundaries.
- Req 27 — The merged-output sidecar shall persist as a typed model carrying facts (frame count, the full measured metric set in both modes) and provenance (the content identity, strategy, its fingerprint, mode and pinned q where fixed, targets/anchor basis, probe facet, sampling, and the winner-set identity as a count plus a hash over the sorted winner ids) — and no verdict. The content identity is the one provenance dimension that escalates: any output's identity mismatch is the phase-level catastrophic condition (fatal / permission-gated full `merged/` wipe — Req 60), not a per-file re-merge; an empty `merged/` carries no identity key and nothing to invalidate.
- Req 28 — `merge.yaml` shall hold the summary-replay aggregate and its basis marker only; the per-output sidecars are the acceptance records.
- Req 29 — The encoding winner replay aggregate question is dissolved: no per-winner aggregate shall be persisted; `encoding.yaml` stays aggregate-only as today.

## 4. Source identity

- Req 30 — The Job phase shall compute the source fingerprint at every run (`size` = file size; `token` = blake2b-128 over head, tail, and two interior windows of approximately one megabyte each), once per run, carried by the job's live `File`.
- Req 31 — `job.yaml` shall persist the path alongside the source fingerprint; every other phase sidecar shall persist the source fingerprint (the `{size, token}` pair, no path) as its identity key.
- Req 32 — If either side of a key comparison lacks a persisted value (an absent fingerprint block or key field on a sidecar), the comparison shall treat that key as unknown, never as a mismatch; within a fingerprint, absence never occurs in the type (Req 10) — the token is authoritative and the optional size participates only as a belt.
- Req 33 — A content-identity mismatch shall be catastrophic for every phase: fatal without `--force`; with it, the phase wipes its own artifacts and re-derives.
- Req 34 — A path change with matching content identity shall rewrite `job.yaml` as a locator update, with no fatal, no force, and no invalidation.
- Req 35 — The spec documentation shall state that the digest guards against accidental wholesale replacement and in-window corruption only — not isolated mid-file bit rot and not adversarial tampering.

## 5. Invalidation: permission, conditions, effects

- Req 35a — The `--force` CLI flag shall grant permission for destructive invalidation effects for the run and shall have no other effect; the derived `force_wipe` result field shall be deleted, and no phase shall perform a wipe because the flag is set.
- Req 36 — Permission shall gate only the deletion of investments — the `encoding/` attempt workspace, and each phase's own artifacts under a source-identity mismatch (Req 33's catastrophic band: fatal without permission, wipe-own with it). Winner wipes, re-derivation, re-measurement, and reproduction shall be automatic once past that gate, and unknown-currency re-derivations (Req 47) shall never need permission. *(Wording clarified 2026-10-09: "whole-workdir source changes" reads ambiguous against Req 33; the band is Req 33's.)*
- Req 37 — Each phase's conditions shall be detected by comparing its own persisted keys against current inputs; effects shall be drawn from the fixed vocabulary (fatal / wipe-own / conservative re-derive / reconstruct / rewrite-own-sidecar), and every effect shall be a disk effect.
- Req 38 — The optimization phase shall own every key-based invalidation over the shared attempt/winner namespace (probe facet, mode, targets, pinned q, sampling, strategy args, content identity); the encoding phase shall perform none — its invalidation step is empty and its recovery is classification-only.
- Req 39 — A strategy-args fingerprint mismatch shall be catastrophic per strategy: fatal without permission; with it, the wipe shall remove only the changed strategies' attempts and winners.
- Req 40 — A mode change in either direction shall invalidate all winners (the mode tag is the key), and a fixed run whose pinned-quality map matches its persisted key shall perform no invalidation — the pending gate alone decides reuse or resume.
- Req 41 — A metrics-sampling change shall wipe winners and re-measure surviving attempts at pick-up, owned at optimization as one mechanic.
- Req 42 — A search targets change shall wipe winners and rewrite the strategy table as a disk effect (no in-memory table clearing).
- Req 43 — A probe facet change shall be catastrophic at optimization for the whole shared namespace; an equal `--crop` override shall be a no-op, and a differing one shall re-resolve the crop cheaply at probe (frame count kept) with the investment consequence handled by the facet key.
- Req 44 — The optimization phase shall reuse its persisted test-chunk selection only when the full set survives in the current chunking output; partial survival shall trigger a fresh full pick with a log line.
- Req 45 — A chunking detection-params change shall re-detect automatically (user-initiated, non-destructive).
- Req 46 — The audio phase shall invalidate on chain-signature change and chain removal, and if its sidecar is absent while outputs exist, it shall treat the outputs' currency as unknown and reproduce them.
- Req 47 — If a phase's own sidecar is absent while its artifacts exist, the phase shall never treat the artifacts as current: cheap-replay substrates re-derive conservatively and automatically (optimization, audio, extraction); expensive non-replayable artifacts reconstruct their keys from their own durable per-artifact records (merged outputs); encoding is certified cross-phase by the optimization keys.
- Req 48 — Layered retention: attempts shall never be auto-deleted; `encoded/` shall be auto-curated (leftovers deleted); deliverable-layer artifacts shall be retained, with deletion only via permission or explicit cleanup.

## 6. Merge acceptance

- Req 49 — Merge recovery shall classify an output COMPLETE only when the output file is present and its sidecar's provenance matches the current inputs on every recorded dimension (content identity, fingerprint, mode/q/targets-anchor, probe, sampling, winner-set identity); any mismatch or an unverifiable winner-set hash shall mean re-merge — except the content identity, whose mismatch is the phase-level catastrophic condition (fatal / permission wipe).
- Req 50 — A targets or anchor mismatch shall cause a re-merge (the winners were re-searched upstream; the old concat is never current), and the phase shall never re-measure a stale concat.
- Req 51 — Verdicts shall be computed live from persisted metrics; no merged artifact shall persist a verdict.
- Req 52 — Merged outputs shall be measured unconditionally on production: measurement requires the file, reference, and sampling only — no target set shall gate or parameterize the measurement pass.
- Req 53 — Measurement and verdict computation shall be separate steps; the quality evaluator shall measure, and verdicts shall be pure functions of measured metrics and a bar applied by the caller.
- Req 59 — When optimization provides no anchor because none was due (the all-strategies path: no test work, no measurements), the encoding winner-limiter scan shall elect a presentation anchor (the smallest-total-size strategy with measured metrics) for delta display. The election shall be presentation-only: never persisted as a key, never a selection input, and never merge's anchor basis; fast exits replay the persisted table as written; and when an anchor was due but missing (a compared run where no strategy carried measurements), the encoding phase shall surface the optimization error rather than elect silently.
- Req 60 — The merge phase shall not delete deliverable-layer artifacts except under the permission-gated content-identity wipe; probe-facet and all other production-basis changes are per-file provenance mismatches (re-merge that output in place — a crash leaves old outputs present), never wholesale wipes.

## 7. Uniform run flow

- Req 54 — The phase template shall sequence `_invalidate()` before a classification-only `_recover()`, and the pending gate shall dispatch: no pending ⇒ fast exit replaying persisted aggregates with live re-selection only; pending ⇒ execute.
- Req 55 — The fixed-quality + cleanup guard shall be enforced at plan-boundary construction validation (a run-configuration contradiction), not in phase skip logic.
- Req 56 — A derive-only pending (explicit pending with a complete ledger, for stale settings aggregates) shall remain a sanctioned path for rebuilding tables and summaries from durable records.
- Req 57 — The spec shall include a mandatory verification step, performed after all other work finalizes, assessing that the invalidation/recovery separation is clean and that the two state axes — parameter currency (invalidation) and presence completeness (ABSENT/PARTIAL/COMPLETE) — remain conceptually distinct in code and docs.

## 8. Dependency access (TODO §93, moved into this window)

- Req 58 — A `PhaseDependencies` typed view (`self._deps[PhaseCls]` → the asserted typed result, live over the registry, key domain = the phase's `DEPENDS_ON`) shall replace `_dep_result`; the 91 call sites become subscripts, and `_dep_result` retires.
