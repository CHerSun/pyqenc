# Recovery rework — sidecar type models

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-06
- Companion to `per-phase-audit.md`; reviews every sidecar's TYPE, CONTENT CLASS, read/write sites, and the per-mode split question (§90/§105).

## 1. Complete sidecar inventory

| Sidecar | Model | Home today | Content class | Read sites | Write timing |
| --- | --- | --- | --- | --- | --- |
| `job.yaml` | `JobSidecar` | stream_model (§62: should move to job.py) | identity facts (source path+size) | job recovery | execute |
| `extraction.yaml` | `ExtractionSidecar` | stream_model | inventory facts + source identity | extraction recovery | execute (dirty/materialize) |
| `probe.yaml` | `ProbeState` | state.py | facet facts (frame_count, crop) | probe recovery; measure standalone | execute |
| `chunking.yaml` | `ChunkingSidecar` | stream_model | facts (scene boundaries) — **params missing** | chunking recovery | execute (on detection) |
| `optimization.yaml` | `OptimizationParams` | state.py | params + derived table (aggregate) | optimization recovery ×2, `_all_strategies` | single post-success save; **always rewritten on the all-strategies skip path** |
| `encoding.yaml` | `EncodingParams` | state.py | params snapshot (probe) + replay aggregates (limiter table, frame totals) | encoding recovery (fast-exit source) | TWO writes on processing: crash-safe early (probe-only) + post-success aggregates |
| `audio.yaml` | `AudioSidecar` | state.py | intent signatures (chain name → sig string) | audio recovery | **before production** (commit-intent) when it differs |
| `merge.yaml` | `MergeParams` | state.py | invalidation keys + summary aggregate | merge recovery, `_reused_result` (2nd load) | post-success (any complete output) |
| `<attempt>.yaml` | `MetricsSidecar` | state.py | attempt FACTS (crf, sampling, frame_count, all metrics) | cache-hit check at pick-up (processing) | per attempt |
| `<chunk>.<res>.yaml` (winner) | `EncodingResultSidecar` | state.py | pair FACTS + one concluding verdict (`targets_met`) | winner scan, optimization aggregation (processing paths) | per promotion |
| `<output>.yaml` (merged) | **untyped dict** | merge.py | facts + verdict + mode-conditional blocks | **merge RECOVERY (content!)**, summary | per output |
| `metrics.yaml` | — | metrics.py | app metrics (out of scope) | — | periodic |

Content-class rules being applied (from the sidecar-facts-only doctrine + this audit):

1. **Facts** (what this file/production IS): always safe to persist; never invalidated by foreign state.
2. **Params/keys** (what produced it — the invalidation comparison basis): belong on PHASE sidecars only; compared at recovery.
3. **Aggregates** (replay-only summaries): phase sidecars; freshness guaranteed by the pending gate.
4. **Verdicts** (comparisons vs foreign state): only on the CONCLUDING layer of the thing they conclude (winner sidecar's `targets_met`, merged sidecar's `targets_met`); re-derived everywhere else.

## 2. Phase sidecars: the fixed/search split (§90, §105)

### What actually differs per mode

| Sidecar | Search-run shape | Fixed-run shape | Common |
| --- | --- | --- | --- |
| `OptimizationParams` | `quality_targets` = the key; table = sizes only (searched table builds without metrics) | key = **pinned q per strategy** (today: nothing — wipe is unconditional, O-2); table = sizes + min-aggregated comparison metrics | probe, test_chunks, sampling |
| `MergeParams` | `quality_targets` key | `anchor` key | sampling, probe, strategy_summaries |
| `EncodingParams` | no mode-conditional key (targets owned upstream; presentation basis flows from Optimization's result) | same | probe, limiter table, frame totals |

### Recommendation

- **Split `OptimizationParams` and `MergeParams` into per-mode variants** via a pydantic discriminated union (`mode: Literal["fixed"] / Literal["search"]`, `Field(discriminator="mode")`), sharing a common base for the common fields — difference by TYPE, not by construction (user-endorsed for `MergeParams` 2026-10-05; §90's census). Mode-specific key comparison re-homes onto the variants (`search.matches(current)` / `fixed.matches(current)`), killing the `if fixed … else` key selection at merge.py:286-299 and the `bool(persisted.quality_targets)` guard at optimization.py:391-395.
- **Discriminator tag over try-both** (§105 asked): a tagged file self-describes in one parse; try-both makes a degenerate fixed file (missing q) silently parse as a valid search file — exactly the class of drift the tag exists to prevent. Pre-alpha: no migration, the tag is simply present from the first typed write.
- **Fixed variant's key = the pinned knob map** `{strategy display_name: quantized quality}` (+ label). This simultaneously fixes O-1 (mode is the union tag ⇒ any switch re-keys) and O-2 (same-q fixed rerun compares equal ⇒ true fast exit; the unconditional `_fixed_mode_entry` wipe becomes the mismatch branch).
- `quality_targets` then exists ONLY on the search variant (§90: omit the key entirely for fixed runs — today's "empty list reads as no targets" ambiguity dies).
- **Do not split `EncodingParams`** — it has no mode-conditional key; its aggregates' basis is upstream-invalidated (anchor change ⇒ optimization invalidation ⇒ pending gate reopens encoding).
- The per-video merged sidecar (untyped dict today) gets a typed model in the same pass (§90 candidate): facts (`frame_count`, metrics — unify to FULL measured set for retention symmetry with `MetricsSidecar`? see open questions), mode-conditional verdict/keys, drop `plot` (derivable from stem) and `anchor` (fleeting election artifact — lives at optimization).

### Where sidecar MODELS live (§62)

All phase sidecar models re-home to their owning phase modules (typed results stay in dataclass result classes near their phase; `state.py` keeps only genuinely shared primitives like `ArtifactState`). Adjacent to §93's `_deps` sweep — same files, same window shape.

## 3. Artifact sidecars: facts-only status

| Sidecar | Facts | Verdicts/foreign state | Verdict |
| --- | --- | --- | --- |
| `MetricsSidecar` (attempt) | crf, sampling, frame_count, ALL metrics | none — pass/fail re-evaluated live (`encode_chunk` cache-hit computes `targets_met` from metrics + CURRENT targets) | **already the model** ✓ |
| `EncodingResultSidecar` (winner) | winning_attempt, crf, metrics, frame_count | `targets_met` — the pair's concluding verdict, consumed by the limiter scan | keep (concluding layer), see provenance below |
| merged output sidecar | frame_count, metrics, quality block (fixed) | `targets_met` + `targets` block (search); `anchor` (fixed) | keep verdict; drop `anchor`, `plot` (§90); type the dict |

Two doc/code drifts found (fix in spec): `EncodingResultSidecar.metrics` docstring claims target-filtered, code persists all measured (code is right); `winning_attempt` becomes redundant under §101 static names (the winner file's name is derivable from the pair identity — the field degrades to a consistency note; keep or drop, decide in design).

## 4. Winner provenance — where does crf live when names go static? (§101 follow-up)

Today crf-at-recovery comes from parsing the q-bearing winner filename (encoding.py:326, :1550). Static winner names remove that source. Consumers of a winner's crf:

| Consumer | Path | Frequency |
| --- | --- | --- |
| fast-exit winner payloads (`EncodingPhaseResult.winners`) | recovery | every reused run |
| merge CRF plot (`_collect_crf_data` reads `payload.crf`) | merge processing | rare (re-merge/re-measure) |
| winner scan med-CRF column | encoding processing | every concluded pass (reads sidecars anyway) |
| optimization fixed ruler (`_aggregate_strategy_metrics`) | optimization processing | fixed runs only |

Options:

| Option | Mechanics | Cost profile |
| --- | --- | --- |
| **A. Phase-sidecar replay aggregate** (recommended) | `encoding.yaml` gains `winners: {strategy: {chunk_id: {crf, res}}}` — same pattern as `limiter_summary`/`winners_frame_totals` (persist on processing, replay on fast exit; pending gate guarantees freshness) | ONE read per reused run; zero recovery sidecar reads; compositions at recovery read the already-loaded phase sidecar |
| B. Lazy composition | payloads carry no crf at recovery; consumers read the winner sidecar at need | zero reads on pure fast exit; spreads reads; weakens the typed payload contract (`crf` no longer always present) |
| C. Keep q in winner filenames | rejected — that IS §101's bug |

Recommendation: **A**. It keeps `EncodedChunk.crf` always-populated (no Optional creep), keeps recovery listing-only, and reuses an established, gate-freshness-backed pattern. The winner sidecar remains the durable per-pair record; the phase aggregate is replay-only (same retention split as summaries).

## 5. Recovery I/O budget (target state)

| Phase | Listings at recovery | Sidecar reads at recovery |
| --- | --- | --- |
| Job | 0 | 1 (`job.yaml`) |
| Extraction | 1 (`extracted/`) | 1 (`extraction.yaml`) |
| Probe | 0 | 1 (`probe.yaml`) |
| Chunking | 0 | 1 (`chunking.yaml`) |
| Optimization | 1 per strategy dir (`encoded/<s>/`) | 1 (`optimization.yaml`) |
| Encoding | 1 per strategy dir (shared machinery) | 1 (`encoding.yaml`) |
| Audio | 1 (`audio/`) | 1 (`audio.yaml`) |
| Merge | 1 (`merged/`) + per-output provenance reads (few files — sanctioned low-N terminal dir) | 1 (`merge.yaml`, summary aggregate only) |

Every cell is O(strategies) or O(1) directory scans + exactly one params sidecar — plus, at merge only, O(outputs) per-file provenance reads (Req 41: per-file reads allowed at O(strategies) scale; high-N dirs stay listing-only). Today's deviations from this table: audio's per-row stats (§103), encoding's regex/index construction (dissolves under §101) — merge's content reads become purposeful (identity-bearing) under the provenance design.

## 6. Open questions for the spec (sidecar domain)

1. ~~Merged sidecar metrics breadth~~ **RESOLVED 2026-10-07 (user, merge validation):** FULL measured set in BOTH modes — and stronger than the earlier lean: the merged-output sidecar carries **no verdict at all** (verdicts live everywhere at merge; the summary rebuild computes them on the processing path). Targets/anchor changes thereby invalidate nothing measured — derive-only summary rebuild instead of today's re-measure (GAP M-8; Req 37–39).
2. `EncodingResultSidecar.winning_attempt` under static names: keep as provenance note or drop.
3. Chunking sidecar gains `scene_threshold` + `min_scene_length` as keys — confirm scope (§47 adjacency).
4. Attempt-sidecar `sampling` staleness drives re-measure at pick-up; under the winners-replay-aggregate world nothing changes — confirm no interaction.
