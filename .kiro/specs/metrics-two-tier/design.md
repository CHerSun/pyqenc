# Design Document: Two-Tier Metrics System

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-15

## Overview

The current `TimeKey` StrEnum maps phase events to dotted string values (e.g.
`"encoding.main"`) that are used as wall-clock measures — a semantic mismatch.
This design replaces that flat model with a two-tier key structure:

- **Top-level keys** (no dot, e.g. `"encoding"`) — true wall-clock elapsed time
  per phase, percentages relative to the grand total.
- **Dotted keys** (one or more dots, e.g. `"encoding.h265"`) — per-process run
  time for sub-actions, percentages relative to the sibling prefix total.

The distinction is implicit in the key string itself — no extra flag or nested
dict is needed. The `MetricsStore` is a flat `dict[str, float]` whose keys
follow this convention. The `YamlMetricsCollector` serialises it into a
structured `time_distribution` section with `top_level` and `dotted`
subsections.

The feature also introduces `Strategy.metric_name` and
`BaseStrategy.metric_short` properties that sanitize ASCII dots in strategy
names before they are used as metric key suffixes, preventing corruption of the
dot-based prefix structure.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Phase call sites  (single import: pyqenc.metrics)              │
│  collector.time(MetricKey.ENCODING)                  ← top     │
│  collector.time(MetricKey.JOB, "probe")              ← fixed   │
│  collector.time(MetricKey.ENCODING, strategy.name)   ← dynamic │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  MetricsCollector Protocol                                      │
│  time(key: MetricKey, *parts: str) → AbstractContextManager    │
│  step(key: MetricKey, *parts: str, convergence_update=None)    │
│  flush() → None                                                 │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  YamlMetricsCollector                                           │
│  _store: MetricsStore  (dict[str, float])                       │
│  _conv_accumulators: dict[str, _ConvergenceAccumulator]         │
│                                                                 │
│  _build_metrics() → PipelineMetrics                             │
│    ├─ _compute_top_level(store) → list[TopLevelEntry]           │
│    └─ _compute_dotted(store)   → dict[str, DottedGroup]         │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  metrics.yaml                                                   │
│  pipeline_metrics:                                              │
│    time_distribution:                                           │
│      total_seconds / total_duration                             │
│      top_level: [...]                                           │
│      dotted: { prefix: { prefix_seconds, breakdown: [...] } }  │
│    convergence: [...]                                           │
└─────────────────────────────────────────────────────────────────┘
```

The `NoOpMetricsCollector` accepts `MetricKey, *parts` keys and discards all
data, unchanged in contract.

## Components and Interfaces

### `MetricKey` StrEnum

Replaces `TimeKey`. Eight members, all flat top-level keys (no dot):

```python
class MetricKey(StrEnum):
    JOB          = "job"
    EXTRACTION   = "extraction"
    CHUNKING     = "chunking"
    AUDIO        = "audio"
    ENCODING     = "encoding"
    OPTIMIZATION = "optimization"
    MERGE        = "merge"
    RECOVERY     = "recovery"
```

`TimeKey` is removed entirely. All call sites are migrated per the conversion
table in Requirement 6.2.

### `dotted()` helper function (`pyqenc/metrics.py`)

There is no standalone `dotted()` helper. Instead, `time()` and `step()` on
the `MetricsCollector` accept a variadic key signature — a `MetricKey` prefix
followed by zero or more string suffix parts. The collector joins them with
`"."` internally:

```python
# top-level key — no suffix
collector.time(MetricKey.ENCODING)

# fixed dotted key — one suffix literal
collector.time(MetricKey.JOB, "probe")

# dynamic dotted key — sanitized strategy name as suffix
collector.time(MetricKey.ENCODING, strategy.metric_name)

# deeper key if ever needed — multiple suffixes
collector.time(MetricKey.ENCODING, "opt", strategy.metric_name)
```

The key joining is internal mechanics — call sites only import `MetricKey`.
No `dotted()` function, no string concatenation, no separate constants.

### `MetricsStore` type alias

```python
MetricsStore = dict[str, float]
```

A flat dict whose keys follow the two-tier naming convention. No subclassing —
just a type alias used in annotations. Key validation (top-level vs dotted) is
enforced at the point of insertion in `YamlMetricsCollector._record()`.

### `MetricsCollector` Protocol (updated)

```python
class MetricsCollector(Protocol):
    def time(self, key: MetricKey, *parts: str) -> AbstractContextManager[None]: ...
    def step(self, key: MetricKey, *parts: str,
             convergence_update: ConvergenceUpdate | None = None) -> None: ...
    def flush(self) -> None: ...
```

`time()` and `step()` accept a `MetricKey` prefix followed by zero or more
string suffix parts. The implementation joins them with `"."` to form the
storage key. A single `MetricKey` with no parts is a top-level key; with one
or more parts it becomes a dotted key. Both sync and async context manager
protocols are supported (existing `__aenter__`/`__aexit__` on `_TimingContext`).

### `YamlMetricsCollector` (updated)

Key changes from the current implementation:

| Aspect | Before | After |
|---|---|---|
| Internal store | `dict[TimeKey, float]` pre-seeded for all keys | `dict[str, float]` sparse, keys added on first use |
| Key type | `TimeKey` only | `MetricKey` + optional `*parts: str` |
| `_build_metrics()` | flat `TimeDistribution` with single `breakdown` list | two-tier `TimeDistribution` with `top_level` + `dotted` |
| Resume (`_try_resume`) | reads `breakdown` list, maps back to `TimeKey` | reads `top_level` and `dotted` sections, restores `_store` |

Constructor signature:

```python
def __init__(
    self,
    work_dir:   Path,
    force_wipe: bool = False,
) -> None:
```

Note: when `parallelism > 1`, dotted key times will exceed their corresponding
top-level times — dotted keys accumulate the sum of all parallel process times
while top-level keys measure wall-clock elapsed time. This is expected and by
design. Parallelism is a runtime setting outside the scope of this spec.

### Suffix sanitization (enforced on `Strategy` and `BaseStrategy`)

ASCII dots are sanitized at the source — on the strategy objects themselves —
so that `strategy.name` and `strategy.strategy_short` are guaranteed to never
contain an ASCII dot (U+002E) regardless of how they are used (metric keys,
filenames, logs). This is enforced at construction time via Pydantic field
validators on `Strategy` and in `BaseStrategy.__init__`:

```python
# pyqenc/models.py — Strategy (Pydantic model)
@field_validator("preset", "profile", mode="before")
@classmethod
def _sanitize_dots(cls, v: str) -> str:
    return v.replace(".", TIME_SEPARATOR_MS)
```

```python
# pyqenc/phases/audio.py — BaseStrategy
def __init__(self, name: str, strategy_short: str) -> None:
    self.name           = name.replace(".", TIME_SEPARATOR_MS)
    self.strategy_short = strategy_short.replace(".", TIME_SEPARATOR_MS)
```

`time()` then simply joins parts using `DOTTED_KEY_SEPARATOR` — no
sanitization needed there, because the guarantee is already upheld by the
strategy objects:

```python
def _build_key(key: MetricKey, *parts: str) -> str:
    return DOTTED_KEY_SEPARATOR.join((key, *parts))
```

Spaces in `strategy_short` (e.g. `"2.0 std"` → `"2․0 std"` after dot
replacement) are left as-is — they are valid in YAML keys and do not corrupt
the dot-based prefix structure.

### Percentage calculation helpers (module-level, `pyqenc/metrics.py`)

`DOTTED_KEY_SEPARATOR = "."` is defined as a named constant in
`pyqenc/constants.py` alongside the other separator constants. All metric key
joining, splitting, and membership checks use it — no literal `"."` appears in
the metrics key machinery:

```python
# pyqenc/constants.py
DOTTED_KEY_SEPARATOR: str = "."
"""Separator used to join MetricKey prefix and suffix parts into a dotted
metric key (e.g. ``"encoding.h265"``). Distinct from file extension dots and
other uses of ``"."`` in the codebase."""
```

```python
# pyqenc/metrics.py
def _is_top_level(key: str) -> bool:
    """Return True when key contains no metric key separator."""
    return DOTTED_KEY_SEPARATOR not in key

def _last_dot_prefix(key: str) -> str:
    """Return everything to the left of the last metric key separator."""
    return key.rsplit(DOTTED_KEY_SEPARATOR, 1)[0]

def _build_key(key: MetricKey, *parts: str) -> str:
    """Join MetricKey prefix and suffix parts into a dotted storage key."""
    return DOTTED_KEY_SEPARATOR.join((key, *parts))

def _compute_top_level_entries(
    store: MetricsStore,
) -> tuple[int, list[TopLevelEntry]]:
    """Build sorted top-level entries and grand total from store."""
    ...

def _compute_dotted_groups(
    store: MetricsStore,
) -> dict[str, DottedGroup]:
    """Build prefix-keyed dotted groups from store."""
    ...
```

Other uses of `"."` in the codebase (file extension parsing in
`encoding.py`, `visualization.py`) are unrelated to metric key structure and
are left unchanged — they use `"."` as a filename component separator, a
different semantic.

## Data Models

### Updated Pydantic models

The existing `TimeEntry`, `TimeDistribution`, and `PipelineMetrics` are
replaced with new models that reflect the two-tier structure:

```python
class TopLevelEntry(BaseModel):
    key:      str   # e.g. "encoding"
    seconds:  int
    duration: str   # "[Dd ]HH:MM:SS"
    percent:  str   # "X.X%"

class DottedEntry(BaseModel):
    key:      str   # e.g. "encoding.h265"
    seconds:  int
    duration: str
    percent:  str   # relative to prefix total

class DottedGroup(BaseModel):
    prefix_seconds:  int
    prefix_duration: str
    breakdown:       list[DottedEntry]  # sorted descending, zeros omitted

class TimeDistribution(BaseModel):
    total_seconds:  int
    total_duration: str
    top_level:      list[TopLevelEntry]    # sorted descending, zeros omitted
    dotted:         dict[str, DottedGroup] # keyed by prefix string

class PipelineMetrics(BaseModel):
    time_distribution: TimeDistribution
    convergence:       list[ConvergenceStats] | None = None
```

`parallelism` is written to `metrics.yaml` as a separate top-level field by
the pipeline orchestrator when it constructs the YAML document — it is not part
of `PipelineMetrics` and not tracked by the collector.

### `MetricsStore` key rules (enforced at insertion)

| Key type | Rule | Example |
|---|---|---|
| Top-level | No ASCII dot | `"encoding"`, `"merge"` |
| Dotted | One or more ASCII dots; prefix = everything left of last dot | `"encoding.h265"`, `"merge.concat"` |

A top-level key and a dotted key sharing the same last-dot prefix may coexist
(e.g. `"encoding"` and `"encoding.h265"` are independent entries).

### YAML output structure

```yaml
pipeline_metrics:
  time_distribution:
    total_seconds: 3847
    total_duration: "01:04:07"
    top_level:
      - key: encoding
        seconds: 2941
        duration: "00:49:01"
        percent: "76.5%"
      - key: optimization
        seconds: 496
        duration: "00:08:16"
        percent: "12.9%"
      # zeros omitted, sorted descending
    dotted:
      encoding:
        prefix_seconds: 5882
        prefix_duration: "01:38:02"
        breakdown:
          - key: encoding.h265
            seconds: 4120
            duration: "01:08:40"
            percent: "70.1%"
          - key: encoding.slow+h265
            seconds: 1762
            duration: "00:29:22"
            percent: "29.9%"
      merge:
        prefix_seconds: 820
        prefix_duration: "00:13:40"
        breakdown:
          - key: merge.concat
            seconds: 410
            duration: "00:06:50"
            percent: "50.0%"
          - key: merge.quality_measure
            seconds: 410
            duration: "00:06:50"
            percent: "50.0%"
  convergence:
    - strategy: slow+h265
      chunks: 42
      attempts:
        total: 189
        min: 2
        avg: 4.5
        max: 9
        stddev: 1.3
```

### Resume protocol

On `_try_resume`, the collector reads `top_level` entries and all `dotted`
breakdown entries, restoring each key's float seconds into `_store`. The
`parallelism` field is ignored on resume — the value passed at construction
time is authoritative.

### Phase call-site mapping (post-migration)

| Phase | Key used | Type |
|---|---|---|
| JobPhase — probe | `MetricKey.JOB` | top-level |
| JobPhase — crop detect | `MetricKey.JOB` | top-level (same key, accumulates) |
| JobPhase — recovery | `MetricKey.RECOVERY` | top-level |
| ExtractionPhase — mkvextract | `MetricKey.EXTRACTION` | top-level |
| ExtractionPhase — recovery | `MetricKey.RECOVERY` | top-level |
| ChunkingPhase — scene detect | `MetricKey.CHUNKING` | top-level |
| ChunkingPhase — split | `MetricKey.CHUNKING` | top-level (same key, accumulates) |
| ChunkingPhase — recovery | `MetricKey.RECOVERY` | top-level |
| AudioPhase — per-strategy | `MetricKey.AUDIO` | top-level |
| AudioPhase — recovery | `MetricKey.RECOVERY` | top-level |
| EncodingPhase — main loop | `MetricKey.ENCODING` | top-level |
| EncodingPhase — recovery | `MetricKey.RECOVERY` | top-level |
| OptimizationPhase — test encodes | `MetricKey.OPTIMIZATION` | top-level |
| OptimizationPhase — recovery | `MetricKey.RECOVERY` | top-level |
| MergePhase — concat | `MetricKey.MERGE` | top-level |
| MergePhase — quality measure | `MetricKey.MERGE` | top-level (same key, accumulates) |
| MergePhase — recovery | `MetricKey.RECOVERY` | top-level |

Dotted keys added by phases in the new design:

| Phase | Call expression | Resulting key |
|---|---|---|
| JobPhase | `collector.time(MetricKey.JOB, "probe")` | `"job.probe"` |
| JobPhase | `collector.time(MetricKey.JOB, "crop_detect")` | `"job.crop_detect"` |
| ChunkingPhase | `collector.time(MetricKey.CHUNKING, "scene_detect")` | `"chunking.scene_detect"` |
| ChunkingPhase | `collector.time(MetricKey.CHUNKING, "split")` | `"chunking.split"` |
| AudioPhase | `collector.time(MetricKey.AUDIO, strategy.strategy_short)` | `"audio.norm"` etc. |
| EncodingPhase | `collector.time(MetricKey.ENCODING, strategy.name)` | `"encoding.slow+h265"` etc. |
| OptimizationPhase | `collector.time(MetricKey.OPTIMIZATION, strategy.name)` | `"optimization.h265"` etc. |
| MergePhase | `collector.time(MetricKey.MERGE, "concat")` | `"merge.concat"` |
| MergePhase | `collector.time(MetricKey.MERGE, "quality_measure")` | `"merge.quality_measure"` |

The key joining is internal to the collector — call sites only import
`MetricKey`. No `dotted()` function, no string constants, no hardcoded
prefixes. Arbitrary depth is supported by passing multiple suffix parts.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what the
system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

### Property 1: Prefix extraction is last-dot

*For any* dotted key string (containing at least one ASCII dot), the prefix
derived by the collector SHALL equal everything to the left of the last dot
separator, regardless of how many dots the key contains.

**Validates: Requirements 1.4**

### Property 2: Time accumulation is additive

*For any* key (top-level or dotted) and any sequence of non-negative elapsed
durations, the accumulated value in `_store` after all durations are recorded
SHALL equal the sum of those durations (within floating-point tolerance).

**Validates: Requirements 2.2, 3.4**

### Property 3: Top-level percentages sum to 100%

*For any* `MetricsStore` state where at least one top-level key has a non-zero
value, the sum of all top-level percentage values in the serialised
`time_distribution.top_level` list SHALL equal `100.0%` (within floating-point
rounding to one decimal place).

**Validates: Requirements 4.1, 4.4**

### Property 4: Dotted percentages sum to 100% per prefix

*For any* `MetricsStore` state and *for any* last-dot prefix `P` that has at
least one dotted key with a non-zero value, the sum of all percentage values in
the corresponding `dotted[P].breakdown` list SHALL equal `100.0%` (within
floating-point rounding to one decimal place).

**Validates: Requirements 4.2, 4.5**

### Property 5: YAML serialisation round-trip preserves all values

*For any* `PipelineMetrics` value, serialising to YAML and deserialising back
SHALL produce an equivalent object — all `top_level` entries, all `dotted`
groups and their `breakdown` entries, and `convergence` data are preserved
exactly.

**Validates: Requirements 5.1, 5.6**

### Property 6: Resume restores accumulated store

*For any* `MetricsStore` state, flushing to `metrics.yaml` and then
constructing a new `YamlMetricsCollector` pointing at the same `work_dir`
(without `force_wipe`) SHALL restore the same accumulated float seconds for
every key that was present in the original store (within integer-rounding
tolerance, since YAML persists integer seconds).

**Validates: Requirements 5.6**

### Property 7: Strategy dot sanitization produces valid metric keys

*For any* strategy name or `strategy_short` value (possibly containing ASCII
dots), the sanitized suffix produced by `metric_name` / `metric_short` SHALL
contain no ASCII dot (U+002E), and the resulting dotted key SHALL group
correctly with sibling keys under the expected prefix when the last-dot prefix
algorithm is applied.

**Validates: Requirements 9.1, 9.5**

### Property 8: Top-level list is sorted descending with no zeros

*For any* `MetricsStore` state, the `top_level` list in the serialised YAML
SHALL be sorted in descending order of `seconds` and SHALL contain no entries
with `seconds = 0`.

**Validates: Requirements 5.2**

### Property 9: Dotted breakdown lists are sorted descending with no zeros

*For any* `MetricsStore` state and *for any* prefix group in the serialised
`dotted` section, the `breakdown` list SHALL be sorted in descending order of
`seconds` and SHALL contain no entries with `seconds = 0`.

**Validates: Requirements 5.3**

**Property reflection:** Properties 3 and 4 are distinct (different
denominators — grand total vs prefix total) and cannot be merged. Properties 8
and 9 are analogous but apply to different tiers; they can be tested with a
single parametrised property but are kept separate for clarity. Properties 5
and 6 are complementary: Property 5 tests the Pydantic model round-trip
(structure fidelity), Property 6 tests the collector's resume logic (value
restoration from integer-rounded YAML). No redundancy.

## Error Handling

| Scenario | Behaviour |
|---|---|
| `flush()` write failure | Log `WARNING`, do not raise; next flush retries |
| `_try_resume` parse failure | Log `WARNING`, start fresh (zero store) |
| Unknown key in persisted YAML on resume | Log `DEBUG`, skip entry |
| ASCII dot in strategy name used as suffix | Silently replaced with `TIME_SEPARATOR_MS`; no warning |
| Grand total is zero (all top-level values are 0) | All top-level percentages reported as `"0.0%"` |
| Prefix total is zero (all sibling dotted values are 0) | All sibling percentages reported as `"0.0%"` |
| `time()` context exits with exception | Elapsed is still recorded; exception is re-raised |
| Concurrent `time()` contexts for the same key | Each context tracks its own `t0`; all contribute to the same accumulator (correct for parallel use) |

## Testing Strategy

### Unit tests (`tests/test_metrics_integration.py`)

The existing integration tests use `TimeKey` and must be migrated to
`MetricKey`. Each test class and assertion referencing `TimeKey.X` is updated
to the corresponding `MetricKey.Y` per the conversion table in Requirement 6.2.
New tests are added for:

- `MetricKey` has exactly 8 members with correct string values (smoke).
- `time()` accepts `MetricKey` with zero or more suffix parts.
- `step()` accepts `MetricKey` with zero or more suffix parts.
- `NoOpMetricsCollector` accepts both without error.
- `Strategy` construction with dots in `preset`/`profile` produces a `name` with no ASCII dots.
- `BaseStrategy` construction with dots in `strategy_short` produces no ASCII dots.
- Top-level and dotted keys coexist in the same store (e.g. `"encoding"` and
  `"encoding.h265"` both present after two `time()` calls).
- YAML `dotted` section is absent when no dotted keys have non-zero values.

### Property-based tests (`tests/test_metrics_properties.py`)

Uses **Hypothesis** (already approved, already used in the test suite).
Each property test runs a minimum of **100 iterations**.

Tag format: `# Feature: metrics-two-tier, Property N: <property_text>`

**Property 1 — Prefix extraction is last-dot**
Generate random dotted key strings (1–3 dots, random segments). Verify
`_last_dot_prefix(key) == key.rsplit(".", 1)[0]`.

**Property 2 — Time accumulation is additive**
Generate a random key (top-level or dotted) and a list of non-negative floats.
Inject them directly into `_store`. Verify `_store[key] == sum(durations)`.

**Property 3 — Top-level percentages sum to 100%**
Generate a random `dict[str, float]` of top-level keys (no dots) with at least
one non-zero value. Inject into a `YamlMetricsCollector._store`. Call
`flush()`. Parse YAML. Sum `float(e["percent"].rstrip("%"))` for all
`top_level` entries. Assert `abs(total - 100.0) < 0.15`.

**Property 4 — Dotted percentages sum to 100% per prefix**
Generate random groups of sibling dotted keys (same prefix, random suffixes,
at least one non-zero). Inject into `_store`. Call `flush()`. Parse YAML.
For each prefix group, sum percentages. Assert `abs(total - 100.0) < 0.15`.

**Property 5 — YAML serialisation round-trip**
Generate random `PipelineMetrics` instances (using Hypothesis composite
strategies for `TopLevelEntry`, `DottedEntry`, `DottedGroup`,
`TimeDistribution`). Serialise to YAML string. Deserialise. Assert structural
equality of all fields.

**Property 6 — Resume restores accumulated store**
Generate a random `MetricsStore` (mix of top-level and dotted keys, non-zero
values). Inject into a `YamlMetricsCollector`. Call `flush()`. Construct a
second `YamlMetricsCollector` with the same `work_dir`. For each key in the
original store, assert `abs(resumed._store[key] - int(round(original))) <= 1`
(integer-rounding tolerance from YAML persistence).

**Property 7 — Strategy dot sanitization produces valid metric keys**
Generate random strings (possibly containing ASCII dots) as `preset`/`profile`
values. Construct a `Strategy` from them. Assert `strategy.name` contains no
ASCII dot (U+002E). Pass `strategy.name` as a suffix to `_build_key`. Apply
`_last_dot_prefix` to the result. Assert prefix equals `MetricKey.ENCODING`.
Repeat analogously for `BaseStrategy.strategy_short`.

**Property 8 — Top-level list sorted descending, no zeros**
Generate random top-level value dicts. Inject into `_store`. Call `flush()`.
Parse YAML `top_level` list. Assert `seconds` values are non-increasing and all
`> 0`.

**Property 9 — Dotted breakdown sorted descending, no zeros**
Generate random dotted value dicts (multiple prefix groups). Inject into
`_store`. Call `flush()`. Parse YAML `dotted` section. For each prefix group,
assert `breakdown` `seconds` values are non-increasing and all `> 0`.

### Migration regression

After migrating all call sites, run the full existing test suite
(`uv run python -m pytest`) to confirm no regressions. The integration tests
serve as the primary regression guard for phase-level `MetricKey` usage.
