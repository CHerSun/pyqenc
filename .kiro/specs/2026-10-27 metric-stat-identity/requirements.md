# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: Metric-Stat Identity — typed measured/targeted sets, the quality-target fold, per-metric parsers
- Created: 2026-10-27
- Completed:

## Cross-Spec Notes

### What this spec touches

| Touched | Where | What changes |
|---|---|---|
| The minimal identity type (Req 7) and the §116 deferral (Req 11.6) | `2026-10-03 unified-quality-summaries` | Built upon, not changed: `MetricStatKey`/`StatisticType`/`MetricStatMap` land there stay. The Req 11.6 deferral (parser classes, `QualityTarget` field fold, full consumer retarget) is consumed by this spec. |
| Search scoring/verdict mechanics | `2026-05-02 quality-search-v3` | Mechanics untouched; parameters become typed (`TargetedMetrics`, `target.identity`). |
| App-metrics two-tier design | `2026-04-22 app-metrics-two-tier` | Naming only: module + public names gain the `App` prefix. Tier design, `metrics.yaml`, `--no-metrics` untouched. |
| Measure-command parsing internals | `2026-04-02 standalone-measure`, `2026-04-11 all-in-one-metrics` | Parser rehoming (Phase B); measured values pinned numerically identical. |
| Sidecar acceptance/identity model | `2026-10-07 artifact-recovery-ownership` | Untouched; only the `metrics` field typing changes (disk bytes identical). |
| Fixed-mode narrower | `2026-10-02 fixed-quality` | Mechanics unchanged; the constant becomes a typed pair set. |

## Introduction

Measured quality metrics flow as `dict[str, float]` keyed by convention
strings. The pair identity itself is typed (`MetricStatKey`, landed
2026-10-12), but everything around it is stringly: ~25 sites re-derive keys
by hand (`f"{t.metric}_{t.statistic}"`), two vocabularies spell one identity
(`vmaf_min` vs `vmaf-min`), the full measured set vs the targeted subset is
re-filtered by hand at five sites, and the ffmpeg log parsing is a generic
file-type dispatch with two competing statistic-name vocabularies. Nothing
type-checks well-formedness; every consumer guesses.

This spec finishes the fold: one identity module owns the metric vocabulary;
functions declare which set they mean (`MeasuredMetrics` vs
`TargetedMetrics`); narrowing to a targeted subset happens in one call from
one pair-set authority; the quality target embeds the pair; and each metric
parses its own logs. Code, docs, and agents stop guessing.

## Requirements

### The identity module

**Req 1.1** The quality-metrics module (`pyqenc/quality_metrics.py`) shall own the metric vocabulary: `MetricType`, `StatisticType`, `MetricInfo` (and, in Phase B, its per-metric subclasses), `MetricStatKey`, the measured/targeted set types, and the narrowing primitive.

**Req 1.2** The quality-metrics module shall not import `pyqenc.models` or `pyqenc.quality` — it is a leaf below the models layer (the quality target embeds the pair type at construction).

**Req 1.3** The quality module shall import its identity definitions from the quality-metrics module; the definitions move out of it (pre-alpha: importers are updated directly, no re-export shims).

### Set vocabulary

**Req 2.1** The quality-metrics module shall define the metric map as a frozen value class over the pair key — `MetricMap`, a `Mapping[MetricStatKey, float]` (no mutation surface; immutability is structural) — with two role subclasses: `MeasuredMetrics` (the full measured set) and `TargetedMetrics` (a subset narrowed to a pair set); the checker treats the roles as incompatible.

**Req 2.2** Functions consuming or producing measured maps shall annotate the role (`MeasuredMetrics`/`TargetedMetrics`), never a bare dict or the base class.

**Req 2.3** Pydantic sidecar fields shall use per-role annotated aliases (typed map in memory; canonical `vmaf_median` strings on disk — the landed `MetricStatMap` boundary, one alias per role, with parsing/wrapping inside the validators).

**Req 2.4** The map's operations shall be owned methods with live consumers — not operators, not module functions: `narrowed_to(pairs) -> TargetedMetrics` (the narrowing primitive), `covers(pairs) -> bool` (the pick-up's presence check), and `worst_of(maps) -> TargetedMetrics` (the optimization min-aggregation). Set algebra (`&`/`-`-shaped needs) stays on the pair-set side as plain `frozenset` arithmetic; the map defines no operator overloads.

### Narrowing

**Req 3.1** The narrowing primitive shall be the map method `narrowed_to(pairs) -> TargetedMetrics`; there is no module-level narrowing function.

**Req 3.2** The encoding plan shall own the mode-aware front `targeted(metrics) -> TargetedMetrics`, delegating to the primitive: while the plan is searched, it shall narrow to the configured targets' identities; while the plan is fixed, it shall narrow to the fixed comparison pairs (Req 4).

**Req 3.3** Every full→targeted derivation shall go through the primitive or the plan front; the hand-rolled sites are deleted (`_winner_targeted_metrics`, the pick-up's `targets_set` projection, merge `_rendered_basis`, merge `_fmt_inline_metrics`' fixed projection, optimization `_comparison_metrics`).

### The fixed comparison narrower

**Req 4.1** The quality-metrics module shall own the fixed-mode narrower as an ordered tuple of pairs — `FIXED_COMPARISON_PAIRS: tuple[MetricStatKey, ...]` — initially every metric × {P10, MEDIAN} in canonical metric-major order.

**Req 4.2** The pair-set constant shall be the sole narrower authority; the string tuple (`constants.FIXED_COMPARISON_STATS`) and the stat-only tuple (`summary_tables.COMPARISON_STATS`) are deleted.

**Req 4.3** If the constant excludes a pair, then every narrowed set shall omit that pair wherever the producing call is made — shrinking the metric dimension (e.g. dropping SSIM for fixed) is a one-line constant edit with no consumer change.

### The quality target fold

**Req 5.1** The quality target shall hold its identity as the pair (`identity: MetricStatKey`) plus `value: float`; the `metric: str` / `statistic: str` fields are deleted.

**Req 5.2** The identity's closed-set validation shall happen at construction (pydantic field of the enum-typed dataclass), with no accessor-time fallback.

**Req 5.3** `QualityTarget.parse` shall remain the tolerant input boundary (`vmaf-min:95`, `med` alias, case-insensitive), delegating pair parsing to `MetricStatKey.parse`.

**Req 5.4** Persisted and displayed target forms shall be unchanged: `targets_as_strings` (sorted `vmaf-min:95.0`) and `__str__` (`vmaf-min≥95`).

### Sidecar roles

**Req 6.1** The attempt, standalone-measure, and merged-output sidecars shall hold the full set (the `MeasuredMetrics` alias) — the re-judging substrate; the merged-output sidecar is the deliberate user-facing full-set exception (attempts are swept after merge).

**Req 6.2** The winner sidecar shall hold the targeted subset (the `TargetedMetrics` alias).

**Req 6.3** Sidecar persistence shall round-trip: save→load preserves the measured values and pair identities, string-keyed files load into typed maps, and a work directory written before the change stays valid — existing attempts are reused without re-measurement.

### Consumer retarget

**Req 7.1** Scoring, verdicts, worst-target search, and the proportional candidate shall consume `TargetedMetrics` and address values by `target.identity`; the hand-derived `f"{t.metric}_{t.statistic}"` sites are deleted.

**Req 7.2** The encoding pick-up's sidecar acceptance shall test the target identities as a pair-set subset of the sidecar's typed keys.

**Req 7.3** The optimization anchor reconstruction shall build targets as `QualityTarget(identity=key, value=…)` from the anchor's narrowed pairs.

**Req 7.4** The `Targets:` kv lines (encoding, optimization, merge) shall render via `str(target)`; the hand-rolled `f"{t.metric}-{t.statistic}≥{t.value}"` sites are deleted.

**Req 7.5** Log and inline rendering shall derive every key form from the pair (`sidecar_key()` for the attempt-line vocabulary, the dash form for merge inline) — rendered output unchanged.

### Summary tables

**Req 8.1** The comparison tables shall derive metric columns and stat rows from pair membership in the narrowing sets, not from `key.statistic ∈ <stat tuple>`; rendered output unchanged — verified by the table suite's semantic assertions (row order, basis cues, delta signs, marks), never byte diffs.

### App-metrics rename

**Req 9.1** The app-metrics module shall be renamed to `pyqenc/app_metrics.py`, and its public names prefixed: `AppMetricKey` (was `MetricKey`), `AppMetricsCollector` (was `MetricsCollector`), `NoOpAppMetricsCollector`, `YamlAppMetricsCollector` (`AppMetrics` is already prefixed).

**Req 9.2** The app-metrics CLI and storage surface (`--no-metrics`, `metrics.yaml`, dotted keys) shall be unchanged.

### Per-metric parsers (Phase B)

**Req 10.1** Each metric — a `MetricInfo` subclass, one static singleton per metric returned by `MetricType.info` — shall own the parsing of its own measurement output: file extension, line/JSON format, and per-frame values as plain floats (no pandas in the identity module).

**Req 10.2** The quality measurer shall own the shared statistics step (quantiles; pandas stays there); per-metric classes shall not duplicate it.

**Req 10.3** Per-metric quirks shall be single-homed: the PSNR inf fallback and its std cutoff, VIF scale averaging from the VMAF JSON, the SSIM `All` column, and the subsample-factor application.

**Req 10.4** The generic dispatch and the dual statistic vocabularies shall be deleted: the `_parsers` list, `FullMetricStatistics`, `_extract_key_stats`, `PerMetricStats`/`MetricStats`, and `flatten_metric_stats`; the measurement output is a flat `MeasuredMetrics`.

**Req 10.5** The rehomed parsing shall produce the same statistics for any measurement input as before the rehoming — verified differentially on fixtures (including the PSNR-inf, single-frame, and sampling-factor edges) plus the VIF property invariants carried to the new seam.

**Req 10.6** Plot output shall be unchanged.

### Glossary

**Req 11.1** The glossary shall register: **metric-stat pair** (`MetricStatKey`), **measured set / targeted set** (the set vocabulary and narrowing), and the fixed comparison pairs; the quality-target entry shall reference the pair; the app-metrics entry shall carry the renamed module and type names.

### Test surface

**Req 12.1** The suite shall move with the seams: metric-key fixtures become typed map constructions, and every dump-shape, yaml-text, or yaml-key assertion in the affected files is replaced by save→load round-trip, tolerant-load, or behavioral output-token assertions (agent-testing.md; the comparison-table suite's semantic style is the exemplar).

**Req 12.2** The fold inverts the construction contract: `QualityTarget` shall reject invalid identities at construction; the tests asserting the old accessor-time failure are replaced by construction-time rejection tests.

**Req 12.3** Phase B shall re-point the VIF property suite at the per-metric parse seam with its invariants preserved (the current entry points are deleted).

**Req 12.4** The known pinning sites reworked under Req 12.1: `test_metric_stat_key` (the sidecar-form test, the display-mirror test, the dump-shape and yaml-text assertions), `test_summary_tables`' dump-shape assertion, and `test_encoding_phase`'s sidecar yaml-key reads.
