# Requirements Document: Two-Tier Metrics System

<!-- markdownlint-disable MD024 -->

- Created: 2026-06-15

## Introduction

The pipeline already records wall-clock time for each phase category in `metrics.yaml`
via `TimeKey` (e.g. `encoding.main`, `merge.concat`). These were used as wall-clock
measures despite having dotted string values — a mismatch this spec corrects.

This feature extends the model with dotted keys: per-process run times for specific
sub-actions within a phase (e.g. `encoding.h265`, `encoding.slow+h265`). Top-level
keys are guaranteed to be wall-clock elapsed time. Dotted keys are not guaranteed to
be wall-clock: with `parallelism=1` they approximate wall-clock time; with
`parallelism > 1` they exceed it (sum of all parallel process times).

The distinction is encoded implicitly in the key structure — no extra flag or nested
dict is needed:

- **Top-level keys** (no dot separator, e.g. `encoding`, `merge`) — wall-clock elapsed
  time. Their `%` is calculated from the grand total of all top-level values.
- **Dotted keys** (one or more dot separators, e.g. `encoding.h265`,
  `encoding.slow+h265`) — process run time, grouped under a parent prefix.
  Their `%` is calculated from the sum of all sibling keys sharing the same
  last-dot prefix (e.g. all keys whose prefix is `"encoding"` sum to give the
  `encoding` dotted total).

The algorithm is generic: it always derives the prefix as everything to the left of
the last dot separator, regardless of how many dots a key contains. Current planned
usage is two levels (e.g. `encoding.h265`, `merge.concat`, `optimization.h265`), but
deeper keys are valid and handled correctly by the same logic.

This replaces the current `TimeKey` StrEnum approach with a flat `dict[str, float]`
whose keys follow the dotted naming convention above.

## Glossary

- **Prefix**: Everything to the left of the last dot separator in a key, e.g.
  `"encoding"` in `"encoding.h265"`. The algorithm always uses the last-dot prefix
  to group siblings, regardless of how many dots the key contains.
- **Sibling keys**: All dotted keys sharing the same last-dot prefix, e.g.
  `"encoding.h265"` and `"encoding.slow+h265"` are siblings under `"encoding"`.
- **MetricsStore**: The flat `dict[str, float]` that holds all accumulated timing
  values, keyed by metric name strings following the dotted naming convention.
- **MetricsCollector**: The existing Protocol in `pyqenc/metrics.py` that phases use
  to record timing. Extended in this spec to accept a `MetricKey` prefix followed by
  zero or more string suffix parts — the collector joins them with `"."` internally
  to form the storage key.
- **TimeKey**: The existing `StrEnum` in `pyqenc/metrics.py` that is being replaced by
  this spec. Its enum members (e.g. `TimeKey.ENCODING_MAIN`) mapped to dotted string
  values (e.g. `"encoding.main"`) that were used as wall-clock measures.
  Under this spec, `TimeKey` is removed and replaced by a new flat `MetricKey` StrEnum
  (or equivalent) whose members map to top-level keys (e.g. `"encoding"`).
  The old dotted string values are repurposed as dotted metric keys — the strings
  themselves are reused, but the enum members are remapped to new top-level keys.
- **MetricKey**: The new `StrEnum` (or equivalent named-constant set) that replaces
  `TimeKey`. Its values are flat top-level keys (e.g. `"job"`, `"encoding"`,
  `"merge"`). Multiple old `TimeKey` members that shared a phase prefix are merged into
  a single `MetricKey` member (e.g. `JOB_PROBE` and `JOB_CROP_DETECT` both become
  `MetricKey.JOB = "job"`). The 8 members are: `JOB`, `EXTRACTION`, `CHUNKING`,
  `AUDIO`, `ENCODING`, `OPTIMIZATION`, `MERGE`, `RECOVERY`.
- **Strategy name**: The display name of an encoding strategy, using `+` as separator
  (e.g. `"h265"`, `"slow+h265"`). Guaranteed to contain no ASCII dot — sanitized at
  construction time on `Strategy` and `BaseStrategy`. Safe to use directly as a suffix
  in `collector.time()` calls, filenames, and logs without per-call-site handling.
- **Grand total**: The sum of all top-level metric values. Used as the
  denominator when computing top-level metric percentages.
- **Prefix total**: The sum of all dotted metric values sharing the same prefix.
  Used as the denominator when computing dotted metric percentages.

## Requirements

### Requirement 1: Key Naming Convention

**User Story:** As a developer, I want a clear, implicit convention for distinguishing
top-level (wall-clock) from dotted (process-sum) metrics, so that I can read the
YAML output and immediately understand what each number means without extra flags.

#### Acceptance Criteria

1. THE MetricsStore SHALL use plain `str` keys with the following structure:
   - Top-level keys contain no dot separator (e.g. `"encoding"`, `"merge"`, `"audio"`).
   - Dotted keys contain one or more dot separators (e.g. `"encoding.h265"`,
     `"encoding.slow+h265"`, `"merge.concat"`, `"optimization.h265"`).
2. THE MetricsCollector SHALL treat top-level keys as wall-clock metrics and dotted
   keys as process-time metrics — no additional flag or field is required to distinguish
   them.
3. THE MetricsStore SHALL allow a top-level key and a dotted key sharing the same
   last-dot prefix to coexist (e.g. `"encoding"` and `"encoding.h265"` may both be
   present).
4. THE MetricsCollector SHALL derive the prefix of any dotted key as everything to
   the left of the last dot separator, regardless of how many dots the key contains.

### Requirement 2: Top-Level Keys (Wall-Clock)

**User Story:** As a developer, I want top-level keys to capture true wall-clock time
for each logical phase, so that I can see how long each phase actually took from the
user's perspective.

#### Acceptance Criteria

1. THE MetricsCollector SHALL record wall-clock elapsed time (via `time.monotonic()`)
   for top-level keys using the existing `time(key)` context manager interface,
   extended to accept `str` keys in addition to `MetricKey` enum values.
2. THE MetricsCollector SHALL accumulate elapsed seconds per top-level key across
   multiple calls (e.g. if `"encoding"` is timed twice, the values sum).
3. WHEN computing percentages for top-level keys, THE MetricsCollector SHALL use the
   grand total (sum of all top-level values) as the denominator.
4. IF the grand total is zero, THE MetricsCollector SHALL report `"0.0%"` for all
   top-level keys rather than dividing by zero.
5. THE following top-level keys SHALL be defined as named constants and used
   by the respective phases:
   - `"job"` — JobPhase wall-clock (probe + optional crop detect)
   - `"extraction"` — ExtractionPhase wall-clock
   - `"chunking"` — ChunkingPhase wall-clock
   - `"audio"` — AudioPhase wall-clock
   - `"encoding"` — EncodingPhase wall-clock (main chunk encoding only)
   - `"optimization"` — OptimizationPhase wall-clock (test encodes for strategy selection)
   - `"merge"` — MergePhase wall-clock
   - `"recovery"` — accumulates wall-clock time across ALL phase `_recover()` calls
     (one per phase), merged into a single top-level key; this allows distinguishing
     time spent on actual work vs. time spent on recovery/reuse detection.
     No dotted keys exist under the `"recovery"` prefix — recovery is measured only
     at the top level.

### Requirement 3: Dotted Keys (Process Time)

**User Story:** As a developer, I want dotted keys to capture per-process run
times for specific sub-actions, so that I can see how parallel work is distributed
and compare strategy-level costs even when parallelism > 1.

#### Acceptance Criteria

1. THE MetricsCollector SHALL record process run time for dotted keys using the
   same `time(key)` context manager interface as top-level keys.
2. WHEN `parallelism = 1`, THE dotted metric value for a key SHALL equal the
   corresponding top-level metric value (within measurement tolerance), because
   process time equals wall-clock time when there is no parallelism.
3. WHEN `parallelism > 1`, THE dotted metric value for a key MAY exceed the
   corresponding top-level metric value, because dotted time is the sum of all
   parallel process times.
4. THE MetricsCollector SHALL accumulate elapsed seconds per dotted key across
   multiple calls (e.g. `"encoding.h265"` accumulates across all chunks encoded with
   the `h265` strategy).
5. WHEN computing percentages for dotted keys, THE MetricsCollector SHALL use the
   last-dot prefix total (sum of all sibling keys sharing the same last-dot prefix)
   as the denominator.
6. IF the prefix total is zero, THE MetricsCollector SHALL report `"0.0%"` for all
   keys under that prefix rather than dividing by zero.
7. THE following dotted keys SHALL be used by the respective phases (current planned
   usage is two levels, but the algorithm supports arbitrary depth):
   - `"job.probe"` — ffprobe metadata probing in JobPhase
   - `"job.crop_detect"` — crop detection scan in JobPhase
   - `"chunking.scene_detect"` — PySceneDetect analysis in ChunkingPhase
   - `"chunking.split"` — FFV1/remux chunk splitting in ChunkingPhase
   - `"audio.<strategy_short>"` — per-strategy audio process time in AudioPhase,
     using `BaseStrategy.strategy_short` as the suffix (e.g. `"audio.norm"`,
     `"audio.dynaudnorm"`, `"audio.aac"`, `"audio.5․1"` with sanitized dot);
     the `"audio"` prefix group works exactly like `"encoding"` — per-strategy
     breakdown, siblings summing to the `"audio"` top-level total.
     Note: `strategy_short` values may contain spaces (e.g. `"2.0 std"`) — spaces
     are left as-is (valid in YAML keys and do not corrupt the dot-based prefix
     structure). Dots in `strategy_short` (e.g. `"5.1"`) are sanitized per
     Requirement 9 (e.g. `"5.1"` → `"audio.5․1"`).
   - `"encoding.<strategy>"` — per-strategy encoding process time in EncodingPhase
     (e.g. `"encoding.h265"`, `"encoding.slow+h265"`)
   - `"optimization.<strategy>"` — per-strategy optimization test encode process time
     in OptimizationPhase (e.g. `"optimization.h265"`, `"optimization.slow+h265"`);
     the `"optimization"` prefix group works exactly like `"encoding"` — per-strategy
     breakdown, siblings summing to the `"optimization"` top-level total
   - `"merge.concat"` — mkvmerge/ffmpeg concatenation time in MergePhase
   - `"merge.quality_measure"` — VMAF/PSNR measurement time in MergePhase
   - ExtractionPhase records only the top-level `"extraction"` key — no dotted keys
     exist under the `"extraction"` prefix (single mkvextract operation, no
     sub-strategy split).

### Requirement 4: Percentage Calculation

**User Story:** As a developer, I want each metric's percentage to be calculated
relative to the correct denominator, so that the numbers are meaningful and
comparable within their tier.

#### Acceptance Criteria

1. FOR ALL top-level keys, THE MetricsCollector SHALL compute percentage as:
   `(key_seconds / grand_total_seconds) * 100`, where `grand_total_seconds` is the
   sum of all top-level key values.
2. FOR ALL dotted keys sharing last-dot prefix `P`, THE MetricsCollector SHALL compute
   percentage as: `(key_seconds / prefix_total_seconds) * 100`, where
   `prefix_total_seconds` is the sum of all keys whose last-dot prefix equals `P`.
3. THE MetricsCollector SHALL format all percentages as `"X.X%"` (one decimal place).
4. FOR ALL valid MetricsStore states, the sum of all top-level percentages SHALL equal
   `100.0%` (within floating-point rounding to one decimal place).
5. FOR ALL valid MetricsStore states and for each last-dot prefix `P` that has at
   least one dotted key, the sum of all percentages for keys with prefix `P` SHALL
   equal `100.0%` (within floating-point rounding to one decimal place).

### Requirement 5: MetricsStore Serialisation to YAML

**User Story:** As a developer, I want the two-tier structure to be clearly visible in
`metrics.yaml`, so that I can read top-level and dotted metrics at a glance and diff
them across runs.

#### Acceptance Criteria

1. THE `metrics.yaml` file SHALL include a `time_distribution` section structured as:

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
         - key: merge
           seconds: 410
           duration: "00:06:50"
           percent: "10.7%"
         # ... all top-level keys, sorted descending by seconds, zeros omitted
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
         optimization:
           prefix_seconds: 992
           prefix_duration: "00:16:32"
           breakdown:
             - key: optimization.h265
               seconds: 620
               duration: "00:10:20"
               percent: "62.5%"
             - key: optimization.slow+h265
               seconds: 372
               duration: "00:06:12"
               percent: "37.5%"
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
         # ... one entry per prefix that has at least one second-level key
   ```

2. THE `top_level` list SHALL be sorted in descending order of `seconds`; entries with
   `seconds = 0` SHALL be omitted.
3. WITHIN each prefix group in `dotted`, the `breakdown` list SHALL be sorted in
   descending order of `seconds`; entries with `seconds = 0` SHALL be omitted.
4. IF a prefix has no second-level keys with non-zero seconds, THE MetricsCollector
   SHALL omit that prefix from the `dotted` section entirely.
5. THE MetricsCollector SHALL write `metrics.yaml` atomically using the `.tmp`-then-rename
   protocol, consistent with the existing implementation.
6. THE MetricsCollector SHALL resume from a persisted `metrics.yaml` on restart,
   restoring all accumulated top-level and second-level values from the `top_level` and
   `dotted` sections.

### Requirement 6: Migration from TimeKey

**User Story:** As a developer, I want all existing `TimeKey` usages replaced with a
new flat `MetricKey` enum, so that call sites use the correct top-level keys and the
codebase has no legacy enum members pointing at the wrong tier.

#### Acceptance Criteria

1. THE `TimeKey` StrEnum SHALL be removed and replaced by a new `MetricKey` StrEnum
   (or equivalent named-constant set) whose values are flat top-level keys
   with no dot separator.
2. THE following conversion table defines the full migration from old `TimeKey` members
   to new `MetricKey` members; multiple old members that share a phase prefix are
   merged into a single new member:

   | Old `TimeKey` member   | Old string value          | New `MetricKey` member | New string value   |
   |------------------------|---------------------------|------------------------|--------------------|
   | `JOB_PROBE`            | `"job.probe"`             | `JOB`                  | `"job"`            |
   | `JOB_CROP_DETECT`      | `"job.crop_detect"`       | `JOB`                  | `"job"`            |
   | `EXTRACTION`           | `"extraction.mkvextract"` | `EXTRACTION`           | `"extraction"`     |
   | `CHUNKING_SCENE_DETECT`| `"chunking.scene_detect"` | `CHUNKING`             | `"chunking"`       |
   | `CHUNKING_SPLIT`       | `"chunking.split"`        | `CHUNKING`             | `"chunking"`       |
   | `AUDIO`                | `"audio.processing"`      | `AUDIO`                | `"audio"`          |
   | `ENCODING_MAIN`        | `"encoding.main"`         | `ENCODING`             | `"encoding"`       |
   | `ENCODING_OPTIMIZATION`| `"encoding.optimization"` | `OPTIMIZATION`         | `"optimization"`   |
   | `MERGE_CONCAT`         | `"merge.concat"`          | `MERGE`                | `"merge"`          |
   | `MERGE_QUALITY_MEASURE`| `"merge.quality_measure"` | `MERGE`                | `"merge"`          |
   | `RECOVERY`             | `"recovery"`              | `RECOVERY`             | `"recovery"`       |

   The new `MetricKey` enum therefore has 8 members: `JOB`, `EXTRACTION`, `CHUNKING`,
   `AUDIO`, `ENCODING`, `OPTIMIZATION`, `MERGE`, `RECOVERY`.

3. THE old dotted string values for `JOB_PROBE`, `JOB_CROP_DETECT`, `CHUNKING_SCENE_DETECT`,
   `CHUNKING_SPLIT`, `ENCODING_MAIN`, `ENCODING_OPTIMIZATION`, `MERGE_CONCAT`, and
   `MERGE_QUALITY_MEASURE` (e.g. `"job.probe"`, `"encoding.main"`) SHALL be repurposed
   as dotted metric keys (see Requirement 3, criterion 7) — the string values themselves
   are reused, but the enum members are remapped to new top-level keys.
   The old `EXTRACTION` string value `"extraction.mkvextract"` is NOT repurposed as a
   dotted key — it is simply dropped, as ExtractionPhase has no dotted breakdown.
   The old `AUDIO` string value `"audio.processing"` is NOT repurposed — it is replaced
   by per-strategy `"audio.<strategy_short>"` keys (see Requirement 3, criterion 7).
4. ALL call sites that previously used `TimeKey` members SHALL be updated to use the
   corresponding `MetricKey` members; no call site SHALL reference `TimeKey` after
   migration.
5. THE `NoOpMetricsCollector` SHALL accept `MetricKey` values and plain `str` keys
   without error, consistent with its existing no-op contract.
6. THE `step(key, convergence_update)` method SHALL accept `MetricKey` and `str` keys
   with the same behaviour as `time()`.

### Requirement 7: No New External Dependencies

**User Story:** As a developer, I want the two-tier metrics extension to use only
already-approved packages and Python stdlib, so that the dependency footprint does
not grow.

#### Acceptance Criteria

1. THE MetricsCollector extension SHALL use only Python stdlib modules and
   already-approved packages (`pydantic`, `pyyaml`).
2. THE MetricsCollector extension SHALL NOT introduce any new pip dependencies.
3. THE MetricsCollector extension SHALL NOT call `psutil` or any external process to
   measure time — only `time.monotonic()` is used.

### Requirement 8: Strategy Name Dot Sanitization

**User Story:** As a developer, I want ASCII dots in strategy names to be replaced
with a look-alike character at the strategy level, so that `strategy.name` and
`strategy.strategy_short` are guaranteed safe for any context that uses `.` as a
separator — metric keys, filenames, logs — without requiring per-call-site handling.

#### Acceptance Criteria

1. THE `Strategy` Pydantic model SHALL sanitize ASCII dots (U+002E) in `preset` and
   `profile` fields at construction time (via field validator), replacing them with
   `TIME_SEPARATOR_MS` (U+2024, ONE DOT LEADER, `"․"`), so that `strategy.name`
   (which is `f"{preset}+{profile}"`) is guaranteed to contain no ASCII dot.
2. THE `BaseStrategy.__init__` SHALL sanitize ASCII dots in both `name` and
   `strategy_short` at construction time using the same replacement, so that
   `strategy.strategy_short` is guaranteed to contain no ASCII dot.
3. THE replacement SHALL use the existing `TIME_SEPARATOR_MS` constant from
   `pyqenc/constants.py`, not a hardcoded character literal.
4. THE sanitization SHALL be silent — no warning or error is raised when a dot is
   found.
5. Spaces in `strategy_short` (e.g. `"2.0 std"`) are NOT replaced — only ASCII dots
   are sanitized.
6. THE `time()` collector method SHALL join key parts with `"."` without any
   additional sanitization — the guarantee is upheld by the strategy objects.
7. A property-based test SHALL verify that constructing a `Strategy` or
   `BaseStrategy` with a name containing ASCII dots produces a `name` /
   `strategy_short` with no ASCII dots, and that using it as a suffix in
   `collector.time()` produces a key that groups correctly under the expected prefix.
