# Recovery rework — naming ownership and the consumption protocol

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-06
- Companion to `per-phase-audit.md`; §100/§101 grounding: who owns every name family today, what changes under static winner names, and how consumption replaces membership testing.

## 1. Name-family inventory (composer / parser / owner)

| # | Family | Compose (today) | Parse (today) | Owner today | Owner target | §101 effect |
| --- | --- | --- | --- | --- | --- | --- |
| N-1 | chunk id `HH꞉MM꞉SS․mmm-…` | `VideoStreamChunk.format_chunk_id` (stream_model:617) | `parse_chunk_id` (:667) — strict inverse pair, pinned by tests | entity ✓ | entity ✓ | none |
| N-2 | attempt file `<chunk>.<res>.q<crf>.mkv` | `EncodedChunk.format_file_name` (stream_model:769) | `EncodedChunk.parse_file_name` (:774) + RAW regex at encoding.py:445, :711, :805 and optimization.py:1054 | entity composes; phases parse via BOTH the entity method and the raw pattern | entity (sole) | unchanged for attempts — q stays in the name (it IS the cache key of the search workspace) |
| N-3 | winner file `encoded/<s>/<chunk>.<res>.q<crf>.mkv` | promotion reuses attempt name (encoding.py:778 — the bug) | regex at :445 + `parse_file_name` at :326/:1550/:1758, optimization.py:1054 | phase (by accident of promotion) | **entity: static `<chunk>.<res>.mkv`** | six parse sites dissolve into static-name lookups; recovery index drops regex |
| N-4 | winner result sidecar `<chunk>.<res>.yaml` | f-string at encoding.py:242 | hand-rolled rsplit+x-digit heuristic at :448-455 | phase | entity (winner stem + `.yaml`; parsing becomes unnecessary under static names) | sidecar name = winner stem swap |
| N-5 | attempt metrics sidecar `<attempt stem>.yaml` | `attempt_path.with_suffix` (encoding.py:183) | same composition at pick-up (:982) | phase convention | entity (attempt stem + `.yaml`) | none |
| N-6 | audio chain output `<stream safe> chain=<name>.<ext>` | `chain_output_path` (audio/chain.py:232) | `_parse_chain_name` (audio.py:480) | split: chain module / phase module | one owner (entity `AudioOutput` or the chain module's family) — compose+parse as inverse pair | none |
| N-7 | merged output `<stem> <strategy>[ <label>=<q>].mkv` | `MergePhase._expected_output_path` + `_q_suffix` (merge.py:841, :880) — phase statics | none (expected-set membership) | phase | `MergedVideo` (display/safe own the base pair today; q-suffix derivable from the strategy's collapsed range) | none directly; owner re-home + §86 interaction |
| N-8 | extracted artifact names (`<stream safe>.<ext>`, attachments bare) | extraction.py:649-689 (`safe_name()` + kind ext; `SubtitleStream.file_extension` entity-side) | none (membership) | phase site over entity name | acceptable as-is (single composition site; ext partially entity-owned) | none |
| N-9 | fixed constants (`timestamps.txt`, `chapters.xml`, sidecar filenames, dir names) | constants.py | — | constants ✓ | ✓ | none |

Doctrine restated: the owner is the only composer AND the only parser; `display_name()`/`safe_name()` is the pair; filesystem work always `safe_name()`; no third accessors; end state has **no pattern-matching for identity anywhere** (regex survives only where it validates free-text input, e.g. include/exclude filters).

## 2. §101 — static winner names: change surface (verified)

Promotion (`_finalize_winning_attempt`):

- name destination `<chunk_id>.<res>.mkv` / `.png` instead of `winning_attempt.name` (encoding.py:775-786);
- result sidecar composition already static (:242) — unchanged;
- `EncodedChunk` gains the winner name family (compose only; parsing unneeded).

Consumers converting from pattern-parse to static lookup:

| Site | Today | Under static names |
| --- | --- | --- |
| `_recover_encoding_attempts` mkv index (encoding.py:443-455) | regex match + hand-rolled sidecar-stem parse | static expected-name membership per pair (via consumption — §3) |
| `_pair_rows` payload (encoding.py:326) | `parse_file_name` → crf/res from name | static name; crf from replay aggregate (sidecar-models.md §4-A); res from... **open: res is NOT in the static name?** — see below |
| parallel pre-population (encoding.py:1550) | same | same |
| `_scan_winner_sidecars` (encoding.py:1758-1769) | regex → derive sidecar path | sidecar path = winner stem + `.yaml` |
| optimization `_aggregate_strategy_metrics` (optimization.py:1054-1059) | regex → sidecar path | same |
| cache-hit `_check_existing_encoding` (encoding.py:710) + cleanup glob (:804) | attempt-pattern glob in `encoding/` — UNCHANGED (attempts keep q-names; this is the workspace, not winners) | unchanged |

**Resolution question — is `<res>` still in the winner name?** Resolution is currently part of the pair's identity because crop can change dimensions. Two candidate static forms:

- `<chunk>.<res>.mkv` — res stays in the name (identity includes output dimensions; a crop change is already a probe-change fatal, so res is stable within a workdir's lifetime); sidecar name pairs trivially.
- `<chunk>.mkv` + res only in the sidecar/aggregate — maximally static, but the sidecar-name pairing needs the res… no: sidecar would also be `<chunk>.yaml`. Simpler still; res becomes a replay-aggregate/sidecar fact.

Recommendation: decide in design; lean `<chunk>.<res>.mkv` (keeps human-debuggability of the dir, zero recovery ambiguity, no new parse).

Pre-alpha note (already in §101): existing q-bearing winners stop matching the static footprint — pairs re-derive as ABSENT (re-encode) or the workdir gets a force-wipe; note it in the fix commit.

## 3. The consumption protocol (§100 mechanics)

One protocol for every listing-recovery phase:

```
listing:  frozen mapping name -> path        (ONE iterdir per owned dir, or per strategy dir)
row:      expected names = payload-owned static set (run-mode context supplied by the phase at row construction)
state:    consume each expected name from the listing
            all consumed   -> COMPLETE
            some           -> PARTIAL
            none           -> ABSENT
after:    leftovers = listing − all consumed names
            -> present-but-unwanted surplus rows (wanted=False; kept in place; deletion only via cleanup)
collision: a name claimed by two rows = naming bug -> loud assert at the consumption site
```

What each phase's leftovers MEAN (the free stale-detection inventory):

| Phase | Leftover after all rows consumed | Today's fate of that leftover |
| --- | --- | --- |
| Extraction | stale subtitle/attachment/container of a re-enumerated source or old filter | invisible (not surfaced) |
| Audio | chain output of a removed/renamed chain, or unrelated chain-token file | surfaced (surplus scan) ✓ — the precedent |
| Optimization/Encoding | winner of an orphan chunk id (re-chunk, re-selection), stray sidecar without mkv, old-resolution winner | invisible (only whole strategy DIRS are orphan-checked) |
| Merge | merge of an unselected strategy, old q output after q change | surfaced via glob ✓ |

Mass vs single-item ownership (the §100 split, as it lands per phase):

| Concern | Owner |
| --- | --- |
| produce the listing(s), once per owned dir | phase |
| row construction + run-mode context (which footprint variant a row expects) | phase |
| expected-name composition (and any parsing) | payload/entity |
| consume + set state | payload (given the listing) or Artifact-level hook driven by the payload's expected set |
| leftovers → surplus rows | phase (aggregation) |
| scheduling, parallelism, progress, per-item production drivers | phase |
| invalidation-key comparison + effects | phase |
| single-item production mechanics (encode-one/merge-one/extract-one) | item machinery (`ChunkEncoder`, chain executor, extractors) — already true |

PARTIAL semantics under consumption: some-expected-present. Attempts-without-winner stays ABSENT (substrate, not artifact) — keep binary there deliberately.

## 4. Producer/consumer composition hypothesis (user sketch, assessed)

Sketch: `File → streams/attachments`, `VideoStream → chunks`, `chunks → EncodedChunk (election from attempts)`, `EncodedChunks → MergedVideo`, with phases owning only mass processing/scheduling/aggregation.

Assessment against the code:

- **Classification + naming to payloads**: directly feasible, this spec (§100 as scoped above). All five current mechanics are phase-local re-derivations of facts the entities already own.
- **Single-item production as payload methods**: partially feasible — `ChunkEncoder.encode_chunk` is already item-scoped machinery (phase passes it context); making the ENTITY the production owner would move run-context (crop, sampling, cleanup, collector) into payloads — wrong layer (payloads are frozen data). Recommendation: production stays with item MACHINERY driven by the phase; the spec states this boundary explicitly so the hypothesis is settled rather than left dangling.
- **"Consumer for many-to-one" (merge)**: merge's expected-set derivation from the winners field is exactly that; under §86's winner-identity key it becomes a typed derivation, not a filesystem relation.

## 5. What deliberately does NOT change

- Attempts keep q-bearing names — they are the search workspace's cache keys; the cache-hit glob and quantized-crf matching at pick-up stay.
- Include/exclude regex over display names — free-text input validation, not identity parsing.
- `Strategy` display/safe passthrough pair; fixed-constant names (N-9).
- The pending gate and the persist-aggregate/replay-on-fast-exit pattern (the consumption protocol strengthens it: consumption leftovers make stale rows visible without any extra reads).
