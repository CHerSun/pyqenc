# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: Unified Quality Summaries — one anchor-relative comparison table across optimization, encoding, and merge
- Created: 2026-10-03

## Cross-Spec Notes

### What this spec touches

| Touched | Where | What changes |
|---|---|---|
| Fixed-mode comparison table format (Req 8.4: size Δ% + headline deltas; the compact rendering shipped 2026-10-03) | `2026-10-02 fixed-quality` | The table shape defined here generalizes it: the same table at merge, search runs included. The fixed-quality spec's table remains the seed format; this spec owns it going forward. |
| "anchor-relative deltas MAY appear in the merged summary" (Req 9.6 — optional polish) | `2026-10-02 fixed-quality` | Promoted from MAY to the core deliverable: the merged summary IS the comparison table. |
| "optimization.yaml SHALL persist … the synthetic set" (Req 8.6) | `2026-10-02 fixed-quality` | Superseded (2026-10-03 review): the synthetic target set is a pure derivation from persisted `strategy_results` and is no longer stored — the sidecar keeps facts + decisions only (`strategy_results`, `selected`, `anchor`), and a changed comparison stat set re-projects old measurements correctly on read. |
| Winner result sidecars carry ALL measured metrics (2026-10-03 change serving the optimization aggregation) | `2026-10-02 fixed-quality` implementation | Reversed per the presentation/retention split (Req 7.3): winner sidecars narrow to the presentation set; the aggregation returns to its retention source (attempt sidecars, full data). |
| Searched-run log invariance (design Correctness Property 1, `2026-10-02 fixed-quality`) | `2026-10-02 fixed-quality` | Redefined for presentation: search runs adopt the unified table (their optimization/merge summary output changes format). Mechanics — behavior, decisions, and non-summary logs — stay invariant. |

### Related, not superseded

- TODO §82 (multi-metric scoring research) — the table is presentation; no composite score is introduced. Reduction-with-bite ideas stay parked there.
- TODO §83 (metrics-absence tolerance) — adjacent via the merge measurement set (Req 8); no default flipping here.
- TODO §86 (general merge invalidation) — adjacent via output naming; untouched here beyond the `Files named` line fix riding along.
- `2026-05-02 quality-search-v3` — search behavior itself is untouched; only its summary tables change.

## Introduction

Three phases currently tell the same story three ways. Optimization compares
strategies on a test-chunk subset (fixed mode: anchor-relative table; search
mode: a size/status list). Encoding summarizes winners per limiter. Merge
summarizes final outputs against config targets. All three are populations of
the same quantities — strategy sizes and metric-statistic values — at growing
sample sizes: test subset, per-chunk winners, full movie.

This spec unifies them into **one comparison table shape** with **one
reference row** (the fixed-mode anchor, or the configured targets in search
mode), rendered wherever a strategy population is concluded: optimization
(test subset), merge (full movie), and in delta-capable form at encoding.
It also cleans two foundations the table stands on: a first-class
metric-statistic pair type (today a stringly plumbing burden with two
separators and no closed statistic set), and a facts-first data path (every
table is built from artifacts into persisted prepared data, then printed —
never recomputed from raw facts on fast-exit runs).

A deliberate part of this spec is a **human-judged variant step**: several
renderings of the table (separator, delta emphasis, verdict marks) are built
from real run data and judged by the user before one is frozen.

## Glossary

- **Comparison table** — the unified per-strategy summary: one row per
  strategy plus a reference row; size column; one column per metric carrying
  the compared statistics; selection/verdict marks.
- **Reference row** — the row all deltas are computed against. Fixed mode:
  the anchor (absolute values shown). Search mode: the configured quality
  targets. Single-strategy runs: the strategy itself (deltas degenerate to
  absolutes).
- **Anchor** — as in the fixed-quality spec: the smallest-size survivor of
  dominance pruning. Elected once per run at optimization (test population).
- **Shadow anchor** — the anchor that *would* be elected from a larger
  population (e.g. the full movie at merge). Reported as a signal; never
  replaces the elected ruler.
- **Population** — the measured set a rendering is built from: test-chunk
  subset (optimization), all winning attempts (encoding), merged outputs
  (merge).
- **Prepared table data** — the persisted, render-ready form of a table
  (rows, values, marks) stored in the owning phase's params sidecar.
- **Metric-statistic pair** — the identity of one measured value: a metric
  (VMAF/PSNR/SSIM/VIF) plus a statistic (min, p05, …, max, std). Today
  serialized ad hoc as `vmaf_median` / `vmaf-median` depending on context.

## Requirements

### Requirement 1 — One comparison table shape, at optimization and merge

**User Story:** As a user, I want the same table telling me the same story at
optimization and merge, so that reading one phase's results trains me to read
the other, and the test-subset preview is directly comparable to the final
outcome.

#### Acceptance Criteria

1. THE pipeline SHALL render one comparison-table shape at exactly two
   places: the OptimizationPhase summary (test-subset population) and the
   MergePhase summary (full-movie population).
2. Search runs SHALL use the same table shape; their reference row is the
   configured quality targets (Req 2.2).
3. The EncodingPhase summary is NOT a comparison table — it keeps its
   winning-limiter/chunks shape in both run modes, unchanged; in fixed mode
   its ruler input is the optimization anchor (Req 3.4).
4. A single shared builder/renderer SHALL own the table (model + rendering);
   the two rendering phases assemble populations and marks — no per-phase
   table formatting.
5. Whether the table prints at both optimization and merge, or only once at
   merge (optimization logging reduced to selection facts), SHALL be decided
   in the variant review (Req 5); the default is both — the early signal has
   diagnostic value.

### Requirement 2 — Reference row semantics and mode-conditional formatting

**User Story:** As a user, I want one unambiguous baseline row, so that every
delta reads against a stated thing — with the table's framing following the
run's goal (targets pursued vs knob pinned).

#### Acceptance Criteria

1. Fixed mode: the first row is the anchor — absolute values, bare size (no
   ratio), as today's fixed-quality table. No symbolic `quality` verdict
   indication anywhere in fixed-mode rows (Req 4.2).
2. Search mode: the first row is the configured quality target set — the
   absolute values wanted, shown where measured values otherwise appear; the
   size cell carries the source placeholder (Req 2.4). Search-mode rows carry
   the symbolic `quality` pass/miss indication (Req 4.2).
3. Single-strategy runs (any mode): the strategy is its own reference; the
   table degrades to absolute values with no delta noise.
4. The merged summary SHALL keep a source row (name + size, `%` of source in
   size cells) with an explicit visual cue that it is the source — the ratio
   cell carries a placeholder that keeps column alignment (exact glyph from
   the variant review).

### Requirement 3 — Anchor lifecycle: elect once, present everywhere, audit at scale

**User Story:** As a user iterating on a pinned knob, I want the anchor
elected once per run at optimization and every later presentation measured
against that same ruler, so I can see whether the cheap test subset's
conclusions held at full scale.

#### Acceptance Criteria

1. The anchor SHALL be elected exactly once per run, at OptimizationPhase
   (test population), persisted in `optimization.yaml`, and carried to
   encoding and merge through phase results.
2. The merge table SHALL compute deltas against the elected anchor (fixed
   mode) — the full-movie population never re-elects the ruler.
3. At merge, a **shadow anchor** SHALL be derived from the full-movie
   population (a re-election in fact — reported, never installed) and
   surfaced: an anchor line always logs (both when the shadow matches and
   when it shifts), and a shift is visually more pronounced than a stable
   line — the signal that the test subset did or did not generalize.
4. The EncodingPhase summary keeps its own limiter/chunks shape; in fixed
   mode its ruler is the optimization anchor. Uncompared fixed runs
   (`--no-optimize`, single strategy) SHALL elect a default anchor at the
   encoding summary (smallest total winner size, presentation-only) so the
   limiter table works in delta mode without an optimization comparison;
   single-strategy runs degenerate to absolute presentation.

### Requirement 4 — Marks: selection state always explicit; verdicts only for true targets

**User Story:** As a user scanning the table, I want to see which strategies
were kept, which were cut, and — in search mode — which met the real targets,
without remembering per-mode conventions.

#### Acceptance Criteria

1. Every strategy row SHALL carry its selection state: survivor / pruned
   (with dominator) at optimization; encoded+merged at merge.
2. A quality verdict glyph column (✔/✘) SHALL appear only where true targets
   exist (search mode, judged against config targets). Fixed-mode rows SHALL
   NOT carry target-style verdicts — the anchor is a logical baseline, not a
   target; fixed-mode emphasis variants are settled in Req 5.
3. Merge MAY highlight a recommended row (e.g. the smallest output) — whether
   and how is a variant-review decision, not a selection mechanism.

### Requirement 5 — Human-judged rendering variants

**User Story:** As the primary reader of these tables, I want to judge real
renderings on real data before one format is frozen, rather than approve an
abstract description.

#### Acceptance Criteria

1. The spec's task plan SHALL include a variant step: several complete
   renderings of the table, generated from a real work dir's data, presented
   to the user for judgment; the chosen variant is frozen by name in the task
   list.
2. Variants SHALL cover at minimum: the paired-statistic separator (middle
   dot and wavy line are the leading candidates; `..` clashes with decimal
   points and `/` is visually too heavy), delta emphasis (plain signed
   numbers vs emoji-marked vs tinted — tty coloring only, emojis as the
   portable fallback), the source-row cue/glyph (Req 2.4), and the
   anchor-shift line styling (Req 3.3).
3. Number formatting (one decimal, sign rules, ±0 rendering) SHALL be
   uniform across variants so the review compares structure, not precision.

### Requirement 6 — Facts → prepared data → rendering (tables built from artifacts)

**User Story:** As a maintainer, I want every summary table materialized as
persisted prepared data on processing runs and merely printed on fast-exit
runs, so that reused runs show exactly what the processing run showed, at
zero recomputation cost.

#### Acceptance Criteria

1. Every summary table (comparison tables and the winning-limiter table)
       SHALL follow a three-step path on processing runs: read facts
       (artifacts: winners, merged outputs, sidecars) → build + persist
       prepared table data in the owning phase's params sidecar → render.
2. Fast-exit (fully reused) runs SHALL follow a two-step path: load prepared
       data → render. No winner-sidecar scans, no recomputation on the fast
       path; recovery classification stays listing-only as today.
3. Prepared table data SHALL be typed (a table model), not free-form dicts —
       the model is the renderer's only input.
4. The `Files named` location hint SHALL reflect actual output naming
       (including the fixed-run ` q<value>` suffix).
5. Replay-only data SHALL NOT be persisted when live-readable from a
       resolved dependency at render time (e.g. the source stem and size for
       the summary's source row come from the JobPhase result, not from
       `merge.yaml`). Invalidation comparisons SHALL use the declared
       invalidation keys only (Req 8.7) — never whole-model equality, which
       replay fields would perpetually break.

### Requirement 7 — First-class metric-statistic pair type

**User Story:** As a developer, I want one value type owning the
metric+statistic identity and its serialization, so table plumbing (and the
rest of the pipeline) stops juggling two string spellings with no closed
statistic set — and as a user, I want presentation surfaces to show only the
statistics worth inspecting, while internal intermediates retain everything.

#### Acceptance Criteria

1. A metric-statistic pair type SHALL exist owning: identity (metric from the
   closed metric set; statistic from a closed statistic set — min, p05, p10,
   p25, median, p75, p90, p95, max, std), canonical serialization (exactly
   one internal/sidecar form and one display form), and parsing — a set of
   acceptable separators for user inputs, the single canonical form for
   sidecar serialization/deserialization. The type is the single naming
   authority; no other code spells a metric-statistic key.
2. The closed statistic set SHALL be defined once (validated against what the
   evaluator actually produces); unknown statistics fail loudly at parse.
3. **Presentation surfaces** — tables, log lines, winner result sidecars —
   SHALL present only the inspectable statistic set (the comparison set:
   p10 + median per metric, unless a surface states otherwise).
   **Retention surfaces** preserve the full measured set: attempt sidecars
   (internal intermediates) and per-video merged-output sidecars (the
   re-measure-avoidance record next to each product — a merged sidecar
   carrying full stats means a re-merged/re-measured output never needs
   re-measuring for any future stat set). The optimization aggregation
   reads retention surfaces so the comparison set can be re-selected
   without re-measuring.
4. Table data paths (targets, ruler keys, sidecar metric keys, prepared
   data) SHALL use the pair type end to end — no ad hoc
   `f"{metric}_{stat}"` / `.rsplit("_", 1)` plumbing in new code.
5. The rework SHALL be mechanical-by-parts: the type lands first with tests,
       then call sites migrate phase by phase; no mixed migration state
       within one phase's data path.

### Requirement 8 — Merge measures what its table consumes

**User Story:** As a user, I want the merged summary's numbers measured, not
assumed — and no broader measurement than the table needs.

#### Acceptance Criteria

1. In fixed runs, merge SHALL measure at least the ruler's statistics
   (p10 + median per metric, per the fixed-quality comparison set) for every
   merged output, in addition to any configured targets.
2. In search runs, merge SHALL measure the configured target statistics (as
   today).
3. The optimization anchor SHALL flow to merge as: the anchor identity +
   survivors + per-strategy facts in `optimization.yaml`, and the ready
   ruler via the optimization result. The ruler values themselves are never
   persisted — they derive on demand from the persisted `strategy_results`
   (a changed comparison stat set re-projects old measurements correctly) —
   and merge never re-measures the ruler.
4. Merge prepared data (Req 6) SHALL persist ONLY the measured values the
   table renders (fixed: ruler statistics via the anchor-derived targets;
   search: configured target statistics) — no full-stat dumps on the phase
   sidecar; the full measured set lives on the per-video sidecar (Req 7.3).
5. Per-video merged-output sidecar content SHALL be mode-honest. Search
   runs: `targets` + target-keyed verdict fields, as today. Fixed runs: the
   pinned knob (quality label + value — parseable; the filename carries it
   for humans only) and the ruler basis (anchor identity) instead of a
   config-`targets` block; full measured stats under `metrics` (retention).
6. The merge phase sidecar's invalidation keys SHALL be mode-honest:
   search → (configured quality targets, sampling, probe); fixed → (ruler
   basis — anchor identity + comparison set, sampling, probe). A fixed
   run's `merge.yaml` carries no configured-`quality_targets` key: those
   targets drive nothing in fixed mode, so a fixed merge must not be
   invalidated (nor re-measured) by their change.

### Requirement 9 — Explicitly deferred

1. Multi-metric composite scoring / stronger strategy reduction → TODO §82.
2. Metrics-absence tolerance, measurement skipping → TODO §83.
3. General merge invalidation (searched-mode winner changes, fingerprints
   vs naming) → TODO §86.
4. QualitySearch V4 → separate effort.
5. The EncodingPhase summary table shape (winning-limiter/chunks) — stays as
   is in both modes; only its ruler input and its data path (Req 6, Req 7
   key types) are touched here.
