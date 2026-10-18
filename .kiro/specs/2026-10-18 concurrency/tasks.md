# Tasks

- Spec: Concurrency — per-domain knobs, uniform bounded execution, and app-metrics alignment
- Created: 2026-10-18
- Completed:

## Task 1 — Config knobs and validation (Req 1–4)

- [x] Add the shared `Concurrency` annotated type (`Field(ge=1)` +
      `AfterValidator` clamp to `os.cpu_count()` with one WARNING; CPU count
      unavailable → unchanged) in `pyqenc/app_config.py`.
- [x] Add `AudioConfig.concurrency` and `MeasurementConfig.concurrency`;
      set `validate_assignment=True` on `EncodingConfig`, `AudioConfig`,
      `MeasurementConfig`.
- [x] Add `audio.concurrency: 1` and `measurement.concurrency: 1` with
      unit-of-work and clamp comments to `default_config.yaml`.
- [x] Tests: `ge=1` rejection; clamp + warning; CPU-count-unavailable path;
      assignment validation (CLI-style plain assignment on a loaded config);
      round-trip properties gain the new fields.

## Task 2 — CLI flag (Req 5)

- [x] Move `--concurrency` out of `_add_quality_arguments` to a placement
      covering `auto`, `video`, `audio`, `measure`.
- [x] `_build_config`: `--concurrency N` writes all three knobs.
- [x] Update `docs/cli-reference.md` row.

## Task 3 — Bounded runner (Req 6–9)

- [x] Implement `run_bounded` per the design sketch; home settled toward the
      simpler solution (`pyqenc/utils/concurrency.py`).
- [x] Tests: input-order starts and results at bound 1 and > 1; empty items;
      bound exceeding item count; error propagation cancels siblings.

## Task 4 — Encoding and optimization on the runner (Req 6–9, 16)

- [x] Delete `ChunkQueue` and the `_encode_chunks_parallel` worker pool;
      `encode_all_chunks` seeds COMPLETE pairs, builds the pending list,
      runs `run_bounded` with per-pair wrappers (`run_in_executor` +
      bookkeeping); rename its parameter to `concurrency`.
- [x] `OptimizationPhase._execute`: same runner call, reading
      `config.encoding.concurrency`.
- [x] Bars keep the duration-weighted basis; advances happen once per
      completed pair by that pair's duration (pre-advance skipped pairs
      likewise).
- [x] Retarget the `_encode_chunks_parallel` test mocks to the new seam or
      replace with behavior tests.

## Task 5 — Audio batch execution and per-chain timing (Req 6–10, 14)

- [x] `AudioPhase._execute`: one `asyncio.run(run_bounded(...))` over the
      pending jobs; `_produce_one`'s per-job loop dissolves; per-job error
      containment and the count-based bar advance unchanged in behavior.
- [x] Wrap each job in `collector.time(MetricKey.AUDIO, chain_name)`.
- [x] Tests: order at 1 vs > 1, containment, per-chain dotted keys.

## Task 6 — Measure per-video tasks (Req 10, 11)

- [x] `run_measure`: one task per video (source = screenshots only;
      distorted = screenshots → metrics → sidecar), `run_bounded` with
      `measurement.concurrency`.
- [x] One shared duration-based bar over Σ distorted effective durations;
      `_run_metrics` feeds `PROGRESS` ticks via the advance pass-down
      (replacing `show_progress=True` inner bars).
- [x] Contain per-video measurement failures; surface failed videos in the
      summary; screenshots-taken summary moves after the task run.
- [x] Tests: task shape, containment, bar feed.
      (Order/containment covered at the runner level + api wiring; the
      full-body behavior rides the Task 12 e2e.)

## Task 7 — App-metrics alignment (Req 12, 13, 15)

- [x] Rename `PipelineMetrics` → `AppMetrics`; flatten the persisted root
      (`time_distribution`, `convergence` at file top; `_write_atomic`,
      `_try_resume`, `__all__`).
- [x] `METRIC_KEY_QUALITY_MEASURE` → `METRIC_KEY_MEASURE` with value
      `measure` (encoding/optimization/merge spans).
- [x] State the wall-vs-sub-time invariant on `AppMetrics`; delete the
      stale `parallelism` note.
- [x] Update `tests/test_metrics.py`, `tests/test_metrics_integration.py`.

## Task 8 — Docs (Req 17)

- [x] Rewrite `docs/architecture.md` §Parallelism and §Pipeline parallelism
      default of 1 to the landed model.
- [x] README: `--concurrency` guidance for audio/measure.
- [x] `default_config.yaml` knob comments carry the empirical results (or
      "no measured benefit") once the validation runs land.

## Task 9 — Backlog hygiene

- [x] Remove TODO §2 and §12 (consumed by this spec); keep the
      `Last known = N` tail marker current.

## Task 10 — Cross-spec review

- [x] Review this spec against `app-metrics-two-tier`,
      `config-settings-alignment`, `audio-chains`, `cli-intent-commands`,
      `progress-bar-revamp`; add supersession summaries to the tops of the
      affected specs.

## Task 11 — Full gates

- [x] `uvx ruff check`, `uvx ty check`, `uv run python -m pytest` (compare
      against the pre-existing failures baseline; `tests/e2e/` blocked by
      TODO §114).
      (2026-10-18: 17 failed / 1132 passed — all 17 verified identical on
      the branch base `ea5366e`; zero regressions.)

## Task 12 — Empirical validation (post-implementation, user-visible runs)

- [x] Video A/B: `h264+ultrafast` fixed-quality e2e, `--concurrency 1` vs
      `2`, fresh work dirs under `D:\_encoding\pyqenc_parallel`.
      (2026-10-18: encoding phase 13:13 → 10:36 in single runs; RULING: no
      benefit claim — the observation is contradicted by the measure-intent
      regression (concurrent metric passes contend for the same cores) and
      ffmpeg already saturates the CPU. The knob is for observed CPU
      underutilization only.)
- [x] Audio A/B: 2 streams × 2 chains, concurrency 1 vs 4.
      (2026-10-18: 51.8s → 22.2s phase, 55.5s → 26.0s wall — real benefit,
      exact effect machine-dependent.)
- [x] Measure effect test: several distorted videos, 1 vs >1.
      (2026-10-18: 2 distorted videos, 889.7s at 1 vs 997.2s at 2 — ~12%
      REGRESSION; one metrics pass already saturates the CPU.)
- [x] Record findings into the knob doc comments (Task 8 remainder).
      Also caught live: nested-`asyncio.run` bug in the measure rework
      (fixed in `0e2b9e9`).

## Task 13 — Closeout

- [ ] Update `- Completed:` in all three spec files.
