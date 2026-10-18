# Tasks

- Spec: Concurrency — per-domain knobs, uniform bounded execution, and app-metrics alignment
- Created: 2026-10-18
- Completed:

## Task 1 — Config knobs and validation (Req 1–4)

- [ ] Add the shared `Concurrency` annotated type (`Field(ge=1)` +
      `AfterValidator` clamp to `os.cpu_count()` with one WARNING; CPU count
      unavailable → unchanged) in `pyqenc/app_config.py`.
- [ ] Add `AudioConfig.concurrency` and `MeasurementConfig.concurrency`;
      set `validate_assignment=True` on `EncodingConfig`, `AudioConfig`,
      `MeasurementConfig`.
- [ ] Add `audio.concurrency: 1` and `measurement.concurrency: 1` with
      unit-of-work and clamp comments to `default_config.yaml`.
- [ ] Tests: `ge=1` rejection; clamp + warning; CPU-count-unavailable path;
      assignment validation (CLI-style plain assignment on a loaded config);
      round-trip properties gain the new fields.

## Task 2 — CLI flag (Req 5)

- [ ] Move `--concurrency` out of `_add_quality_arguments` to a placement
      covering `auto`, `video`, `audio`, `measure`.
- [ ] `_build_config`: `--concurrency N` writes all three knobs.
- [ ] Update `docs/cli-reference.md` row.

## Task 3 — Bounded runner (Req 6–9)

- [ ] Implement `run_bounded` per the design sketch; home settled toward the
      simpler solution (expect `pyqenc/utils/concurrency.py`).
- [ ] Tests: input-order starts and results at bound 1 and > 1; empty items;
      bound exceeding item count; error propagation cancels siblings.

## Task 4 — Encoding and optimization on the runner (Req 6–9, 16)

- [ ] Delete `ChunkQueue` and the `_encode_chunks_parallel` worker pool;
      `encode_all_chunks` seeds COMPLETE pairs, builds the pending list,
      runs `run_bounded` with per-pair wrappers (`run_in_executor` +
      bookkeeping); rename its parameter to `concurrency`.
- [ ] `OptimizationPhase._execute`: same runner call, reading
      `config.encoding.concurrency`.
- [ ] Bars become count-based over pairs (pre-advance skipped pairs).
- [ ] Retarget the `_encode_chunks_parallel` test mocks to the new seam or
      replace with behavior tests.

## Task 5 — Audio batch execution and per-chain timing (Req 6–10, 14)

- [ ] `AudioPhase._execute`: one `asyncio.run(run_bounded(...))` over the
      pending jobs; `_produce_one`'s per-job loop dissolves; per-job error
      containment and the count-based bar advance unchanged in behavior.
- [ ] Wrap each job in `collector.time(MetricKey.AUDIO, chain_name)`.
- [ ] Tests: order at 1 vs > 1, containment, per-chain dotted keys.

## Task 6 — Measure per-video tasks (Req 10, 11)

- [ ] `run_measure`: one task per video (source = screenshots only;
      distorted = screenshots → metrics → sidecar), `run_bounded` with
      `measurement.concurrency`.
- [ ] One shared duration-based bar over Σ distorted effective durations;
      `_run_metrics` feeds `PROGRESS` ticks via the advance pass-down
      (replacing `show_progress=True` inner bars).
- [ ] Contain per-video measurement failures; surface failed videos in the
      summary; screenshots-taken summary moves after the task run.
- [ ] Tests: task shape, containment, bar feed.

## Task 7 — App-metrics alignment (Req 12, 13, 15)

- [ ] Rename `PipelineMetrics` → `AppMetrics`; flatten the persisted root
      (`time_distribution`, `convergence` at file top; `_write_atomic`,
      `_try_resume`, `__all__`).
- [ ] `METRIC_KEY_QUALITY_MEASURE` → `METRIC_KEY_MEASURE` with value
      `measure` (encoding/optimization/merge spans).
- [ ] State the wall-vs-sub-time invariant on `AppMetrics`; delete the
      stale `parallelism` note.
- [ ] Update `tests/test_metrics.py`, `tests/test_metrics_integration.py`.

## Task 8 — Docs (Req 17)

- [ ] Rewrite `docs/architecture.md` §Parallelism and §Pipeline parallelism
      default of 1 to the landed model.
- [ ] README: `--concurrency` guidance for audio/measure.
- [ ] `default_config.yaml` knob comments carry the empirical results (or
      "no measured benefit") once the validation runs land.

## Task 9 — Backlog hygiene

- [ ] Remove TODO §2 and §12 (consumed by this spec); keep the
      `Last known = N` tail marker current.

## Task 10 — Cross-spec review

- [ ] Review this spec against `app-metrics-two-tier`,
      `config-settings-alignment`, `audio-chains`, `cli-intent-commands`,
      `progress-bar-revamp`; add supersession summaries to the tops of the
      affected specs.

## Task 11 — Full gates

- [ ] `uvx ruff check`, `uvx ty check`, `uv run python -m pytest` (compare
      against the pre-existing failures baseline; `tests/e2e/` blocked by
      TODO §114).

## Task 12 — Empirical validation (post-implementation, user-visible runs)

- [ ] Video A/B: `h264+ultrafast` fixed-quality e2e, `--concurrency 1` vs
      `2`, fresh work dirs under `D:\_encoding\pyqenc_parallel`.
- [ ] Audio A/B: 2 streams × 2 chains, concurrency 1 vs 4.
- [ ] Measure effect test: several distorted videos, 1 vs >1.
- [ ] Record findings into the knob doc comments (Task 8 remainder).

## Task 13 — Closeout

- [ ] Update `- Completed:` in all three spec files.
