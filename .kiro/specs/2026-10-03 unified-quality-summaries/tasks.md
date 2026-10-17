# Unified Quality Summaries — Tasks

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-12 (requirements + design approved same day)
- Completed: 2026-10-12

## 0. Cross-spec review (convention)

- [x] 0.1 Review this spec against `2026-10-02 fixed-quality` and
  `2026-10-07 artifact-recovery-ownership`; add a short supersession/changes
  summary to the top of both this spec and each affected one (notably: the
  converged table form superseding the fixed-quality seed's presentation;
  `StrategyRow` converging the two replay row models; the two-anchor
  lifecycle replacing the single-election carry).

## 1. Metric-statistic pair type — minimal scope (Req 7)

- [x] 1.1 `StatisticType` closed set (min, p05, p10, p25, median, p75, p90,
  p95, max, std), validated against what the measurer produces; unknown
  statistics fail loudly at parse.
- [x] 1.2 `MetricStatKey` frozen hashable pair: `sidecar_key()` →
  `vmaf_median` (THE canonical form), `display()` → `VMAF 50%`
  (percentiles as `%` per the plot vocabulary; min/max/std as words),
  tolerant `parse()` (dash/underscore separators, `med` alias).
- [x] 1.3 Pydantic dict-key annotated alias (the `FingerprintYaml`/
  `CropYaml` pattern applied to keys) — canonical strings on disk, typed
  keys in memory.
- [x] 1.4 `QualityTarget.key()` accessor returning the pair (fields stay;
  the field fold is §116's).
- [x] 1.5 Tests: happy (round-trips), properties (parse∘serialize
  identity, display determinism), error (unknown metric/statistic, bad
  separators).

## 2. format_table — the plain-text formatter (Req 10, TODO §119)

- [x] 2.1 The util (`pyqenc/utils/`): headers, rows, per-column
  left/right alignment, dash rule rows, dynamic widths (content + header
  maxima, floor hints), suffix-safe truncation.
- [x] 2.2 Bare columns: content-only zones, no header text, no rule-row
  segment (empty header as the flag).
- [x] 2.3 Rule rows preserve in-cell `|` divider positions (scanned from
  the rendered cells); pipe-free columns keep solid spans.
- [x] 2.4 Logger-plain contract documented on the util: no ANSI, no
  borders, 2-space gaps, narrow-glyph constraint.
- [x] 2.5 Tests: properties (width = max of content/header, truncation
  preserves suffixes, rule length = column width), behavior (bare
  columns, pipe-preserving rules).

## 3. StrategyRow + the comparison renderer (Req 1, 2, 4, 6)

- [x] 3.1 `StrategyRow` pydantic model — `{strategy, size_bytes, metrics:
  dict[MetricStatKey, float]}` — THE persisted shape for both sidecars.
- [x] 3.2 The comparison renderer (`pyqenc/summary_tables.py`): folded
  size cell (MB right-aligned + fixed-width %Δ sub-value), sub-column
  delta cells (per-statistic signed differences, ` | ` divider,
  right-aligned sub-widths), header statistic lists, ruler cues (◎,
  `(src)`, `(anchor)`), convention line, blank-line contract, below-table
  detail lines; every presentation constant named.
- [x] 3.3 Ruler assembly per mode: targets ruler (search — riding the
  source row at merge, standalone at optimization), anchor ruler (fixed),
  uncompared fixed absolutes (no ruler row); size-%Δ basis (source at
  merge, smallest row at optimization; basis row carries no delta).
- [x] 3.4 Marks derivation: optimization = selection only (`◀`, both
  modes); merge = `◀` smallest + live `✔`/`✘` in search (all-targets-pass);
  all marks data-driven from rows + live bar.
- [x] 3.5 Tests: renderer behavior (semantic content asserts — rows,
  marks, deltas, cues — not byte-pinned cosmetics), caveated rendering
  (missing keys → `—`, no source row at optimization, single-stat
  columns).

## 4. Optimization adoption (Req 1, 3, 6, 9)

- [x] 4.1 Tolerance-epsilon dominance: the two-tolerance rule
  (`optimize_tolerance` on size, per-metric `acceptance_delta` on every
  compared statistic, no compensation); anchor = smallest survivor;
  dominator attribution for the below-table `pruned:` lines. Verify
  against the two real-data cases (2026-10-03, 2026-10-09: 0/4 under
  strict dominance).
- [x] 4.2 Search-mode metrics aggregation into rows — the winner-sidecar
  worst-chunk scan wired in search mode too (target-keyed stats).
- [x] 4.3 Both phase tables replaced by the module (search select table
  and fixed comparison deleted); `StrategySummaryRow` → `StrategyRow`,
  persisting the full worst-chunk statistics.
- [x] 4.4 Replay path renders from persisted rows with live-derived
  ruler/marks; delete the `Selected strategies:` line; convention line +
  below-table pruned lines land.
- [x] 4.5 Tests: dominance property tests (tolerance boundary cases both
  dimensions), anchor election, replay fidelity (fast-exit render
  byte-identical to the processing render), selection semantics per mode.
- [x] 4.6 Skip-rule rework (TODO §123): `_skip_check` fires for search runs
  only; `--no-optimize` fixed runs execute the test encodes with dominance
  pruning disabled (every strategy selected; anchor = smallest test size
  over the unpruned population); fixed runs therefore always carry a
  presentation ruler into encoding. Tests: mode-conditional skip,
  single-strategy fixed election, `--no-optimize` fixed selection
  semantics.

## 5. Merge adoption (Req 3, 6, 8; closes §109)

- [x] 5.1 The presentation bar feeds every surface: table ruler, rendered
  keys, and the per-output inline completion line (delete the
  unconditional `plan.targets` pass and `_fmt_inline_metrics`' direct
  read).
- [x] 5.2 Merge anchor election over the full-movie rows (same
  tolerance-epsilon rule); table deltas vs the merge anchor; the anchor
  audit line (test anchor identity via the optimization result; stable at
  INFO, shift at WARNING with both names; shifted → the test anchor
  renders as a regular row).
- [x] 5.3 `MergeSidecar`: rows → `StrategyRow` (rendered basis only),
  `output_dir` once, per-row paths deleted; `Files named`/`Location`
  render from it + the live source stem.
- [x] 5.4 Delete the live re-stat at render (persisted `size_bytes`
  drive the table); delete the miss-detail block and the `Targets:`
  reminder; subsampling footnote whenever sampling > 1.
- [x] 5.5 Tests: mode-conditional rendering, replay byte-identity,
  audit-line behaviors (stable/shift/uncompared), rendered-basis
  persistence.

## 6. Verification render (Req 5)

- [x] 6.1 Render the frozen renderer on a real work dir's data (the
  2026-10-09 fixed-run shape — 4 strategies, anchor, near-ties inside both
  tolerance bars, a dominated case, a mixed-delta row; regenerate to that
  shape if the dir is gone) and present all four tables to the user;
  freeze. Post-freeze changes are constant flips only.
  Verified three ways 2026-10-12: the synthetic render (presented), a live
  `-q 18 h26*+ultrafast` run on `test.mkv` (live optimization + merge
  tables, a real near-tie prune, the anchor-holds audit line, inline
  values-only in fixed mode), and the all-reuse replay (byte-identical
  merge table, zero recompute). The live rerun additionally caught the
  partial-re-test regression — fixed on main in 287e023 with a
  regression test.

## 7. Docs + housekeeping

- [x] 7.1 `docs/architecture.md`: the `summary_tables` module, the
  one-row-shape mechanics, the two-anchor lifecycle (current-state only).
- [x] 7.2 TODO hygiene: remove §97, §109, §111, §123 (consumed here); update
  §119 (internal-formatter decision recorded; remainder = the limiter/
  measure/key-value re-homes); note in §116 that the minimal key type
  landed here.
- [x] 7.3 Full unit-suite green gate (the §114 ffmpeg hang keeps
  `tests/e2e/` out of the gate; any e2e run follows the standing speed
  rule).
- [x] 7.4 Update `- Completed:` (last task).
