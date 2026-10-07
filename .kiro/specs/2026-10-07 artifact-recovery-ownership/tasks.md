# Artifact Recovery Ownership — Tasks

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-07
- Completed:

## 0. Cross-spec review (convention)

- [x] 0.1 Review this spec against `2026-09-25 file-stream-model`, `2026-09-28 artifact-model`, `2026-10-02 fixed-quality`, `2026-10-05 cli-intent-commands`, and the §99 optimization-live-selection fix; add a short supersession/changes summary to the top of both this spec and each affected one (notably: winner naming, sidecar shapes, invalidation ownership, §90 split — typing here, summary narrowing stays with `2026-10-03 unified-quality-summaries`).

## 1. Dependency access (TODO §93, first — final access shape)

- [ ] 1.1 `PhaseDependencies` typed view: `self._deps[PhaseCls]` → asserted typed result, live over the registry, key domain = the phase's `DEPENDS_ON` (Req 58); settle `_ensure_dependencies` instance access.
- [ ] 1.2 Sweep all 91 `_dep_result` call sites to subscripts; retire `_dep_result`.

## 2. Fingerprint foundation (Req 10–11a, 10d)

- [ ] 2.1 The `Fingerprint` value type `{token: str, size: int|None = None}` (token REQUIRED; `Fingerprint(None, None)` unconstructible) + uniform comparison (token authoritative; size belt — participates when present on both sides).
- [ ] 2.2 `Strategy.fingerprint` — hash of canonical dump minus the declared exclusion set (`codec.default_quality`), exclusion declared on the model; opaque, never re-validated (Req 10a).
- [ ] 2.3 `ResolvedChain.fingerprint` — rename of the chain signature; `audio.yaml` stores hash tokens, not full chain JSON (Req 10b; behavior change).
- [ ] 2.4 Id-set derivation — hash over sorted chunk ids with `size` = count; one helper serving the chunk-set key and the winner-set provenance field.
- [ ] 2.5 Source fingerprint derivation at Job — blake2b-128 over head + tail + two interior ~1 MiB windows; computed once per run; carried by the live `File` (Req 30).

## 3. Permission model + per-phase identity keys (Req 30–35a, 47)

- [x] 3.1 `job.yaml` → `{path, fingerprint}`; path-only change = locator update rewrite (no fatal/force) (Req 34); place the rewrite per the template (pending → execute).
- [x] 3.2 Delete `JobPhaseResult.force_wipe` and all six `if force_wipe` command-wipes; the raw `--force` permission rides the job result (Req 35a); fatal messages name `--force` truthfully (J-2 fix).
- [ ] 3.3 Wire the source fingerprint key on every phase sidecar (extraction, probe, chunking, optimization base, audio, merge per-output) with the catastrophic branch: fatal without permission, wipe-own with it (Req 33) — closes X-2, P-1, C-2, and the remaining identity rows.
- [ ] 3.4 Unknown-currency: own-sidecar-missing alone triggers the conservative treatment per phase (extraction/audio/optimization wipe + re-derive; encoding cross-certified; merge per-output records decide) (Req 47; the §99 branch stays).

## 4. Naming (Req 1–9b, 25, 61)

- [ ] 4.1 Winner family: `<chunk>.mkv` + `<chunk>.yaml` on `EncodedChunk`; promotion composes the static name; delete the six scattered regex parses and the hand-rolled sidecar-stem parse (E-1/E-2).
- [ ] 4.2 Attempt family: `<chunk>.q<q>.mkv` + stem-swap sidecar; exact-name lookup (no glob/parse); delete the post-encode resolution rename; attempt sidecar gains `resolution` (Req 9, 9b).
- [ ] 4.3 Winner sidecar: drop `winning_attempt`, gain `resolution`; metrics = TARGETED subset only (Req 25).
- [ ] 4.4 Merged output naming re-homes to `MergedVideo` incl. the per-strategy pinned-q suffix whenever collapsed (uniformity gate dropped — M-2b).
- [ ] 4.5 Audio chain-name parse re-homes next to its composer as the inverse pair (Req 8).

## 5. Optimization rework (Req 18–21, 38–44)

- [ ] 5.1 Per-mode union sidecars (tag `mode`; search `targets` / fixed `pinned`; common base: `source`, `chunks`, `strategies`, `probe`, `sampling`); `summary` key for the table; semantic field names (Req 18-19, 61); all keys persisted by the all-strategies path too (Req 20, 35).
- [ ] 5.2 All shared-namespace invalidation here: identity/facet/args-fingerprints = fatal / permission (per-strategy scope for fingerprints); mode/targets/q/sampling/chunks = auto wipe-winners + disk-effect sidecar rewrite (no in-memory table clear) (Req 38-42); fixed same-q = no invalidation — gate decides (O-2); test-chunk set-presence as derivation (O-6); fixed+cleanup guard → plan-boundary construction validation (Req 55).
- [ ] 5.3 Sidecar models re-home to phase modules (Req 21).

## 6. Encoding rework (Req 4, 9a, 12–17, 24)

- [ ] 6.1 Empty `_invalidate`; classification-only recovery: pair ledger over static names, consumption + residual curation (orphan dirs, foreign names — winner-layer auto-delete; attempts kept) (Req 16-17).
- [ ] 6.2 Attempt pick-up = completeness check by sidecar at execution (name match alone never accepts) (Req 9a); `EncodedChunk` drops the crf field entirely (Req 4).
- [ ] 6.3 `encoding.yaml` = ONE `summary` block (limiter + frames), no keys (Req 24, 61).
- [ ] 6.4 Anchor election fallback at the winner-limiter scan when none was due; loud when due-but-missing (Req 59).

## 7. Audio rework (Req 15, 46)

- [ ] 7.1 Listing-first classification (one listing before rows; same listing feeds surplus) (§103/A-2).
- [ ] 7.2 `_invalidate` via composed names (sidecar chain map × dependency stream set × entity composer); conservative unknown-currency wipe (A-1).

## 8. Merge rework (Req 6–7, 27–28, 49–53, 60)

- [ ] 8.1 Typed per-output sidecar: facts (frame count, FULL metrics) + provenance (source — escalates, strategy fingerprint, mode/q/targets-anchor, probe, sampling, winner-set fingerprint) — no verdict (Req 27).
- [ ] 8.2 Per-file acceptance in recovery (Req 49); NO phase-level wipe except the permission-gated identity wipe (Req 60 — M-9); probe facet per-file (M-9); targets/anchor mismatch ⇒ re-merge, never re-measure a stale concat (M-8).
- [ ] 8.3 Unconditional measurement — the `plan.targets` gate dies; evaluator signature split: measure vs verdict-as-pure-function (Req 52-53 — M-6/D13); live verdicts everywhere (Req 51).
- [ ] 8.4 `merge.yaml` = `summary` + basis marker only (Req 28, 61).

## 9. Template split (Req 54, 56)

- [ ] 9.1 `_invalidate()` sequenced before classification-only `_recover()` (disk-effects-only invariant); derive-only pending retained (Req 56).

## 10. Verification and closeout

- [ ] 10.1 Post-implementation re-run of `split-assessment.md` (its checklist: no listing reads in any `_invalidate`; no in-memory handoff; two-axis vocabulary; empty-encoding case study; the five nuances hold).
- [ ] 10.2 Three gates green (`ruff check` + `ty check` via `uvx` + `pytest`); e2e with encoding uses `--strategies "h265*+ultrafast"`.
- [ ] 10.3 Arch-doc update (current-state only) + TODO.md consumption marks (§9, §11, §39, §62, §68, §86, §100, §101, §103, §105 consumed; §90/§92/§106 partial; renumber pointer).
- [ ] 10.4 Update this spec's `- Completed:` date; final commit per convention (semver + conventional, body = actual changes only).
