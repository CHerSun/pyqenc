# Design Document

<!-- markdownlint-disable MD024 -->

- Spec: Metric-Stat Identity — typed measured/targeted sets, the quality-target fold, per-metric parsers
- Created: 2026-10-27
- Completed: 2026-10-27

## Decisions (user rulings, 2026-10-27)

| Question | Ruling |
|---|---|
| Disambiguate app-metrics vs quality metrics | `App` prefix on the app-metrics side (`AppMetrics` already exists; `MetricKey`/`MetricsCollector` were the unprefixed holdouts). "Metric" stays with the quality domain — "measurement" is taken by the act/result vocabulary (`QualityMeasurement`/`QualityMeasurer`, the 0.21.7 rename), and VMAF/PSNR/SSIM/VIF are metrics in every external vocabulary. |
| Identity module home | New leaf module `pyqenc/quality_metrics.py` — reuses the glossary term *quality metrics* verbatim; mirrors `app_metrics.py`. Not folded into `models.py`. |
| `FIXED_COMPARISON_STATS` as explicit targets? | No values, no targets — a **typed pair set** (stat-only typing rejected: it cannot narrow the metric dimension). A narrower, not a judging reference. |
| Full vs targeted at typing level | Explicit roles on every signature — code must not guess the expectation. Static-only enforcement accepted. Superseded same day (user follow-up, "make them more functional"): the NewType form is replaced by a **frozen `Mapping` value class** with owned operations — NewTypes would still mutate, and the operations belong to the type. |
| Map operators (`&`, `-`)? | Rejected: operators on a mapping-like type are ambiguous (`dict & set` is not a Python convention) and ungreppable (the no-ad-hoc-derivation audit rule keys on finding `narrowed_to(`). The set algebra those operators would express lives on the **pair sets** as plain `frozenset` arithmetic — free. Map operations are owned **methods**, each with a live consumer (census: `narrowed_to` ×5 sites, `covers` ×1, `worst_of` ×1). |
| Per-metric classes: carry vs compose | **Carry** — `MetricInfo` subclasses, static singletons; no parallel hierarchy. |
| Sequencing vs QualitySearch V4 (§81 record split) | Identity fold first; `QualityPoint.metrics` becomes `TargetedMetrics` mechanically now, the sentinel split stays in V4's spec. |
| Set role at rest | User-facing sidecars narrowed where possible; the merged-output sidecar keeps the full set (no other place for it once attempts are swept); underlying stores (attempts) full for re-judging. |

## The contract

```python
# pyqenc/quality_metrics.py — leaf: imports nothing from models/quality
class MetricType(Enum): ...          # .info -> the metric's static singleton
class StatisticType(Enum): ...
class MetricInfo: ...                # Phase B: + parse_log() on subclasses
class MetricStatKey: ...             # the pair — landed form, unchanged

class MetricMap(Mapping[MetricStatKey, float]):
    # Mapping mixin supplies get/items/keys/values/__contains__/__iter__/__len__
    # from three abstracts; __eq__ over the data dict (same role only —
    # cross-role falls back to identity), no mutation surface, no hash.
    def narrowed_to(self, pairs: Iterable[MetricStatKey]) -> TargetedMetrics: ...
    def covers(self, pairs: Iterable[MetricStatKey]) -> bool: ...
    @staticmethod
    def worst_of(maps: Iterable[TargetedMetrics]) -> TargetedMetrics: ...

class MeasuredMetrics(MetricMap): ...   # role subclasses: zero behavior —
class TargetedMetrics(MetricMap): ...   # signatures stay role-distinct

FIXED_COMPARISON_PAIRS: tuple[MetricStatKey, ...]   # every metric × {P10, MEDIAN}, canonical order

MeasuredMetricsYaml = Annotated[MeasuredMetrics, BeforeValidator(...), PlainSerializer(...)]
TargetedMetricsYaml = Annotated[TargetedMetrics, ...same validators...]
```

```python
# models.py
class QualityTarget(BaseModel):
    identity: MetricStatKey      # closed-set validation at construction
    value:    float
    @classmethod
    def parse(cls, text: str) -> "QualityTarget": ...   # delegates to MetricStatKey.parse
    def __str__(self) -> str: ...                       # "vmaf-min≥95" — unchanged

class EncodingPlan(BaseModel):
    def targeted(self, metrics: MeasuredMetrics) -> TargetedMetrics:
        """Search: the targets' identities. Fixed: FIXED_COMPARISON_PAIRS.
        Delegates to metrics.narrowed_to(...)."""
```

```python
# quality.py — scoring on the classes
def _score_attempt(metrics: TargetedMetrics, targets: list[QualityTarget]) -> float:
    for t in targets:
        actual = metrics[t.identity]             # no key reconstruction, no presence re-check dance
        info   = t.identity.metric.info          # deficit/passes/acceptance_delta
```

## Import graph (why the leaf module exists)

`quality.py` imports `models.py`; a pydantic field annotation must resolve at
class creation, so the deferred-import trick that today hides `MetricStatKey`
inside `QualityTarget.key()` cannot type the folded field. The identity
vocabulary therefore moves below both. Direction only — no cycles:

```
quality_metrics.py  ←  models.py  ←  quality.py  ←  phases
        ↑                                   ↑
        └───────── all phases, summary_tables ┘
```

`models.py` gains one import of a stdlib-only leaf (pydantic's
`BeforeValidator`/`PlainSerializer` come from its own dependency) — no new
package weight. Phase B keeps it that way: `parse_log` returns plain
per-frame floats, so pandas stays in the measurer and never enters the
`models.py` import chain (CLI startup unaffected).

## Migration map (every stringly site → its new home)

| Today | Becomes |
|---|---|
| `quality.py: MetricType/StatisticType/MetricInfo/MetricStatKey/MetricStatMap/flatten_metric_stats` | moved to `quality_metrics.py` (`flatten` dies in Phase B) |
| `QualityTarget.metric/.statistic: str` + `key()` accessor | `identity: MetricStatKey`; `key()` and the deferred imports die |
| `constants.FIXED_COMPARISON_STATS: tuple[str, …]` + `summary_tables.COMPARISON_STATS: tuple[StatisticType, …]` | one `FIXED_COMPARISON_PAIRS: tuple[MetricStatKey, …]` in `quality_metrics.py` |
| `_winner_targeted_metrics` (incl. the `rsplit` fixed branch) | `plan.targeted(metrics)` |
| pick-up `targets_set` + `issubset`/`get` probes (encoding.py:1172/1244/1249) | `sidecar.metrics.covers({t.identity for t in targets})` / `sidecar.metrics[t.identity]` |
| optimization `_aggregate_strategy_metrics` hand-rolled min-reduce | `MetricMap.worst_of(per_chunk_maps)` |
| `_score_attempt`/`find_worst_target`/`failed_targets`/`_compute_proportional_candidate` key builds (quality.py ×5) | `target.identity` |
| merge `_rendered_basis` (typed, hand-rolled) | `plan.targeted(measured)` |
| merge `_fmt_inline_metrics` / `_log_missed_targets_warning` lookups | targeted-map lookups by `t.identity`; dash form via the pair |
| `Targets:` kv lines ×3 (encoding:1942, optimization:432, merge:410) | `str(target)` (the 2026-10-26 bycatch) |
| optimization `_comparison_metrics` + anchor reconstruction (`QualityTarget(metric=…, statistic=…)`) | `narrow_to(anchor_metrics, FIXED_COMPARISON_PAIRS)` → `QualityTarget(identity=key, value=…)` |
| summary_tables stat-tuple filters/column assembly (`:274/:284/:79`) | pair-membership over `FIXED_COMPARISON_PAIRS` |
| `fmt_metric_summary` (log_format.py) | takes `TargetedMetrics`; renders `sidecar_key()` — bytes identical |
| `metrics.py` + `MetricKey` + `MetricsCollector`/`NoOp`/`Yaml` | `app_metrics.py` + `App`-prefixed names (~15 source, ~20 test import lines) |
| `QualityPoint.metrics: dict[str, float] | None` | `TargetedMetrics | None` (sentinel split stays with V4) |
| Phase B: `_parsers` dispatch, `parse_psnr/ssim/vmaf/vif_file`, `compute_statistics`, `_extract_key_stats`, `FullMetricStatistics`, `PerMetricStats` | four `MetricInfo` subclasses own `parse_log` → per-frame floats; measurer owns quantiles; the nested world and both vocabularies die |

## Narrowing semantics

A narrowing spec is a **set of pairs** — the common denominator. The two
sources: user targets (search) → `{t.identity for t in plan.targets}`; fixed
→ `FIXED_COMPARISON_PAIRS`. `narrowed_to` is the single filter body;
`plan.targeted` picks the spec and delegates. The merged-output sidecar's
rendered basis and the winner sidecar's subset are the same call —
mode-awareness lives only in the plan.

`FIXED_COMPARISON_PAIRS` is an ordered tuple (metric-major, stat rank within)
because the tables need a stable column order; narrowing tests membership
against the tuple's set view. Today it enumerates all four metrics ×
{P10, MEDIAN} — identical scope to the string tuple it replaces. Shrinking
the metric dimension later (the SSIM question) is an edit to this constant
alone (Req 4.3); which metrics get *measured* remains `measurement.metrics`'
job — no second scoping mechanism.

## Typing mechanics

- `MetricMap` is a `collections.abc.Mapping` subclass: the mixin derives
  `get`/`items`/`keys`/`values`/`__contains__`/`__iter__`/`__len__` from
  `__getitem__`/`__iter__`/`__len__` over an internal dict — the delegation
  cost that once argued against classes is three methods. Added by hand:
  `__eq__` over the data dict (same role only; cross-role returns
  `NotImplemented` — a full set is never equal to a targeted subset even
  content-wise). No `__hash__` (maps are never dict keys); no mutation
  surface — `Mapping` has no `__setitem__`, so immutability is structural,
  not by convention.
- Pydantic fields use the per-role `Annotated` aliases; the `BeforeValidator`
  parses string keys via `MetricStatKey.parse` and wraps into the role
  instance, the `PlainSerializer` unwraps to canonical `sidecar_key()` strings
  (both roles share the same dump — subset and full serialize identically).
  Because the annotation is an arbitrary class, the six models carrying map
  fields (attempt/winner/measure/merged sidecars, `MergedVideo`,
  `StrategyRow`) opt into `arbitrary_types_allowed`; validation is the
  isinstance our validator's output satisfies.
- `MetricStatKey` stays a frozen stdlib dataclass; pydantic v2 validates
  dataclass fields natively — `QualityTarget.identity` needs no adapter.
- `EncodingPlan.targeted` lives on the plan (the mode home): construction
  invariants already guarantee searched ⇒ targets present.

## Behavior preservation (behavioral verification — no pinning)

Tests observe behavior, never forms (agent-testing.md): round-trips,
tolerant loads, output tokens, error contracts — no yaml text, no dump
shapes, no rendered-byte diffs.

- Sidecars: save→load round-trip preserves values and pair identities;
  string-keyed loads yield typed maps; recovery behavior — a pre-change
  work directory reuses its attempts without re-measuring.
- Tables: the existing semantic suite stays green (row order, basis cues,
  delta signs, marks, missing-value caveats — its stated no-cosmetics
  policy); only the key plumbing underneath moves.
- Log lines: token-level assertions (attempt lines carry the
  `psnr_min=…`-form tokens, merge inline the dash form) — the existing
  assertion style, not whole-line equality.
- Measured values: differential equivalence across the rehoming on fixtures
  plus properties; edges named (PSNR inf, single-frame chunks, sampling
  factor).
- Phase A does not touch the parsing path; Phase B does not touch
  persistence forms. Each phase is independently gate-green.

## Phase split

- **Phase A — the fold.** Module move, set types, narrowing, target fold,
  consumer retarget, summary-table rekeying, app-metrics rename, glossary,
  docs, version.
- **Phase B — the parsers.** `parse_log` on the four subclasses, measurer
  rewire, nested-vocabulary deletion. Same PR, separate commit wave, gate
  between.
