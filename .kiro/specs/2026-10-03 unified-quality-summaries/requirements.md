# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: Unified Quality Summaries — one comparison table across optimization and merge
- Created: 2026-10-03
- Amended: 2026-10-12 — convergence rework: one persisted row shape + one renderer for all four populations (optimization/merge × search/fixed); strategy rows show deltas in every mode; %-size deltas replace ratios; tolerance-epsilon dominance (§97) folds in; marks are mode-dependent; the shared plain-text table formatter (§119) folds in; the merge miss-detail block is dropped

## Cross-Spec Notes

### What this spec touches

| Touched | Where | What changes |
|---|---|---|
| Fixed-mode comparison table format (Req 8.4: size Δ% + headline deltas; the compact rendering shipped 2026-10-03) | `2026-10-02 fixed-quality` | The table shape defined here generalizes it: the same table at merge, search runs included. Amended 2026-10-12: the converged form supersedes the seed's details — %-size deltas (not ×ratio), strategy rows always Δ vs the ruler (search included), column headers carry the stat endpoints. |
| "anchor-relative deltas MAY appear in the merged summary" (Req 9.6 — optional polish) | `2026-10-02 fixed-quality` | Promoted from MAY to the core deliverable: the merged summary IS the comparison table. |
| "optimization.yaml SHALL persist … the synthetic set" (Req 8.6) | `2026-10-02 fixed-quality` | Superseded (2026-10-03 review): the synthetic target set is a pure derivation from persisted strategy rows and is no longer stored — the sidecar keeps facts + decisions only, and a changed comparison stat set re-projects old measurements correctly on read. |
| Winner result sidecars carry ALL measured metrics (2026-10-03 change serving the optimization aggregation) | `2026-10-02 fixed-quality` implementation | Reversed per the presentation/retention split (Req 7.4): winner sidecars narrow to the presentation set; the aggregation returns to its retention source (attempt sidecars, full data). |
| Searched-run log invariance (design Correctness Property 1, `2026-10-02 fixed-quality`) | `2026-10-02 fixed-quality` | Redefined for presentation: search runs adopt the unified table (their optimization/merge summary output changes format). Mechanics — behavior, decisions, and non-summary logs — stay invariant. |

### Folded in by the 2026-10-12 amendment

- TODO §97 (epsilon-dominance) — consumed by Req 9: BOTH dimensions tolerate
  (stats: the per-metric `acceptance_delta`; size: `optimize_tolerance`, the
  search-selection band reused). §97's open mechanics questions are answered
  there.
- TODO §109 (fixed-mode merge presentation judges config targets) — closed by
  Req 8.7: the presentation bar feeds the table, the rendered keys, and the
  inline completion line.
- TODO §111 (the four 2026-10-09 table-sketch details) — all four land here:
  size-column numeric form → Req 2.5; generalized metric-column statistics
  → Req 2.4 + Req 7; epsilon-dominance → Req 9; the uniform mark column →
  Req 4. The ◀ selection-marker idea (2026-10-12) is Req 4.1.
- TODO §119 (unify table formatting) — the internal plain-text formatter is
  Req 10, and the comparison renderer is its first consumer. Re-homing the
  remaining tables (winner-limiter, measure summary, key-value) stays in
  §119's remainder, outside this spec.
- TODO §116 (metric-stat value class) — the minimal identity type lands here
  (Req 7); the per-metric parser classes, the `QualityTarget` field fold, and
  the full consumer retarget stay there (Req 11.6).
- TODO §123 (mode-blind optimization skip) — direction folded in (Req 3.4,
  Req 2.7): fixed runs always run the test population and elect an anchor;
  `--no-optimize` fixed = pruning disabled, all selected. The encoding-side
  default-anchor fallback it supersedes was already deleted by this
  amendment's encoding descope.

### Related, not superseded

- `2026-10-07 artifact-recovery-ownership` (predecessor spec) — the per-mode
  sidecar typing and the `summary` replay aggregates landed there; this spec
  builds on them and converges the two row models into one (Req 6.1).
- The EncodingPhase winner-limiter summary — out of scope here (ruled
  2026-10-12): this spec covers exactly the two comparison tables
  (optimization, merge); encoding's summary shape and its ruler input are
  landed reality owned by the fixed-quality spec.
- TODO §82 (multi-metric scoring research) — the table is presentation; no
  composite score is introduced. Reduction-with-bite ideas stay parked there.
- TODO §83 (metrics-absence tolerance) — adjacent via the merge measurement
  set (Req 8); no default flipping here.
- TODO §86 (general merge invalidation) — adjacent via output naming;
  untouched here beyond the `Files named` line riding Req 6.4.
- `2026-05-02 quality-search-v3` — search behavior itself is untouched; only
  its summary tables change.

## Introduction

The optimization and merge summaries currently tell the same story two
ways. Optimization compares strategies on a test-chunk subset (fixed mode:
anchor-relative table; search mode: a size/status list). Merge summarizes
final outputs against config targets. Both are populations of the same
quantities — strategy sizes and metric-statistic values — at growing sample
sizes: test subset, full movie.

This spec unifies the optimization and merge summaries into **one comparison
table shape** with **one ruler row** (the configured targets in search mode,
the anchor in fixed mode), backed by **one persisted row shape** in both
params sidecars and rendered by **one renderer** whose presentation adapts to
the data the population provides — not to phase identity. Deltas are the
primary presentation in both modes (Δ vs targets in search, Δ vs anchor in
fixed); absolutes live on the ruler row alone. The mechanics are identical in
both phases: aggregate from artifacts on processing runs and persist; on
fast-exit runs, emerge the table from the persisted rows. A slimmed
human-judged step remains: the frozen presentation renders on a real work
dir for final visual confirmation (Req 5).

## Glossary

- **Comparison table** — the unified per-strategy summary: one row per
  strategy plus a ruler row; a size column (MB + folded %Δ); one column per
  metric carrying the compared statistics; a mark column.
- **Ruler row** — the ◎-marked row every delta reads against: the configured
  quality targets (search) or the anchor (fixed). At merge in search mode the
  targets ride the source row (its metric cells are otherwise undefined —
  source-vs-source is perfect by construction); at optimization in search
  mode a standalone `targets` row leads.
- **Test anchor** — the smallest-size survivor of tolerance-epsilon pruning
  over the test population; elected once per run at optimization; rules the
  optimization table. Its identity (never its values) flows to merge for
  the anchor audit.
- **Merge anchor** — the same election run at merge over the full-movie
  population; rules the merge table and the fixed-mode presentation bar. It
  does not replace the test anchor — it acts the same way on its own
  population; the test anchor participates at merge only as the audit
  reference.
- **Anchor audit** — the below-table merge line comparing the two
  elections: stable (same strategy both times) at INFO, a shift at WARNING
  with both names; when shifted, the test anchor's strategy renders as a
  regular row whose deltas vs the merge anchor quantify the difference
  in-table.
- **Population** — the measured set a table is built from: test-chunk subset
  (optimization) or merged outputs (merge). Each cell's value is ONE
  aggregate over the population per statistic — the worst-chunk value (min
  across test chunks; a strategy is only as good as its worst chunk, the
  no-compensation principle applied across chunks) at optimization, the
  output's own pooled full-movie measurement at merge. No per-winner
  distribution: pass/miss counting is the winner-limiter summary's business
  (out of scope, Req 11.5).
- **StrategyRow** — THE persisted row shape: `{strategy, size_bytes,
  metrics}`. One shape in `optimization.yaml` and `merge.yaml`; fill breadth
  varies by phase (Req 6.8).
- **Presentation bar** — the live verdict basis: config targets (search) or
  the merge anchor's synthetic set (fixed). Derived at render; never
  persisted as a bar.
- **Below-table detail lines** — exception explanations printed after the
  table (one blank line after the table): pruned-with-dominator (fixed
  optimization), the anchor-audit line (fixed merge), the subsampling
  footnote.
- **Size %Δ** — the folded size-delta sub-value (`456.9 (+0.0%)`): vs the
  source at merge, vs the smallest strategy row at optimization; the basis
  row itself carries no delta.

## Requirements

### Requirement 1 — One comparison table, one row shape, at optimization and merge

**User Story:** As a user, I want the same table telling me the same story at
optimization and merge, so that reading one phase's results trains me to read
the other, and the test-subset preview is directly comparable to the final
outcome.

#### Acceptance Criteria

1. THE pipeline SHALL render one comparison-table shape at exactly two
   places: the OptimizationPhase summary (test-subset population) and the
   MergePhase summary (full-movie population). The EncodingPhase
   winner-limiter summary is out of scope (Req 11.5).
2. Search runs SHALL use the same table shape; their ruler row is the
   configured quality targets (Req 2.2).
3. One persisted row shape — `StrategyRow {strategy, size_bytes, metrics}` —
   SHALL back both tables (`optimization.yaml`, `merge.yaml`), and one
   shared renderer SHALL own every cell; the phases only assemble row
   populations and rulers. No per-phase table formatting survives.
4. Rendering SHALL be caveated on data available, never on phase identity:
   a source row renders only when the population has one; metric columns
   render only for metrics the ruler carries; missing values render `—`.
5. THE merge phase SHALL render rows for strategies that produced outputs
   only (actual results); discrepancies re-actualize them through the
   invalidation ladder — stale rows never render.
6. THE renderer SHALL emit one blank line after the table before any
   below-table detail lines.

### Requirement 2 — The ruler row, deltas everywhere, and the size column

**User Story:** As a user, I want every strategy row read against one stated
ruler and one stated size basis, in every run mode — deltas are the primary
presentation because "how far off" is the question I bring to the table.

#### Acceptance Criteria

1. Fixed mode: the anchor is the ruler row — ◎-marked, absolute metric
   values, bare size (no self-delta). The test anchor rules the
   optimization table; the merge anchor rules the merge table (Req 3). Rows
   SHALL be sorted by size ascending; the ◎ mark locates the ruler wherever
   the sort places it (under tolerance-epsilon dominance a pruned row can
   be smaller than its dominator — first position is not guaranteed).
2. Search mode: the configured targets are the ruler. At merge they SHALL
   ride the source row (◎-marked, `(src)` cue); at optimization a standalone
   `targets` ruler row SHALL lead (no size value).
3. Strategy rows SHALL show deltas vs the ruler in BOTH modes — Δ vs targets
   in search, Δ vs anchor in fixed. Each metric cell carries one delta per
   compared statistic, in the header's statistic order — a plain signed
   difference per statistic, never averaged or otherwise aggregated across
   statistics. The ruler row alone carries absolutes. (Supersedes the
   original search-mode presentation of absolute values in strategy cells.)
4. Column headers SHALL list the compared statistics per metric, in the
   closed statistic set's order, rendered as sub-columns separated by
   ` | `: `VMAF 10% | 50%` for a p10+median basis; `VMAF 50%` when a
   single statistic is targeted; `VMAF 10% | 50% | 90%` for a
   three-statistic basis. Search: the per-metric targeted statistics;
   fixed: the comparison statistics. (The values are parallel statistics —
   sub-columns, not a numeric range.)
5. THE size column SHALL be one column: the MB value with the folded size
   %Δ sub-value (`456.7 (-63.0%)`, `512.3 (-58.5%)`). The %Δ basis is the
   source (merge) or the smallest strategy row (optimization); the basis row
   itself carries no delta — the source row shows the `(src)` cue.
6. All numeric cells SHALL be right-aligned at uniform precision
   (decimal-point alignment); strategy names left-align; name truncation is
   suffix-safe (the `(src)`/`(anchor)` suffix always survives).
7. Single-strategy runs degenerate naturally: in fixed mode the only
   strategy is elected anchor and its ruler row IS the strategy row
   (absolutes by construction); in search mode the targets ruler stays.
   Fixed runs always carry a ruler (Req 3.4) — no-ruler absolute
   rendering has no fixed-mode case (a no-measurement anchor is the
   caveated-rendering exception, Req 1.4).
8. Number formatting SHALL be uniform across the table: one decimal for
   metric values, deltas, and %Δ values; zero deltas render bare (`0.0`);
   the ` | ` value separator, glyphs, and number formats are named
   constants, switchable without renderer changes. ONE value separator
   serves headers, the ruler row's absolute pairs, and strategy delta
   pairs alike; each statistic's value right-aligns within its own
   sub-width so the `|` forms a continuous inner divider across the
   header and all rows (the sub-column effect — the pipe is a divider,
   structurally kin to the table's rules, not a pause between values).
   The dash rule row SHALL preserve the pipe positions within compound
   columns (`---------|------`); pipe-free columns keep solid rule spans.
9. One convention line SHALL precede the table, stating the ruler identity,
   the Δ basis, the size-%Δ basis, and the mark legend (e.g. `ruler:
   configured targets ◎ · Δ vs targets · size Δ% vs source · ◀ recommended`).

### Requirement 3 — Anchor lifecycle: elect per population, audit across populations

**User Story:** As a user iterating on a pinned knob, I want each table
ruled by the anchor elected from its own population, with the test-subset
conclusion audited at full scale — so I see whether the cheap preview's
ruler held, and by how much it moved.

#### Acceptance Criteria

1. The test anchor SHALL be elected exactly once per run, at
   OptimizationPhase, as the smallest survivor of the tolerance-epsilon
   pruning over the test population (Req 9); it rules the optimization
   table. Its values derive on demand from the persisted strategy rows and
   are never stored.
2. The merge anchor SHALL be elected at MergePhase by the same rule over
   the full-movie population; the merge table SHALL compute its deltas
   against the merge anchor. The test anchor's values participate nowhere
   at merge.
3. The test anchor's identity SHALL flow to merge (via the optimization
   result) for the anchor audit only: a below-table line always logs —
   stable at INFO (`anchor holds at full scale: …`), a shift at WARNING
   with both names. When shifted, the test anchor's strategy renders as a
   regular row whose deltas vs the merge anchor quantify the difference
   in-table.
4. Fixed runs SHALL always run the optimization test population and elect
   an anchor (TODO §123): the single-strategy skip applies to search runs
   only; `--no-optimize` fixed runs run the test encodes with dominance
   pruning disabled — every strategy is selected, and the anchor is the
   smallest test size (the unchanged smallest-survivor rule over an
   unpruned population). Search-mode skips stay as today.

### Requirement 4 — Marks: selection at optimization, verdicts only for true targets at merge

**User Story:** As a user scanning the table, I want to see which strategies
were kept and which were cut — and at merge, whether the real targets were
met — without remembering per-mode conventions.

#### Acceptance Criteria

1. Optimization tables (both modes) SHALL carry selection marks only: `◀` on
   selected (search: the tolerance band) / surviving (fixed: dominance)
   strategies; no quality glyphs — the deltas carry the quality story.
2. Merge tables SHALL carry the recommendation mark `◀` on the smallest
   output (both modes). Search mode SHALL additionally carry live `✔`/`✘`
   verdicts against the user targets — `✔` only when every targeted
   statistic is met (the same all-targets-pass semantics as the search
   itself); the missing statistic is the negative delta in its cell. Fixed
   mode SHALL NOT carry verdict glyphs — the anchor is a baseline, never a
   target.
3. All marks SHALL be data-driven — derived at render from persisted facts
   plus the live bar; no glyph is hand-assigned.
4. Below-table detail lines replace in-table annotations: fixed optimization
   names each pruned strategy with its dominator (`pruned: h265+veryfast —
   dominated by h264+ultrafast`); search optimization needs none (the %Δ
   reads directly against the stated tolerance).
5. The standalone `Selected strategies:` line after the optimization table
   SHALL be deleted (the `◀` marks state it); the merge miss-detail block and
   the `Targets:` reminder line SHALL be deleted (the deltas plus the ruler
   row state it); the subsampling footnote SHALL stay whenever sampling > 1.
6. THE mark zone SHALL render as a bare column — content only, no header
   text, no rule-row segment (Req 10.1) — keeping the right edge of an
   already-wide table calm; its legend rides the convention line (Req 2.9).
   Final visual weight is judged at the verification render (Req 5).

### Requirement 5 — Verification render (human-judged, slimmed)

**User Story:** As the primary reader of these tables, I want to see the
frozen presentation on real data once before it ships, knowing every knob is
a named constant I can flip later.

#### Acceptance Criteria

1. The presentation constants are pre-settled (2026-10-12 rulings: ` | ` as
   THE single value separator — the sub-column divider — after rejecting
   the `…`-and-`/` pair and then the middle dot `·` (too weak a pause
   between negative values); `◎` ruler glyph; `(src)` text cue; bare `0.0`
   zero; %-size deltas; one-decimal numbers). The task plan SHALL include
   one visual verification pass: the frozen renderer run on a real work
   dir's data (near-ties, an anchor, a dominated case), presented to the
   user before freeze.
2. Every presentation constant (separators, glyphs, cue strings, number
   formats) SHALL be a named constant — post-freeze tweaks are constant
   flips, not renderer changes.

### Requirement 6 — Aggregate on processing, persist, emerge on fast-exit

**User Story:** As a maintainer, I want identical table mechanics in both
phases — persist prepared rows on processing runs, print them on fast-exit
runs — so reused runs show exactly what the processing run showed, at zero
recomputation cost.

#### Acceptance Criteria

1. Processing runs SHALL follow: read facts (artifacts: winners, merged
   outputs, sidecars) → aggregate into StrategyRow lists → persist in the
   owning phase's params sidecar → render.
2. Fast-exit runs SHALL follow: load the persisted rows → render. No
   winner-sidecar scans, no output re-stat, no recomputation on the fast
   path.
3. THE renderer's input SHALL be typed models only (rows, ruler, marks) —
   never free-form dicts.
4. `merge.yaml` SHALL carry the outputs' directory once (`output_dir`), not
   a path per row; the `Files named`/`Location` lines render from it plus
   the live source stem, reflecting actual naming (including the fixed-run
   ` q<value>` suffix).
5. Replay-only data SHALL NOT be persisted when live-readable from a
   resolved dependency (the source row's stem and size come from the
   JobPhase result).
6. Marks, ruler values, and verdicts SHALL never persist — derived at
   render in both phases, so a changed verdict bar re-renders old rows
   correctly.
7. Fast-exit rendering SHALL be byte-identical to the processing run's
   table; the merge table renders persisted sizes (the live output re-stat
   is deleted).
8. Row fill breadth is the per-phase caveat: optimization rows keep the
   full worst-chunk statistics (min across test chunks, per statistic — the
   ruler re-derivation substrate; a changed comparison stat set
   re-projects); merge rows keep the rendered basis only (full retention
   lives on the per-video sidecars; discrepancies re-actualize).

### Requirement 7 — First-class metric-statistic pair type (minimal scope here)

**User Story:** As a developer, I want one value type owning the
metric+statistic identity and its serialization, so the table plumbing stops
juggling two string spellings — and, as a user, I want presentation surfaces
to show only the statistics worth inspecting, while internal intermediates
retain everything.

#### Acceptance Criteria

1. A metric-statistic pair type SHALL own: identity (metric from the closed
   metric set; statistic from the closed statistic set — min, p05, p10, p25,
   median, p75, p90, p95, max, std), canonical serialization (exactly one
   internal/sidecar form and one display form), and parsing — tolerant for
   user inputs, canonical for sidecars. The type is the single naming
   authority; no other NEW code spells a metric-statistic key.
2. Display forms SHALL follow the plot vocabulary: percentile statistics
   render as percents (`p10` → `10%`, `median` → `50%`); `min`, `max`, `std`
   stay words; metrics render upper-case. Sidecar keys stay `vmaf_median`;
   the CLI/config vocabulary (`vmaf-med:96`, the `med` alias) parses
   unchanged.
3. The closed statistic set SHALL be defined once (validated against what
   the measurer actually produces); unknown statistics fail loudly at parse.
4. **Presentation vs retention** — presentation surfaces (tables, log
   lines, winner result sidecars) show the inspectable set (the comparison
   statistics, or the targeted set where a surface states otherwise);
   retention surfaces (attempt sidecars, per-video merged sidecars) keep the
   full measured set. The optimization aggregation reads retention surfaces
   so the comparison set can be re-selected without re-measuring.
5. Table data paths SHALL use the pair type end to end in NEW code;
   existing stringly sidecar dicts convert at table assembly through the
   type's own parse — no ad hoc `f"{metric}_{stat}"` / `rsplit` plumbing in
   new code.
6. The rework SHALL be mechanical-by-parts: the type lands first with
   tests, then the table module, then the phases — no mixed migration state
   within one phase's data path.
7. Deferred to TODO §116 (Req 11.6): the per-metric parser classes, the
   `QualityTarget` field fold, retargeting the sidecar models and remaining
   consumers, and unifying the visualization internals' `p5`/`p50`
   spellings.

### Requirement 8 — Merge measures what its table consumes

**User Story:** As a user, I want the merged summary's numbers measured, not
assumed — and judged against the bar the run's mode actually pursues.

#### Acceptance Criteria

1. In fixed runs, merge SHALL measure at least the ruler's statistics (the
   comparison set: p10 + median per metric by default) for every merged
   output, in addition to any configured targets. (Already true today: the
   full set is measured; rows filter.)
2. In search runs, merge SHALL measure the configured target statistics (as
   today).
3. The test anchor's identity, the survivors, and the per-strategy facts
   SHALL flow to merge via the optimization result and `optimization.yaml`;
   merge elects its own ruler (the merge anchor, Req 3.2) from its
   full-movie rows. Ruler values are never persisted and never re-measured
   at merge.
4. Merge rows SHALL persist the rendered basis only — the presentation
   bar's keys (fixed: the merge anchor's comparison statistics; search:
   target statistics). No full-stat dumps on the phase sidecar.
5. Per-video merged-output sidecars stay mode-honest as landed (provenance
   mode union: quality (fixed) / targets (search); full measured set under
   `metrics` for retention).
6. `merge.yaml` keeps the summary-replay aggregate only (no identity, no
   invalidation keys — as landed); a fixed merge is therefore never
   invalidated by configured-target changes.
7. THE presentation bar SHALL feed every merge presentation surface: the
   table's ruler, the rows' rendered keys, and the per-output inline
   completion line. (Closes §109 — the processing path stops passing
   `plan.targets` unconditionally.)

### Requirement 9 — Tolerance-epsilon dominance (fixed-mode pruning; TODO §97)

**User Story:** As a user on a pinned knob, I want pruning with a bite —
near-ties inside the tolerances I already configured should count as equal,
so real alternatives get cut instead of everything surviving.

#### Acceptance Criteria

1. In fixed mode, strategy A dominates strategy B iff `size(A) ≤
   size(B) · (1 + optimize_tolerance/100)` AND every compared statistic of
   A is ≥ B's minus that metric's `acceptance_delta`. Both tolerances reuse
   existing bars — the search-selection band (`optimize_tolerance`) and the
   per-metric early-acceptance delta (`acceptance_delta`). Comparison is a
   per-statistic conjunction with NO compensation across statistics or
   metrics — a median surplus can never cover a p10 deficit, mirroring the
   all-targets-pass semantics of search mode (equivalently: the worst
   delta across every compared statistic must still be within tolerance).
   The tolerance also absorbs estimator noise, which is larger in tail
   statistics (p10) than in the median, especially under subsampling.
2. Search-mode selection SHALL stay as is — the size tolerance band over
   strategies with winners; dominance does not apply.
3. Both elections SHALL take the smallest survivor of the tolerance-epsilon
   pruning over their own population — the test anchor at optimization, the
   merge anchor at merge (Req 3).
4. The pruning effect SHALL be verified against the two real-data cases
   (the 2026-10-03 and 2026-10-09 fixed runs: 0/4 pruned under strict
   dominance, with near-ties inside both tolerance bars).

### Requirement 10 — One plain-text table formatter (TODO §119)

**User Story:** As a maintainer, I want column alignment, width fitting, and
rule rows encoded exactly once, so every new table stops re-implementing
them and the look stays ours.

#### Acceptance Criteria

1. THE pipeline SHALL own one internal plain-text table formatter —
   headers, rows, per-column alignment, dash rule rows, dynamic widths, and
   suffix-safe truncation, encoded once. Rule rows preserve in-cell divider
   positions: a column whose cells contain `|` renders its rule with `|` at
   the same offsets, dashes elsewhere; pipe-free columns keep solid spans.
   Columns MAY be bare: content-only
   zones with no header text and no rule-row segment (an empty header is
   the candidate flag), so low-weight trailing zones like marks render
   through the formatter instead of hand-appended text. The comparison
   renderer is its first consumer; re-homing the remaining tables
   (winner-limiter, measure summary, key-value) is TODO §119's remainder,
   not this spec (Req 11.5).
2. The formatter's output SHALL stay logger-plain: no ANSI, no borders,
   2-space column gaps, and a documented narrow-glyph constraint (wide or
   ambiguous-width glyphs are out of scope for table cells by convention).
3. External table libraries are REJECTED (research 2026-10-12): `rich`
   (terminal-display-first, heavy), `tabulate` (buys nothing — every
   compound cell is a pre-formatted string anyway), `wcwidth` (unneeded
   under the narrow-glyph convention).

### Requirement 11 — Explicitly deferred or out of scope

1. Multi-metric composite scoring / stronger strategy reduction → TODO §82.
2. Metrics-absence tolerance, measurement skipping → TODO §83.
3. General merge invalidation (searched-mode winner changes, fingerprints
   vs naming) → TODO §86.
4. QualitySearch V4 → separate effort.
5. The EncodingPhase winner-limiter summary — out of scope entirely (ruled
   2026-10-12): neither its shape nor its data path nor its formatter
   re-home happens here; any later re-home belongs to TODO §119's remainder.
6. The full metric-stat key migration — per-metric parser classes,
   `QualityTarget` field fold, sidecar and remaining-consumer retargeting,
   visualization `p5`/`p50` unification → TODO §116.
