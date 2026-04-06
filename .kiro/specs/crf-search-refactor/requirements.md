# Requirements: CRF Search Refactor

<!-- markdownlint-disable MD024 -->

- Created: 2026-04-06
- Completed:

> **Note (2026-04-06 — pre-implementation changes to superseded code):**
> Before this refactor was implemented, the following changes were made to `CRFHistory` / `adjust_crf` and `CodecConfig`:
> - `CodecConfig.quality_range` order preserved as-is; `quality_better`, `quality_worse`, `quality_higher_is_better` properties added.
> - `CodecConfig.quality_max_step` field added.
> - `CRFHistory.add()` made unconditional; fields renamed to `pass_crf` (better boundary) / `fail_crf` (worse boundary).
> - `adjust_crf` made direction-agnostic; `quality_max_step` parameter added.
> - `_load_history_from_sidecars` removed — recovery handled by artifact-check-per-step in the encoding loop.
>
> These changes are superseded by this refactor. The direction-agnostic concepts and `quality_max_step` carry forward.

## Introduction

The current CRF (quality) search implementation in `pyqenc/quality.py` is split across two loosely coupled objects: `CRFHistory` (a passive dataclass) and `adjust_crf()` (a standalone function that reads `CRFHistory` internals and returns the next CRF to try). The caller in `encoding.py` additionally maintains `best_failing_attempt`, `best_failing_crf`, and `best_failing_metrics` as separate local variables alongside the history object.

This refactor introduces a unified `CRFSearchProtocol` that encapsulates the full search state — bracket tracking, best-result tracking, and next-CRF computation — behind a single `record()` call. The existing algorithm is preserved as `CRFSearch` (implementing the protocol). A new `CRFSearchV2` implementing a 3-point sweet-spot search algorithm is added and becomes the default. The encoding caller is simplified to use the protocol interface exclusively.

## Glossary

- **Quality parameter**: The encoder-specific quality control value passed to the video encoder. Referred to generically as "quality" or "CRF" in code for brevity; the actual parameter name and direction depends on the codec (CRF for x264/x265/SVT-AV1 where lower = better quality; CQ for NVENC where lower = better quality; QP for some hardware encoders). The search algorithms treat it uniformly as a `Decimal` value with a defined `[min, max]` range and granularity.
- **CRFSearchProtocol**: The `Protocol` (structural interface) that all CRF search implementations must satisfy.
- **CRFSearch**: The refactored implementation of the existing binary-bracket search algorithm, implementing `CRFSearchProtocol`.
- **CRFSearchV2**: The new 3-point sweet-spot search algorithm, implementing `CRFSearchProtocol`.
- **Granularity**: The minimum step size between CRF values, expressed as a `Decimal` (e.g. `Decimal("0.5")` for CRF, `Decimal("1")` for QP).
- **Pass**: An encoding attempt where all quality targets are met.
- **Fail**: An encoding attempt where at least one quality target is not met.
- **Best-fail**: The failing attempt with the highest composite quality score (closest to meeting all targets), as computed by `_score_failing_attempt()`.
- **Bracket**: The `[pass_crf, fail_crf]` interval being searched.
- **Sentinel**: An initial boundary value (codec min or max) used before a real pass or fail has been observed.
- **Sidecar**: A YAML file stored alongside an encoded chunk file containing its measured metrics and CRF value.
- **ChunkEncoder**: The class in `pyqenc/phases/encoding.py` that drives the per-chunk encoding loop.
- **quality_targets**: A list of `QualityTarget` objects specifying the metric, statistic, and threshold that must be met.
- **_score_failing_attempt()**: Existing function in `quality.py` that computes a composite score for a failing attempt (sum of normalized deficits for failing targets only; higher = closer to passing).

---

## Requirements

### Requirement 1: CRFSearchProtocol Interface

**User Story:** As a developer, I want a well-defined protocol for CRF search objects, so that search algorithms are interchangeable and the encoding loop depends only on the interface.

#### Acceptance Criteria

1. THE `CRFSearchProtocol` SHALL be defined as a `typing.Protocol` (or ABC) in `pyqenc/quality.py`.
2. THE `CRFSearchProtocol` SHALL declare an `attempts` property returning `int` — the total number of encoding attempts recorded so far.
3. THE `CRFSearchProtocol` SHALL declare a `record(crf, quality_results) -> Decimal | None` method that records one attempt result, updates internal state, and returns the next CRF to try or `None` when the search is exhausted or the current result is accepted. `quality_targets`, `granularity`, `quality_better`, `quality_worse`, and `quality_max_step` SHALL be supplied at construction time, not per-call.
4. THE `CRFSearchProtocol` SHALL declare a `best_crf` property returning `Decimal | None` — the best CRF found so far (highest passing CRF if any pass exists, otherwise the best-fail CRF).
5. THE `CRFSearchProtocol` SHALL declare a `best_metrics` property returning `dict[str, float] | None` — the full metrics dict associated with `best_crf`.
6. THE `CRFSearchProtocol` SHALL declare a `best_targets_met` property returning `bool` — `True` if and only if `best_crf` corresponds to a passing attempt.
7. WHEN `record()` has never been called, THE `CRFSearchProtocol` implementation SHALL return `None` for `best_crf` and `best_metrics`, and `False` for `best_targets_met`.

---

### Requirement 2: CRFSearch — Preserved Legacy Algorithm

**User Story:** As a developer, I want the existing CRF search logic preserved as `CRFSearch`, so that existing tests remain valid and the old algorithm is available as a named alternative.

#### Acceptance Criteria

1. THE `CRFSearch` class SHALL implement `CRFSearchProtocol`.
2. THE `CRFSearch` class SHALL encapsulate the state currently held by `CRFHistory` (`fail_crf`, `pass_crf`, `fail_metrics`, `pass_metrics`, `attempts`) and SHALL be initialised with `quality_better`, `quality_worse`, `quality_targets`, `granularity`, and optional `quality_max_step`.
3. WHEN `CRFSearch.record()` is called, THE `CRFSearch` SHALL update its internal bracket state and return the same next-CRF value that the standalone `adjust_crf()` function would return for identical inputs.
4. THE `adjust_crf()` standalone function SHALL be retained in `pyqenc/quality.py` with its existing signature `adjust_crf(current_crf, quality_results, quality_targets, history, granularity) -> Decimal | None`, accepting a `CRFHistory` dataclass instance.
5. THE `CRFHistory` dataclass SHALL be retained in `pyqenc/quality.py` for use by `adjust_crf()`.
6. WHEN `CRFSearch.record()` returns `None` (exhausted or accepted), THE `CRFSearch.best_crf` SHALL equal the highest passing CRF observed, or the best-fail CRF if no pass was ever found.
7. THE `CRFSearch.best_targets_met` SHALL be `True` if and only if at least one passing attempt was recorded.

---

### Requirement 3: CRFSearchV2 — 3-Point Sweet-Spot Algorithm

**User Story:** As a developer, I want a new `CRFSearchV2` algorithm that tracks a 3-point state (pass / best-fail / outer-fail), so that the search can exploit quality score information to converge faster on the sweet spot.

#### Acceptance Criteria

1. THE `CRFSearchV2` class SHALL implement `CRFSearchProtocol`.
2. THE `CRFSearchV2` SHALL be initialised with `quality_better`, `quality_worse` (the codec's quality range as `Decimal` values), `quality_targets`, `granularity`, and optional `quality_max_step`.
3. WHEN `CRFSearchV2` is initialised, THE `CRFSearchV2` SHALL set internal state: `pass_crf = crf_min` (sentinel), `best_fail_crf = crf_max` (sentinel), `fail_crf = crf_max` (sentinel), with `best_fail_crf == fail_crf` indicating 2-point mode until a real fail is observed.
4. WHEN `record()` is called with a passing result, THE `CRFSearchV2` SHALL update `pass_crf = current_crf` and continue searching the `[pass_crf ... best_fail_crf]` range using proportional interpolation (same as `CRFSearch`).
5. WHEN `record()` is called with a failing result whose score (per `_score_failing_attempt()`) is greater than the current `best_fail` score AND the attempt was drawn from range B `[best_fail_crf ... fail_crf]`, THE `CRFSearchV2` SHALL promote `pass_crf = old best_fail_crf`, set `best_fail_crf = current_crf`, and keep `fail_crf` unchanged.
6. WHEN `record()` is called with a failing result whose score is greater than the current `best_fail` score AND the attempt was drawn from range A `[pass_crf ... best_fail_crf]`, THE `CRFSearchV2` SHALL set `fail_crf = old best_fail_crf`, set `best_fail_crf = current_crf`, and keep `pass_crf` unchanged.
7. WHEN `record()` is called with a failing result whose score is less than or equal to the current `best_fail` score AND the attempt was drawn from range B, THE `CRFSearchV2` SHALL tighten `fail_crf = current_crf`.
8. WHEN `record()` is called with a failing result whose score is less than or equal to the current `best_fail` score AND the attempt was drawn from range A, THE `CRFSearchV2` SHALL tighten `pass_crf = current_crf`.
9. WHEN computing the next CRF, THE `CRFSearchV2` SHALL select the midpoint of the larger of the two ranges `[pass_crf ... best_fail_crf]` and `[best_fail_crf ... fail_crf]`.
10. WHEN both `[pass_crf ... best_fail_crf]` and `[best_fail_crf ... fail_crf]` are ≤ `granularity`, THE `CRFSearchV2.record()` SHALL return `None` and accept `best_fail_crf` as the final result.
11. WHEN `CRFSearchV2.record()` returns `None` due to exhaustion, THE `CRFSearchV2.best_crf` SHALL equal the highest passing CRF if any pass was found, otherwise `best_fail_crf`.
12. THE `CRFSearchV2.best_targets_met` SHALL be `True` if and only if at least one passing attempt was recorded.

---

### Requirement 4: Convergence Guarantee (Both Algorithms)

**User Story:** As a developer, I want both search algorithms to always terminate within a logarithmic number of attempts, so that the encoding pipeline cannot loop indefinitely or degrade to a linear scan of every possible quality value.

#### Acceptance Criteria

1. FOR ALL valid inputs, THE `CRFSearch` SHALL terminate (return `None` from `record()`) in O(log N) attempts where N is the number of distinct quality values in the range -- i.e. each attempt must halve at least one active search interval.
2. FOR ALL valid inputs, THE `CRFSearchV2` SHALL terminate (return `None` from `record()`) in O(log N) attempts -- each attempt must halve at least one of the two active ranges.
3. WHEN `record()` returns `None`, THE search object SHALL NOT return a non-`None` value from any subsequent `record()` call.

---

### Requirement 5: Protocol State Invariants

**User Story:** As a developer, I want the protocol properties to always reflect a consistent state, so that callers can rely on them without defensive checks.

#### Acceptance Criteria

1. FOR ALL sequences of `record()` calls on any `CRFSearchProtocol` implementation, THE `best_targets_met` SHALL be `True` if and only if `best_crf` corresponds to a passing attempt.
2. WHEN at least one `record()` call has been made, THE `best_crf` SHALL NOT be `None`.
3. WHEN `best_targets_met` is `True`, THE `best_crf` SHALL be the highest CRF value among all passing attempts recorded so far.
4. WHEN `best_targets_met` is `False`, THE `best_crf` SHALL be the CRF of the attempt with the highest `_score_failing_attempt()` score among all failing attempts recorded so far.
5. THE `attempts` property SHALL equal the total number of `record()` calls made, regardless of pass/fail outcome.

---

### Requirement 6: ChunkEncoder Caller Refactoring

**User Story:** As a developer, I want `ChunkEncoder.encode_chunk()` to use the `CRFSearchProtocol` interface exclusively, so that the manual best-failing tracking variables are eliminated and the loop is simplified.

#### Acceptance Criteria

1. WHEN `encode_chunk()` runs its encoding loop, THE `ChunkEncoder` SHALL call `history.record(crf, metrics, quality_targets, granularity)` instead of `adjust_crf(crf, metrics, quality_targets, history, granularity)`.
2. THE `ChunkEncoder` SHALL use `history.best_crf`, `history.best_metrics`, and `history.best_targets_met` instead of the local variables `best_failing_crf`, `best_failing_metrics`, and `best_failing_attempt`.
3. WHEN `history.record()` returns `None`, THE `ChunkEncoder` SHALL finalize the winning attempt using `history.best_crf` and `history.best_metrics`.
4. THE `ChunkEncoder` SHALL NOT maintain `best_failing_crf`, `best_failing_metrics`, or `best_failing_attempt` as separate local variables.
5. THE `ChunkEncoder` SHALL continue to track `best_crf` (highest passing CRF) and `final_attempt` (the `AttemptMetadata` for the winning file) as local variables for file-path resolution, since these are not part of the protocol.

---

### Requirement 7: _load_history_from_sidecars Removed

**User Story:** As a developer, I want the encoding loop to handle recovery via artifact-check-per-step, so that pre-scanning sidecars in filesystem order (which produced inconsistent bracket state) is eliminated.

#### Acceptance Criteria

1. THE `ChunkEncoder._load_history_from_sidecars()` method SHALL be removed.
2. THE encoding loop SHALL recover by calling `_check_existing_encoding()` on each quality step before encoding — reusing cached artifacts naturally until it reaches an un-encoded value.
3. THE `CRFSearch` and `CRFSearchV2` constructors SHALL NOT require pre-population from sidecars.
4. THE encoding loop SHALL track whether any attempt in the search loop required actual encoding or metric measurement work (i.e. was not a pure cache hit).
5. WHEN all attempts for a chunk were cache hits (no encoding or measurement performed), THE `ChunkEncodingResult` SHALL set `reused=True` and the progress bar SHALL advance with `AdvanceState.SKIPPED` for that chunk.
6. WHEN at least one attempt required real work, THE progress bar SHALL advance normally (not skipped), so ETA reflects actual encode time.

---

### Requirement 8: Default Algorithm Selection

**User Story:** As a developer, I want `CRFSearchV2` to be the default algorithm used by the encoding phase, so that new encodes benefit from the improved search strategy.

#### Acceptance Criteria

1. WHEN `ChunkEncoder.encode_chunk()` initialises a new search (no prior sidecars), THE `ChunkEncoder` SHALL instantiate `CRFSearchV2` with the codec's `crf_min` and `crf_max`.
2. THE `CRFSearch` (old algorithm) SHALL remain instantiable and usable as a drop-in alternative by passing it where a `CRFSearchProtocol` is expected.
3. THE encoding phase SHALL NOT hard-code `CRFSearch` as the default; `CRFSearchV2` SHALL be the default.

---

### Requirement 9: Property-Based Test Coverage

**User Story:** As a developer, I want property-based tests covering both algorithms' convergence and state invariants, so that regressions are caught automatically.

#### Acceptance Criteria

1. THE test suite SHALL include a property-based test verifying that `CRFSearch` always terminates within the bound defined in Requirement 4.1 for any valid `(quality_better, quality_worse, granularity, quality_results_sequence)`.
2. THE test suite SHALL include a property-based test verifying that `CRFSearchV2` always terminates within the bound defined in Requirement 4.2 for any valid `(quality_better, quality_worse, granularity, quality_results_sequence)`.
3. THE test suite SHALL include a property-based test verifying the state invariants in Requirement 5 for both algorithms.
4. THE test suite SHALL include example-based tests for each of the state-transition cases in `CRFSearchV2` (Requirement 3.5 through 3.8).
5. THE test suite SHALL include a property-based test verifying the `_score_attempt` sign contract: positive for pass, negative for fail, `0.0` for early acceptance, raises for missing keys.

---

### Requirement 10: Attempt Filename Rename (.crf → .q)

**User Story:** As a developer, I want attempt filenames to use `.q<value>` instead of `.crf<value>`, so that the naming is accurate for all quality control mechanics (CRF, CQ, QP, bitrate).

#### Acceptance Criteria

1. THE `ENCODED_ATTEMPT_GLOB_PATTERN` constant SHALL be updated from `"*.crf*.mkv"` to `"*.q*.mkv"`.
2. THE `ENCODED_ATTEMPT_NAME_PATTERN` regex SHALL be updated to match `.q<value>` instead of `.crf<value>`, with the capture group renamed from `crf` to `quality`.
3. THE `ChunkEncoder._get_attempt_path()` SHALL produce filenames using `.q{value}` instead of `.crf{value}`.
4. THE `ChunkEncoder._check_existing_encoding()` SHALL use the updated glob and regex.
5. ALL callers of `m.group("crf")` SHALL be updated to `m.group("quality")`.
6. THE global `CRF_METRIC_POSITIVE_DELTA` constant SHALL be removed; per-metric `acceptance_delta` on `MetricInfo` replaces it.
7. THE global `PADDING_QUALITY_NUMBER` constant SHALL be removed; a computed `quality_log_padding: int` property on `CodecConfig` replaces it. The padding width SHALL be derived from `max(abs(quality_better), abs(quality_worse))` quantized to `quality_granularity` — specifically `len(str(Decimal(str(max_val)).quantize(quality_granularity)))`. This ensures log columns align correctly for any codec range (e.g. CRF 0–51 with gran 0.5 → 4 chars; VBR 0–100 with gran 0.1 → 5 chars; QP 0–63 with gran 1 → 2 chars). File naming does NOT use this padding — filenames sort correctly without it.
