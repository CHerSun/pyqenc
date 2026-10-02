# Implementation Plan — Fixed-Quality Mode

<!-- markdownlint-disable MD024 -->

- Created: 2026-10-03

## Overview

Staged config-layer-first so fixed runs become *constructible* before any phase
behavior changes: the CLI override + validation + derived mode land first (pure
additive, nothing consumes it yet), then the fixed-run phase behaviors
(invalidation, banner, guard), then the encoder seam, then optimization's
compared-run machinery, then encoding/merge presentation. Every task leaves all
three gates green and searched runs byte-identical. Requirement ids reference
`requirements.md`.

## Notes

- After each code task run `uv run ruff check .`, `uvx ty check`, and the
  relevant `uv run python -m pytest`.
- Searched-run invariance is the standing regression bar: without `-q`, or with
  every strategy ranged, logs and behavior must be byte-identical to today
  (design Correctness Property 1). The reuse-run on an existing work dir is part
  of every e2e check.
- E2E on real media only with the speed rule: `--strategies "h265*+ultrafast"`
  (never slow presets); fixed-run e2e adds `-q 18`.
- Keyword-only construction for every new model/dataclass field; no aliased
  duplicate imports; no test-only production code (the `measure_attempts` seam
  is live — both phase call sites pass it explicitly, Task 3).
- The off-path of `measure_attempts` (metrics-absence tolerance) is deliberately
  unwired — TODO §83 owns it. Do not add a CLI toggle for it in this spec.
- Separator tolerance for `-q` parsing is local; canonical serialized forms
  follow TODO §81's outcome when it lands (interim: serialize pairs with `:`).

## Tasks

- [x] 1. Config layer: `-q` override, validation, derived mode (Req 1, 2, 3, 4)
  - `EncodingConfig`: add `quality_range_override: tuple[Decimal, Decimal] | None
    = None` (CLI-only field, never in YAML; assignment triggers the existing
    resolved-cache invalidation) and a derived `fixed_quality: bool` property —
    `True` iff every resolved strategy has `quality_better == quality_worse`
  - CLI: `-q` / `--quality` in `_add_quality_arguments` (cli.py); `_build_config`
    parses single value → `[v, v]`, pair → unordered bounds (separators `:`,
    `-`, `..`), sets the override before the re-resolve at cli.py:334
  - `_effective_codec` (app_config.py): CLI override replaces the profile range
    (precedence CLI > profile > codec bounds); when the resulting range excludes
    `default_quality`, clamp it to the nearest bound and log the adjustment
    (Req 2.3)
  - Range validation at the shared site (extend `_validate_profile_quality_range`
    + the override sibling): subset-of-codec rule for the override; endpoint
    granularity alignment — every endpoint (single-point or pair, codec /
    profile / CLI layer) an exact multiple of every matched codec's
    `quality_granularity`, loud exit listing strategy, codec, granularity and
    the nearest aligned values (Req 2.4, incl. the h265+AV1 intersection case)
  - `_build_config` post-re-resolve loud exits: uniform `quality_label` check
    when `-q` supplied, listing per-strategy labels (Req 4); mixed-mode stop —
    some collapsed + some ranged (Req 3.2)
  - Tests (`tests/test_app_config_properties.py` + unit): all `-q` parse forms
    and separators; unordered normalization incl. VBR direction; subset and
    granularity errors (single-point, pair, multi-codec intersection, profile-
    and codec-declared ranges); label-mix exit; mixed-mode exit; `default_quality`
    auto-adjust for both profile narrowing and CLI override; `fixed_quality`
    derivation; searched configs unchanged

- [x] 2. Fixed-run phase entry: wipe, banner, cleanup guard (Req 5, 6, 7)
  - Identify the single always-executed point on OptimizationPhase that both the
    optimize path and the `_skip_check` all-strategies path pass through (the
    phase runs on every `auto`/`encode` invocation as encoding's mandatory dep);
    all three behaviors key on `config.encoding.fixed_quality` there
  - Cleanup guard first (Req 7): fixed + `job_result.cleanup >= INTERMEDIATE`
    → hard stop with the attempts-are-the-substrate message; searched runs never
    guarded
  - Unconditional winner-layer wipe (Req 6): reuse `_wipe_encoded_dir` on every
    fixed start; no value/mode persistence; recovery stays listing-only —
    winners re-derive from attempts during execution (verify the pair ledger
    flips to pending after the wipe on a previously-complete work dir)
  - Banner (Req 5): one prominent WARNING per fixed run — fixed mode, pinned
    label + value, per-chunk search disabled, nominal-knob size comparison is
    not cross-family-comparable, merged measurement remains; plus the
    every-survivor-fully-encodes heads-up for multi-strategy runs (Req 5.2);
    follows the banner/separator conventions (blank line + banner block)
  - Tests (`tests/unit/test_optimization_phase.py`): guard stops fixed+cleanup
    and passes fixed+NONE and all searched combos; wipe fires on both paths and
    never on searched runs; banner emitted once per fixed run with both
    strategy-count variants; no banner on searched runs

- [x] 3. `measure_attempts` seam on the shared encoder (Req 9.2, 9.3)
  - `ChunkEncoder`: add keyword-only `measure_attempts: bool = True` alongside
    the existing construction params; thread from `_make_encoder` call sites —
    optimization passes `measure_attempts=True` explicitly (Req 8.1), encoding
    passes its configured value (currently always True — the False wiring is
    §83)
  - The seam's contract pinned by tests but NOT exposed via CLI (see Notes);
    default-on behavior byte-identical today
  - Tests: constructor threading at both call sites; default True; a
    `measure_attempts=False` unit test documenting the seam (sidecar metric-keys
    requirement lifted — Req 2 of §83's future work stays out of scope)

- [x] 4. Compared-run optimization: pruning, anchor, synthetic set, table
  (Req 8)
  - Aggregation: per strategy, min-across-test-chunks for every measured
    `(metric, statistic)` + total size, derived from the test-encode results
    (in-memory winner payloads; same data the sidecars persist)
  - Dominance pruning replaces `_apply_tolerance` for fixed compared runs only:
    A dominates B iff `size(A) ≤ size(B)` and A ≥ B on every aggregated
    metric-stat; dominated strategies excluded; exact duplicates survive;
    survivors = `selected_strategies`; searched runs keep tolerance untouched
    (Req 8.5)
  - Anchor = smallest-size survivor, ties by resolved-strategy order (Req 8.2);
    synthetic target set = anchor's aggregated stats over all measured metrics
    (Req 8.3)
  - `OptimizationParams` (optimization.yaml schema): persist anchor identity,
    synthetic set (sorted) and survivors (Req 8.6); recovery reuses persisted
    state on re-run without re-encoding
  - Comparison table in the optimization summary log: per non-anchor strategy —
    size Δ% and headline metric deltas vs the anchor (normalized 0–100 scales;
    anchor row is the baseline), full stats in the sidecar (Req 8.4)
  - `OptimizationPhaseResult`: carry the synthetic set (typed field) for the
    encoding phase
  - Tests: h264-pruned-by-h265 case; av1-vs-h265 both survive; duplicate
    survival; anchor chosen after pruning incl. the dominated size-tie edge and
    order tie-break; min-across-chunks aggregation; tolerance NOT applied in
    fixed; persistence round-trip + reuse; single-strategy and `optimize:
    false` runs skip all of it (Req 8.7)

- [ ] 5. Fixed-mode encoding and merged presentation (Req 9.1, 9.4–9.7)
  - Verify + pin (tests first): fixed encoding performs exactly one accepted
    attempt per pair via the existing single-point degenerate path — no loop
    changes expected (Req 9.1)
  - Presentation targets: compared fixed runs judge winner sidecars /
    limiter-style output against the synthetic set from the optimization result
    instead of `resolved_targets`; uncompared fixed runs use no presentation
    targets (absolute values; the limiter table self-extinguishes on empty
    metrics/targets) (Req 9.4, 9.5); sidecar validation keys follow the same
    set; `targets_met` is presentation-only in fixed mode (Req 9.7)
  - MergePhase: suppress `_log_missed_targets_warning` on fixed runs (Req 9.6);
    anchor-relative summary deltas are optional polish — implement only if
    cheap
  - Tests: compared-run sidecar/presentation targets; uncompared absolute
    display; merged warning suppressed on fixed, present on searched; searched
    encoding paths byte-identical (incl. reuse-run)

- [ ] 6. Docs + e2e closeout
  - docs: fixed-quality section (usage: `-q` forms; compared vs uncompared
    behavior; banner meaning; cleanup guard rationale; iterative q-change
    workflow incl. attempts preservation); demote the duplicated-profile
    workaround from recommended to legacy-equivalent (still valid — fixed-ness
    is derived)
  - E2E on real media (`--strategies "h265*+ultrafast"`, `-q 18`):
    single-strategy uncompared run + reuse-run; two-strategy compared run
    (pruning + anchor table + survivors both merged); q-change re-run on the
    same work dir (wipe → re-derivation from attempts, old attempts unwanted
    but intact); cleanup-guard stop check; one searched control run for
    byte-identical logs
  - Final three-gate pass (`uv run ruff check .`, `uvx ty check`,
    `uv run python -m pytest`)
