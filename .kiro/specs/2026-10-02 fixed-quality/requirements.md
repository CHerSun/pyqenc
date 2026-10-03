# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: Fixed-Quality Mode — first-class pinned-knob encoding via a CLI quality override
- Created: 2026-10-02
- Completed: 2026-10-03

## Cross-Spec Notes

### What this spec supersedes

| Superseded | Where | What changed |
|---|---|---|
| The duplicated-profile workaround for fixed-CRF runs (a per-value profile with `quality_range: [v, v]`) | usage practice | Replaced by the `-q/--quality` CLI override (Req 1). A collapsed config profile remains valid and equivalent — fixed-ness is derived, never declared — but is no longer the ergonomic path, and its per-chunk measurement cost is now an explicit control (Req 9). |
| "fixed mode must force `optimize: false` / single strategy" (working assumption mid-design) | design discussion 2026-10-02 | Superseded within the same discussion: optimization remains legitimate in fixed mode — anchor-relative presentation + Pareto dominance pruning (Req 8). Only *mixed* runs are stopped (Req 3). |
| Optimization's implicit "quality targets were met, so sizes are comparable" assumption | `2026-09-09 artifact-state-refactor` lineage / optimization phase | In fixed mode that assumption is void by construction; Req 8 replaces blind size selection with dominance pruning and never claims quality parity across strategies. |

### Related, not superseded

- `2026-09-28 artifact-model` — recovery/ledger mechanics this spec builds on unchanged: presence-based completeness, `wanted` derivation, listing-only recovery classification, phase-params sidecars.
- `2026-05-02 quality-search-v3` — search behavior in searched runs is untouched; fixed runs simply present the search a single-point domain (its existing degenerate behavior — one attempt, immediate exhaustion).
- TODO §81 (separator tolerance; one canonical serialized form) — `-q` bound parsing adopts the same tolerant-input policy; canonical serialization aligns with whatever §81 lands.
- TODO §82 (multi-metric scoring research) — the anchor + dominance design is v1; research may refine presentation and pruning later. No composite score is introduced meanwhile.
- TODO §83 (metrics-absence tolerance) — created by this spec; the prerequisite for ever defaulting per-chunk measurements off. This spec adds only the control (Req 9.3) and does not flip any default.
- TODO §68 (config-change invalidation of encoding results) — explicitly **not** relied upon (Req 6.4).

## Introduction

pyqenc's pipeline assumes the user wants per-scene quality pursuit: search the
encoder's quality knob per chunk until metric targets pass, then optimize across
strategies under the "targets met ⇒ sizes comparable" assumption. In practice a
second operating mode is at least as common: pin the knob (CRF/CQ/QP) to a known
value and encode, using pyqenc for chunking, resumability and measurement — not for
search. Today that mode exists only as a workaround: duplicate a profile with a
collapsed `quality_range`, and accept its costs — a config mutation per value
(config is meant to be tuned once; per-run variation belongs at the CLI), full
per-attempt measurement the run cannot use for decisions, and an optimization
phase whose size comparison becomes unsound the moment fixed and searched
strategies mix.

This spec makes pinned-knob runs first-class:

1. **One CLI lever** — `-q/--quality` as a quality-range override applied to every
   matched strategy (single value = fixed; pair = constrained search).
2. **A derived run mode** — fixed iff every resolved strategy's effective range is
   a single point; mixed runs stop loudly. No mode is ever declared or persisted.
3. **Honest fixed-mode optimization** — Pareto dominance pruning removes
   strictly-worse strategies; the smallest *survivor* becomes the measurement
   anchor against which the rest are shown as deltas; every survivor is encoded
   (opinion-free — no composite score, no auto-pick).
4. **Safe iteration** — the winner layer is invalidated wholesale at every
   fixed-mode start and re-derived from attempts, so "try CRF 18, look, adjust,
   re-run" costs only the new encodes; a cleanup guard hard-stops configurations
   that would destroy the re-derivation substrate.

## Glossary

- **Quality knob** — the codec's quality parameter rendered through `{quality}` in
  `encoder_args` (CRF for x264/x265/SVT-AV1, CQ/QP for NVENC variants, Mbit/s for
  the VBR-labeled codec). Type is cosmetic: each codec declares `quality_label`,
  range, and granularity; mechanics are label-agnostic.
- **Effective range** — a strategy's quality domain: the codec's `quality_range`
  narrowed by the profile's `quality_range`, overridden by the CLI `-q` value.
  Stored `(better, worse)` in codec direction (reversed for VBR-style codecs).
- **Fixed run** — every resolved strategy's effective range is a single point
  (`quality_better == quality_worse`). **Searched run** — every resolved strategy
  has a real range. **Mixed** — anything else; always a loud stop.
- **Anchor** — in fixed-mode optimization, the survivor of dominance pruning with
  the smallest total test-chunk encode size. Its measured metric statistics
  become the run's ruler — pruning is the election; the anchor is only its
  baseline.
- **Synthetic target set** — the anchor's measured `(metric, statistic) → value`
  map, aggregated across test chunks. Used for *relative presentation only* —
  never as search goals, pass/fail gates, or selection thresholds.
- **Dominance** — strategy A dominates B iff `size(A) ≤ size(B)` and A is ≥ B on
  every measured metric-statistic. Dominated strategies are excluded; survivors
  form the Pareto front.
- **Winner layer** — the `encoded/` directory (promoted winning attempts, one slot
  per chunk × strategy). Distinct from the **attempt workspace** (`encoding/`,
  crf-embedded filenames, the re-derivation substrate).

## Requirements

### Requirement 1 — CLI quality override lever

**User Story:** As a user, I want to pin or narrow the quality knob for a run from
the command line, so that per-run variation never requires editing the stable
config or duplicating profiles.

#### Acceptance Criteria

1. THE CLI SHALL provide `-q` / `--quality` on every subcommand that receives
   quality/encoding arguments, accepting either one decimal value or a pair of
   decimal values.
2. A single value `v` SHALL set the override range to `[v, v]` (fixed). A pair
   `a, b` SHALL be normalized to `(better, worse)` per codec direction — input
   order is free; the codec's own direction convention decides meaning. A pair
   with equal values after normalization is fixed (identical to the single form).
3. The pair separator SHALL accept at least `:`, `-` and `..` (tolerant input per
   the §81 principle); any form serialized *by* pyqenc SHALL use the single
   canonical form §81 lands on.
4. The override SHALL apply uniformly to every strategy resolved from the
   effective strategies list (after `--strategies` overrides), replacing any
   profile-level `quality_range`. Precedence: CLI > profile > codec bounds.
5. When `-q` is absent, config behavior SHALL be byte-identical to today.

### Requirement 2 — Override validation lives at the range-validation site

**User Story:** As a developer, I want all range/granularity rules in one place,
so that CLI overrides and config profiles are validated by the same code and the
same errors.

#### Acceptance Criteria

1. Override validation SHALL extend the existing quality-range validation path
   (`_validate_profile_quality_range` and its CLI-override sibling), not live as a
   separate general validator.
2. The override range SHALL be a subset of every matched codec's range (the
   existing narrowing-only rule, evaluated per strategy).
3. The codec's `default_quality` (the search's starting point) SHALL remain
   inside the effective range: whenever an override or profile narrowing
   excludes it, the effective codec SHALL auto-adjust it to the nearest range
   bound, with a log line recording the adjustment. No loud exit — the starting
   point is a hint the search refines, never a user assertion.
4. Every effective-range endpoint — single-point or pair, whichever layer
   supplied it (codec, profile, CLI override) — SHALL be an exact multiple of
   every matched codec's `quality_granularity` (across codecs: the
   intersection, i.e. the coarsest step wins — h265 0.5 + AV1 1.0 ⇒ integers
   only). Violations SHALL exit loudly, listing strategy, codec, granularity
   and the nearest aligned values (e.g. `-q 18.5` with an integer-step codec;
   `-q 22.3:28.7` at 0.5 steps). Rationale: the search treats range boundaries
   as sentinel candidates and can attempt an endpoint value verbatim, so a
   misaligned endpoint violates the encoder-args "already quantized" contract
   — integer-step encoders would receive fractional values. Endpoint alignment
   is a search-correctness requirement, not a formatting preference.
5. Criteria 3–4 SHALL apply retroactively to profile-declared `quality_range`
   values, closing the existing validation gaps for config profiles — including
   the pre-existing case of a narrowed profile excluding `default_quality`.

### Requirement 3 — Derived run mode; mixed runs stop

**User Story:** As a user, I want the run's mode to be a fact about my resolved
strategies rather than another switch, so that config-collapsed profiles and `-q`
are the same thing through one door.

#### Acceptance Criteria

1. Run mode SHALL be derived once, after strategy resolution: **fixed** iff every
   resolved strategy has `quality_better == quality_worse`; **searched** iff every
   resolved strategy has a real range.
2. A mixed set (some collapsed, some ranged) SHALL stop loudly before any phase
   runs, explaining the all-fixed-or-all-searched rule and listing the offending
   strategies. This is the replacement for the old fixed/searched optimization
   conflict.
3. No mode flag SHALL be persisted, threaded as a first-class config field, or
   accepted from config — mode is always re-derived from the resolved strategies.

### Requirement 4 — Uniform quality-label check

**User Story:** As a user sharing one `-q` value across strategies, I want to be
told before any work that my strategies use different knob types, so that a
shared number is never silently reinterpreted per codec.

#### Acceptance Criteria

1. When `-q` is supplied, all matched strategies SHALL share the same
   `quality_label` (string equality). A violation SHALL exit loudly, listing each
   offending strategy with its label.
2. The check SHALL run after CLI overrides and strategy re-resolution in
   `_build_config`, before any phase executes.
3. Without `-q`, mixed labels remain legal in searched runs: the search operates
   per strategy over its own range — codec args with a templated `{quality}` are
   all it consumes — so the knob type never crosses strategy boundaries, and
   every winner independently meets the same metric bar.

### Requirement 5 — Fixed-mode banner and strategy-count heads-up

**User Story:** As a user, I want an unmissable declaration of what a fixed run
does and does not guarantee, so that I never mistake nominal-knob parity for
quality parity.

#### Acceptance Criteria

1. Every fixed run SHALL emit one prominent WARNING banner at OptimizationPhase
   start (both the optimize and the skip/all-strategies paths), stating: fixed
   mode active; knob pinned (label + value); per-chunk search disabled; size
   comparison is at *nominally* equal knob and knob scales are NOT comparable
   across encoder families (h264 CRF ≠ h265 CRF ≠ AV1 CRF); merged-output
   measurement remains as the final check.
2. Fixed runs with multiple surviving strategies SHALL additionally state that
   every surviving strategy will fully encode the video.
3. Searched runs SHALL NOT emit the banner (logs unchanged).

### Requirement 6 — Winner-layer invalidation at fixed start

**User Story:** As a user iterating on the knob value, I want each fixed run to
start from a clean winner layer while keeping every attempt, so that re-runs are
cheap and correct without any q-persistence bookkeeping.

#### Acceptance Criteria

1. On every fixed-mode start, OptimizationPhase — the single invalidation point
   for `encoded/` and a mandatory dependency of encoding, executing on the
   all-strategies path as well — SHALL unconditionally delete the winner layer
   (reusing `_wipe_encoded_dir`).
2. No persistence of the fixed value or mode SHALL be introduced for
   invalidation purposes; recovery classification SHALL remain listing-only (no
   attempt/winner sidecar reads; phase-params sidecars remain readable as today).
3. Winners SHALL be re-derived from the attempt workspace during execution
   (crf-embedded filenames; presence-based completeness).
4. This requirement SHALL NOT depend on TODO §68 (config-change invalidation),
   which may never be implemented.
5. Attempts produced under previous values SHALL be preserved as unwanted
   artifacts — never deleted by mode machinery (cleanup obeys Req 7).

### Requirement 7 — Cleanup guard (hard stop)

**User Story:** As a user on a multi-day fixed encode, I want protection from
configurations that would destroy my resumability substrate, so that an
interrupted run can always be continued without re-encoding completed work.

#### Acceptance Criteria

1. A fixed run with cleanup level ≥ INTERMEDIATE SHALL hard-stop before encoding
   begins, with a message explaining that attempts are the re-derivation
   substrate for fixed re-runs and cleanup deletes them (winners alone cannot
   re-derive after a value change or interruption).
2. The guard SHALL apply only to fixed runs; searched runs are unchanged.
3. The guard SHALL respect the inclusive CleanupLevel ordering
   (INTERMEDIATE ⊂ ALL).

### Requirement 8 — Fixed-mode optimization: anchor ruler + dominance pruning

**User Story:** As a user comparing strategies at a pinned knob, I want an honest
relative comparison and the removal of strictly-worse options — without the tool
pretending a single number can rank quality for me.

#### Acceptance Criteria

1. Optimization test-chunk measurement SHALL stay enabled in fixed mode
   regardless of the encoding-phase measurement control (the anchor needs data;
   cost is bounded by the test-chunk sample).
2. The **anchor** SHALL be chosen from the survivors of dominance pruning
   (criterion 5) as the strategy with the smallest total test-encode size; ties
   broken deterministically by resolved-strategy order. The anchor is by
   construction a member of the Pareto front (nothing dominates the size
   minimum) and is the only front member selectable without a quality opinion.
3. The **synthetic target set** SHALL be the anchor's measured metrics,
   aggregated per `(metric, statistic)` as the minimum across test chunks,
   covering every measured metric (not just configured targets). It feeds the
   existing relative-scoring machinery (`find_worst_target`, deficits, limiter
   rendering) for presentation only.
4. The comparison presentation SHALL show, per non-anchor strategy: size delta %
   vs the anchor and metric deltas on a compact headline set (all metrics
   normalized 0–100); full detail in logs/sidecars. The anchor row is the
   baseline ("+0.8 vmaf-med, +0.6 vif-med, +31% size" style).
5. Selection SHALL be Pareto dominance pruning only: dominance per the glossary,
   evaluated over **all** measured metric-statistics (conservative — more
   metrics, fewer prunes); dominated strategies are excluded; every survivor is
   selected and encoded. No composite score SHALL be computed; no auto-pick
   among the front SHALL be made; the size-tolerance rule SHALL NOT apply in
   fixed mode (it presumes the voided quality-parity assumption).
6. `optimization.yaml` SHALL persist the anchor identity, the synthetic set and
   the survivor list (phase-params sidecar).
7. Uncompared fixed runs — single strategy, or `optimize: false` — SHALL skip
   anchor machinery entirely (the existing skip paths perform no test-chunk
   comparison); no synthetic set is produced.

### Requirement 9 — Fixed-mode encoding and presentation

**User Story:** As a user running my dominant workflow (one strategy, one pinned
value), I want the encode loop to do exactly one attempt per chunk and keep the
existing mechanics, so that resumability is untouched and measurement cost is an
explicit control rather than an accident.

#### Acceptance Criteria

1. Fixed-mode encoding SHALL perform exactly one attempt per (chunk, strategy)
   at the pinned value and accept it unconditionally (the single-point domain's
   existing degenerate behavior: first candidate taken, search space exhausted).
   Promotion, hard-linking and sidecar mechanics are unchanged.
2. A `measure_attempts` control SHALL exist on the shared chunk-encoding
   machinery, supplied externally by the calling phase. Its default SHALL be
   *measure* (current behavior). This spec introduces the control only;
   default-flipping and metrics-absence tolerance are deferred to TODO §83.
3. Optimization SHALL always measure its test chunks (Req 8.1), independently of
   this control.
4. In compared fixed runs (optimization enabled, multiple strategies),
   winner-sidecar metrics and limiter-style presentation SHALL be judged
   against the anchor synthetic set (relative deltas), not config targets.
   Config targets SHALL NOT drive any encoding decision or verdict in fixed
   mode.
5. Uncompared fixed runs (single strategy or `optimize: false`) SHALL display
   absolute measured values with no verdicts — no ruler exists without an
   optimization comparison.
6. The merged phase SHALL display measured metrics and SHALL suppress the
   config-target missed-targets warning in fixed mode (search-tuned targets would
   read as all-miss noise); anchor-relative deltas MAY appear in the summary
   when a synthetic set exists.
7. No acceptance-critical decision in fixed mode SHALL depend on a
   `targets_met`-style verdict; verdict fields are presentation-only in fixed
   mode.

### Requirement 10 — Explicitly deferred

**User Story:** As a maintainer, I want the non-goals on record, so that this
spec stays reviewable and follow-ups have a home.

#### Acceptance Criteria

1. Per-chunk measurement skipping (default flip) and metrics-absence tolerance →
   TODO §83; this spec ships the control with default = measure.
2. Multi-metric scoring refinements (composites, grain-aware/temporal metrics,
   BD-rate ideas) → TODO §82; v1 is anchor + dominance.
3. Config-change invalidation of encoding results (§68) → not relied upon, not
   implemented here.
4. Quality-target profiles (per-target-set profiles) → future; the anchor
   derives from the measured set, so it composes.
5. V4 QualitySearch rework → separate effort, untouched here.
6. Constant-bitrate / 2-pass encoding → explicitly not a goal. VBR-labeled
   codecs work mechanically through their own knob (the override is
   label-agnostic); no special cases are added for them.
