# Process flows — per-phase, condition → knob → outcome

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-07
- Purpose: the connected view of the settled design (`requirements.md`, `design.md`, rulings in `spec-plan.md`). Each phase has **few knobs** — the tables below prove it by listing them all. Links: `Req N` → requirements.md; `D#` → spec-plan decisions.

**Legend (all charts):**

| Shape / style | Meaning |
| --- | --- |
| diamond | a condition check (key comparison unless noted) |
| cylinder | FATAL — stops unless `--force` (permission, Req 35a) |
| orange box | an invalidation EFFECT (one vocabulary, design §2a): wipe-own / wipe-winners / re-produce / rewrite-own-sidecar |
| blue box | recovery = completeness classification (consume listing → ABSENT/PARTIAL/COMPLETE) |
| green box | processing (`_execute`) |
| dashed blue box | fast exit (no pending): replay aggregates + live re-selection |
| `ident≠` | own persisted content identity ≠ live content identity (the one canonical source condition, Req 30–33) |

## 0. The one template (every phase is an instance)

```mermaid
flowchart TD
    INV{"_invalidate(): compare own keys"} -->|"fatal cond, no --force"| F[("FAILED: message names --force")]
    INV -->|"fatal cond + --force"| WIPE["wipe own artifacts (permission band)"]
    INV -->|"soft cond"| EFF["auto effect: wipe-winners / re-produce / rewrite own sidecar"]
    INV -->|"no condition fired"| REC{"_recover(): consume listing → states + leftovers"}
    WIPE --> REC
    EFF --> REC
    REC -->|"pending"| EX["_execute(): produce missing; persist aggregates once"]
    REC -->|"no pending"| FE["FAST EXIT: replay aggregates, live re-selection"]
    EX --> OUT["typed result → downstream"]
    FE --> OUT
    F -.-> OUT
    classDef fast fill:#eef6ff,stroke:#335577,stroke-dasharray: 6 4
    class FE fast
```

Knob count per phase: **Job 2 · Extraction 2 · Probe 3 · Chunking 2 · Optimization 9→4 effects · Encoding 0 (!) · Audio 3 · Merge 2.**

## 1. Job — identity owner

Inputs: source path, config flags (`--force`, cleanup, no-metrics). Knobs:

| Condition | Effect |
| --- | --- |
| `ident≠` (size+digest), no permission | **fatal** |
| `ident≠` + permission | nothing at Job itself — each downstream phase wipes its own (keys re-detect every run; no flag to lose, D3) |
| path-only change | locator update: rewrite `job.yaml` (no fatal, D11) |
| `job.yaml` missing | rebuild (probe identity) |

```mermaid
flowchart TD
    A{"job.yaml present?"} -->|no| P["probe File + digest, pending → write"]
    A -->|yes| B{"content identity matches?"}
    B -->|"size/digest differ"| C{"--force?"}
    C -->|no| F[("FAILED")]
    C -->|yes| OK["continue: downstream phases see their own ident≠"]
    B -->|"path differs, content same"| LOC["rewrite job.yaml (locator update)"] --> OK
    B -->|match| OK
    OK --> REC["row: File COMPLETE (identity verified)"]
    P --> REC
    REC --> OUT["JobPhaseResult: File, config, permission flag"]
```

Outputs: the run context every phase reads; **permission rides the result** (the old `force_wipe` wipe-order is deleted).

## 2. Extraction — inventory + container artifacts

Inputs: `File`, include/exclude filter, mode (`video_required`, `materialize`). Knobs:

| Condition | Effect |
| --- | --- |
| `ident≠` | fatal / wipe `extracted/` + sidecar (permission) |
| sidecar missing **with files present** | conservative: wipe + re-extract (unknown currency, Req 47) |

```mermaid
flowchart TD
    INV{"ident≠?"} -->|"yes + --force"| W["wipe extracted/ + sidecar"]
    INV -->|"yes, no --force"| F[("FAILED")]
    INV -->|no| R{"resolve inventory: load sidecar or ffprobe; compose expected names"}
    W --> R
    R --> L["ONE listing of extracted/ → consume names → COMPLETE / PARTIAL / ABSENT; leftovers retained + surfaced"]
    L -->|"pending"| X["extract missing components; persist inventory"]
    L -->|"no pending"| FE["FAST EXIT: stream table"]
    X --> O["stream artifacts + timestamps_path"]
    FE --> O
    classDef fast fill:#eef6ff,stroke:#335577,stroke-dasharray: 6 4
    class FE fast
```

Filter/intent changes are **not knobs** — they only re-derive `wanted` (orthogonal).

## 3. Probe — the slow facet

Inputs: video stream, `--crop` override. Knobs:

| Condition | Effect |
| --- | --- |
| `ident≠` | re-probe (cheap; the downstream facet key handles the investment loss) |
| `--crop` **equals** committed crop | **no-op** (P-2) |
| `--crop` **differs** | re-resolve crop, keep cached frame count (permission question fires downstream via the facet key, not here) |
| sidecar missing | re-probe |

```mermaid
flowchart TD
    A{"probe.yaml current?"} -->|"missing / ident≠"| X["re-probe: crop = manual → detect; frames = cached → timestamps → null-count"]
    A -->|"crop override present"| B{"equals committed?"}
    B -->|yes| N["no-op — reuse as-is"]
    B -->|differs| X2["re-resolve crop only (frames stay cached)"] --> W["write probe.yaml + crop_source"]
    A -->|current| N
    X --> W
    N --> O["ExtendedVideoStream → every video phase"]
```

The facet (frame count + crop) is a **structured key downstream** — compared field-wise, never whole-model (Req 23).

## 4. Chunking — boundaries

Inputs: extended stream, `scene_threshold`, `min_scene_length`. Knobs:

| Condition | Effect |
| --- | --- |
| `ident≠` | fatal / wipe sidecar + re-detect (permission) |
| detection params ≠ | **auto** re-detect (user-initiated config; nothing deleted — D8/C-1) |
| sidecar missing | re-detect (deterministic) |

```mermaid
flowchart TD
    A{"keys match? ident + params"} -->|"ident≠"| F[("FAILED / wipe + re-detect")]
    A -->|"params≠"| X["re-detect (auto)"]
    A -->|match| D["derive windows: boundaries + CURRENT stream (derivation, not keys)"]
    X --> W["write chunking.yaml"] --> D
    D --> O["N chunk windows (virtual) → optimization + encoding"]
```

Re-chunk ripples need **no knob here** — new chunk ids change the *wanted set* downstream; old winners surface as consumption leftovers (Req 16).

## 5. Optimization — owns the shared namespace (`encoding/` + `encoded/`)

Inputs: plan (mode, strategies+fingerprints, targets / pinned-q map), chunks, probe facet, sampling, tolerance, `optimize` flag. **Encoding adds none of its own** (Req 38) — this is "one pipeline in two steps."

| Condition (own sidecar, per-mode variant) | Effect |
| --- | --- |
| `ident≠` · probe facet ≠ | **fatal** / wipe attempts+winners+keys (permission) |
| fingerprint ≠ (per strategy) | **fatal** / wipe that strategy only (permission) — D4 |
| mode ≠ (either direction) | wipe winners (auto) — O-1 |
| **chunk-set fingerprint ≠** (re-chunk; same derivation as merge's winner-set) | wipe winners at `_invalidate` (auto) — ahead of recovery/search, uniform with merge |
| search: targets ≠ · fixed: pinned map ≠ | wipe winners + rewrite table (auto) — fixed **equal** = no-op, gate decides (O-2) |
| sampling ≠ | wipe winners; attempts re-measure at pick-up (auto) |
| test-chunk set not fully present | fresh full pick (auto) — O-6 |
| sidecar missing with winners present | conservative wipe winners (§99) |
| pairs COMPLETE but table stale/absent | **derive-only pending**: rebuild table from winners |

```mermaid
flowchart TD
    S{"optimize on, 2+ strategies?"} -->|no| AS["all-strategies result (persists ALL keys — Req 20)"]
    S -->|yes| INV{"compare keys: ident · facet · fingerprints · mode · targets/q · sampling"}
    INV -->|"fatal band"| F[("FAILED / wipe namespace")]
    INV -->|"winner band"| WW["wipe winners + table rewrite"]
    INV -->|match| R{"pair ledger: winners presence by static names → to-test projection"}
    WW --> R
    R -->|"pairs pending"| X["test-encode pending pairs → derive table: sizes from listing, metrics from winner sidecars"]
    R -->|"complete + stale table"| DO["derive-only: rebuild table, rewrite sidecar"]
    R -->|"complete + fresh"| FE["FAST EXIT: live selection — tolerance / dominance / anchor; never persisted"]
    X --> SEL["select → persist sidecar once (post-success)"]
    DO --> SEL
    SEL --> O1["selected_strategies + anchor + synthetic ruler (compared runs)"]
    AS --> O2["selected_strategies ONLY — no anchor, no ruler on this path (no work, no measurements); the encoding limiter elects a presentation anchor (Req 59)"]
    FE --> O1
    classDef fast fill:#eef6ff,stroke:#335577,stroke-dasharray: 6 4
    class FE fast
```

## 6. Encoding — zero invalidation, classification + production

Inputs: chunks, selected strategies, presentation targets/ruler, crop, sampling. **No `_invalidate`** (the empty case study, D12).

```mermaid
flowchart TD
    R{"_recover_: pair ledger; expected = chunk-ids × selection, static names mkv + yaml"} --> L["consume listings → COMPLETE / ABSENT; leftovers (old chunk-ids, orphan dirs) → winner-wipe effect (auto curation, Req 16-17)"]
    L -->|"pending"| X["per pending pair: search proposes q → attempt at exact address chunk.q-value.mkv"]
    X --> C{"attempt sidecar complete? pick-up completeness check — Req 9a"}
    C -->|"valid facts"| HIT["cache hit → steer search"]
    C -->|"stale sampling"| RM["re-measure (no re-encode)"]
    C -->|missing| ENC["encode → write attempt + sidecar"]
    HIT --> CONV["converged → promote winner: chunk.mkv + facts sidecar"]
    RM --> CONV
    ENC --> CONV
    CONV --> AG["post-success: limiter table + frame totals on encoding.yaml; ELECTS a presentation anchor when optimization provided none (all-strategies runs — Req 59); loud when an anchor was DUE but missing"]
    L -->|"no pending"| FE["FAST EXIT: replay aggregates, re-assert frame preservation"]
    AG --> O["winners → merge"]
    FE --> O
    classDef fast fill:#eef6ff,stroke:#335577,stroke-dasharray: 6 4
    class FE fast
```

Attempts are **never classified, never individually invalidated** — the substrate, consulted lazily (Req 9a).

## 7. Audio

Inputs: tracks (extraction), chains+select (config). Knobs:

| Condition | Effect |
| --- | --- |
| chain fingerprint ≠ | delete that chain's outputs → reproduce (auto, Req 10b) |
| chain removed from config | cleanup delete (auto) |
| sidecar missing with outputs | conservative reproduce (A-1/Req 47) |
| `ident≠` | fatal / wipe (permission) |

```mermaid
flowchart TD
    INV{"compare chain fingerprints + ident"} -->|"chain≠ / removed / unknown"| DEL["delete that chain's outputs"]
    INV -->|match| R{"ONE listing before rows (Req 15): expected track×chain by membership; same listing → surplus"}
    DEL --> R
    R -->|"pending"| X["execute chains (parallel, per-row failure isolation)"]
    R -->|"no pending"| FE["FAST EXIT"]
    X --> O["outputs"]
    FE --> O
    classDef fast fill:#eef6ff,stroke:#335577,stroke-dasharray: 6 4
    class FE fast
```

`select` changes are not knobs — live `wanted` only.

## 8. Merge — per-file acceptance flag

Inputs: winners, plan (mode, targets / ruler+anchor, pinned q), probe facet, sampling, stem, timestamps. Knobs:

| Condition | Effect |
| --- | --- |
| `ident≠` | fatal / **wipe `merged/`** (permission — the ONE deliverable-layer wipe) |
| per-output **provenance** ≠ (fingerprint · mode/q/targets-anchor · **probe facet** · sampling · winner-set fingerprint — Req 27/49/60) | re-merge that output in place (deliverables retained — a crash leaves old outputs present) / re-measure only (sampling-only difference); targets/anchor mismatches ⇒ re-merge — the winners were re-searched upstream (M-8) |

```mermaid
flowchart TD
    INV{"ident≠?"} -->|"no --force"| F[("FAILED")]
    INV -->|"--force"| W["wipe merged/ — the ONE permission-gated deliverable wipe"]
    INV -->|match| R
    W --> R{"per expected output (low-N reads): file + sidecar present? provenance matches? (fingerprint · mode/q/targets-anchor · probe facet · sampling · winner-set fingerprint); name collisions revalidate"}
    R -->|COMPLETE| FE["FAST EXIT: replay summary + LIVE verdicts"]
    R -->|"absent / provenance≠"| X["re-merge: concat (mkvmerge → propedit → promote) → measure UNCONDITIONALLY: file + reference + sampling, no bar (Req 52)"]
    R -->|"file present, only sampling≠"| XM["re-measure only"]
    X --> SC["verdicts LIVE: search vs targets; fixed anchor-relative presentation — no verdict persisted"]
    XM --> SC
    SC --> W["write per-output sidecar: facts + provenance; summary → merge.yaml (replay + basis marker)"]
    W --> O["merged deliverables"]
    FE --> O
    classDef fast fill:#eef6ff,stroke:#335577,stroke-dasharray: 6 4
    class FE fast
```

Stale outputs of removed strategies / old q values: **retained deliverables** (rename-for-taste is the user's); a name collision is a conflict signal → revalidate.

## The knob census

| Phase | Fatal-band knobs | Winner/auto knobs | Total conditions |
| --- | --- | --- | --- |
| Job | 1 | 1 (locator rewrite) | 2 |
| Extraction | 1 | 1 (unknown-currency wipe) | 2 |
| Probe | 0 | 3 (re-probe, crop equal/differs) | 3 |
| Chunking | 1 | 1 (params re-detect) | 2 |
| Optimization | 3 (ident, facet, fingerprint) | 6 (mode, targets/q, sampling, test-set, chunk-set, missing-sidecar) + derive-only | 9 conditions → **4 effects** |
| Encoding | **0** | 1 (consumption curation) | 0 keys |
| Audio | 1 (ident) | 3 (chain≠, removed, missing-sidecar) | 3-4 |
| Merge | 1 (ident — the only deliverable wipe) | 1 (per-file provenance → re-merge / re-measure) | 1 + per-file fields |

Every phase: **at most 3 fatal-band knobs; most work rides 1-2 auto effects.** The complexity lives in the *typed sidecars* (which fields are keys — Req 19's lists), not in the flow.
