# Metric-Stat Identity — Tasks

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-27 (requirements + design approved same day)
- Completed:

Branch: `metric-stat-identity` — one PR, two commit waves (Phase A, Phase B),
the three gates green between waves. Version bumps ride each wave (MINOR).

## 0. Cross-spec review (convention)

- [x] 0.1 Review this spec against `2026-10-03 unified-quality-summaries`
  (Req 7 base + Req 11.6 deferral consumed), `2026-05-02 quality-search-v3`
  (typed parameters, mechanics untouched), `2026-04-22 app-metrics-two-tier`
  (naming only), and `2026-10-07 artifact-recovery-ownership` (sidecar field
  typing only); add the supersession/changes summary to the top of this spec
  and each affected one.

## Phase A — the fold

## 1. The identity module (Req 1)

- [x] 1.1 `pyqenc/quality_metrics.py`: `MetricType`/`StatisticType`/
  `MetricInfo`/`_METRIC_INFO`/`MetricStatKey` move out of `quality.py`;
  the module imports nothing from `models`/`quality`; all importers updated,
  no re-export shims.
- [x] 1.2 `MetricMap(Mapping[MetricStatKey, float])`: three abstracts over
  an internal dict (mixin derives the rest), same-role `__eq__`, no hash, no
  mutation surface; zero-behavior role subclasses `MeasuredMetrics`/
  `TargetedMetrics`; owned methods `narrowed_to(pairs)`, `covers(pairs)`,
  `worst_of(maps)`.
- [x] 1.3 Per-role pydantic aliases: `BeforeValidator` parses string keys via
  `MetricStatKey.parse` and wraps into the role instance; `PlainSerializer`
  dumps canonical `sidecar_key()` strings; `arbitrary_types_allowed` on the
  six map-carrying models.
- [x] 1.4 `FIXED_COMPARISON_PAIRS: tuple[MetricStatKey, ...]` — every metric
  × {P10, MEDIAN}, metric-major order; `constants.FIXED_COMPARISON_STATS` and
  `summary_tables.COMPARISON_STATS` deleted, importers rewired.

## 2. The quality target fold (Req 5)

- [x] 2.1 `QualityTarget.identity: MetricStatKey` + `value: float`; string
  fields and `key()` deleted; closed-set validation at construction;
  `parse` delegates to `MetricStatKey.parse`; `targets_as_strings`/`__str__`
  output unchanged.

## 3. Narrowing (Req 3, Req 4)

- [x] 3.1 `EncodingPlan.targeted(metrics) -> TargetedMetrics`: searched →
  the targets' identities; fixed → `FIXED_COMPARISON_PAIRS`; delegates to
  `narrowed_to`.
- [x] 3.2 The five hand-rolled derivations die: `_winner_targeted_metrics`
  (incl. the `rsplit` branch), the pick-up projection, merge
  `_rendered_basis`, merge `_fmt_inline_metrics` fixed projection,
  optimization `_comparison_metrics`.

## 4. Sidecars and records (Req 6)

- [x] 4.1 Attempt / measure / merged-output sidecars → the `MeasuredMetrics`
  alias; winner sidecar → `TargetedMetrics`; `MergedVideo` and `StrategyRow`
  follow their producers; on-disk forms carry over via the validators.

## 5. Consumer retarget (Req 7, Req 8)

- [x] 5.1 `quality.py`: `_score_attempt`/`find_worst_target`/
  `failed_targets`/`_compute_proportional_candidate` consume
  `TargetedMetrics` and address by `target.identity`; `QualityPoint.metrics`
  → `TargetedMetrics | None` (sentinel split stays with V4).
- [x] 5.2 Encoding: pick-up `covers({t.identity …})` + typed lookups; winner
  write takes `TargetedMetrics`; limiter tallies and `worst_key` by identity;
  `Targets:` kv via `str(target)`.
- [x] 5.3 Merge: `_rendered_basis` → `plan.targeted`; inline/missed-target
  rendering by identity (dash form via the pair); `Targets:` kv via
  `str(target)`.
- [x] 5.4 Optimization: anchor reconstruction builds
  `QualityTarget(identity=key, value=…)` from the narrowed anchor map;
  `Targets:` kv via `str(target)`.
- [x] 5.5 `summary_tables`: metric columns / stat rows from pair membership
  in the narrowing sets; `fmt_metric_summary` takes the typed map and renders
  `sidecar_key()`.
- [x] 5.6 `flatten_metric_stats` returns `MeasuredMetrics` (deleted in
  Phase B).

## 6. App-metrics rename (Req 9)

- [x] 6.1 `metrics.py` → `app_metrics.py`; `MetricKey` → `AppMetricKey`;
  `MetricsCollector` → `AppMetricsCollector`; `NoOp`/`Yaml` collectors
  prefixed; `METRIC_KEY_MEASURE` renamed; all source + test imports updated;
  CLI/storage surface unchanged.

## 7. Test surface, Phase A (Req 12)

- [x] 7.1 `test_metric_stat_key` rework: sidecar-form and display-mirror
  tests replaced by round-trip/behavioral assertions; dump-shape and
  yaml-text assertions removed (load→dump→load stability stays).
- [x] 7.2 Construction-rejection tests for `QualityTarget` (replacing
  `TestQualityTargetKey`'s accessor-failure premise); `QualityTarget.parse`
  happy/property/error structure preserved.
- [x] 7.3 `MetricMap` suite: happy (narrowing of a representative full set),
  properties (narrowed ⊆ full by keys, `narrowed_to`∘identity, `covers`
  agreement with subset checks, `worst_of` = per-key min, immutability),
  error (mutation rejected).
- [x] 7.4 Fixture migration to typed constructions (quality-search
  properties, encoding phase/winner-scan, encoding quality); summary-table
  dump-shape assertion → round-trip only; encoding-phase sidecar reads →
  `load_model` round-trips.
- [x] 7.5 Glossary (Req 11) + `docs/architecture.md` pass; TODO §116 status
  line updated.
- [x] 7.6 Gates green (ruff, ty, pytest); commit wave A (version bump
  included).

## Phase B — the parsers

## 8. Per-metric parse (Req 10)

- [x] 8.1 `parse_log` on `MetricInfo` subclasses — four static singletons;
  per-frame plain floats; pandas stays out of the identity module.
- [x] 8.2 Quirks single-homed: PSNR inf fallback + std cutoff (one place),
  VIF scale averaging from the VMAF JSON, SSIM `All` column, factor
  application.
- [x] 8.3 Measurer rewire: shared quantile step stays; `_parsers` dispatch,
  `FullMetricStatistics`, `_extract_key_stats`, `PerMetricStats`/`MetricStats`,
  `flatten_metric_stats` deleted; `QualityMeasurement.metrics` is a flat
  `MeasuredMetrics`; measure-phase nested readers retargeted.
- [x] 8.4 Plot output unchanged.

## 9. Test surface, Phase B (Req 12)

- [x] 9.1 VIF property suite re-pointed at the parse seam, invariants
  preserved.
- [x] 9.2 Differential fixtures across the rehoming, edges included (PSNR
  inf, single-frame chunks, sampling factor); happy/property/error structure
  per agent-testing.md.
- [x] 9.3 Gates green; commit wave B (version bump included).

## 10. Close-out

- [ ] 10.1 Cross-spec summaries finalized (task 0.1) and spec `Completed:`
  dated; TODO §116 removed outright.
