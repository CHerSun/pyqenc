# Design Document

<!-- markdownlint-disable MD024 -->

- Spec: Concurrency — per-domain knobs, uniform bounded execution, and app-metrics alignment
- Created: 2026-10-18
- Completed: 2026-10-19

## Current state (verified 2026-10-18)

- One knob: `encoding.concurrency: int` (`app_config.py:205`, default `1` at
  `default_config.yaml:37`); no validation, no audio or measurement knob.
- The CLI applies overrides by plain assignment in `_build_config`
  (`cli.py:375-399`), bypassing pydantic validation (no
  `validate_assignment` on the subconfigs).
- Optimization and encoding run `_encode_chunks_parallel` — N worker tasks
  over `ChunkQueue` plus a redundant `Semaphore(N)` (`encoding.py:1569-1717`);
  the machinery is always-async, including at `concurrency=1` (proven in every
  e2e run; `ProgressBar` updates already arrive from non-main threads).
- Audio is serial: one `asyncio.run(execute_chain(...))` per (track, chain)
  job (`audio.py:494-512`, `_produce_one:550`); `execute_chain` is a genuine
  coroutine over `run_ffmpeg_async`.
- `measure` is one coroutine with sequential awaits: per-video screenshot
  loop, then per-video metrics loop (`measure.py:1129`, `1164`); per-video
  inner bars via `show_progress=True, bar_title=…` (`_run_metrics:377-380`).
- Bar feed today: merge threads its outer bar into measurements
  (`show_progress=advance`, `merge.py:867` → weighted `PROGRESS` ticks,
  `visualization.py:1194`) — the pass-down seam the measure metrics sweep
  will reuse. Encoding/optimization fraction is duration-weighted
  (Σ chunk durations × strategies) with no in-job feed — the doctrine KEEPS
  that basis; only the update cadence stays one advance per completed pair.
- App metrics: `PipelineMetrics` with a `pipeline_metrics` root wrapper
  (`metrics.py:235,733`) — both terminology leftovers; dotted key
  `quality_measure` (`constants.py:91`); the spec's `parallelism` field was
  never implemented (`metrics.py:241` note); TODO §12: no per-chain audio
  timing exists.
- `docs/architecture.md:510,601` describe the pre-`audio-chains` fan-out
  parallelism — stale since that rework.

## Ruling log (user decisions, 2026-10-18)

- Uniform async for concurrency == 1 and > 1 — no synchronous twin path.
  `ProgressBar` (alive_progress) renders from its own monitor thread and
  `advance()` is a plain callable; cross-thread feeding is production-
  exercised, so sync-at-1 buys nothing and adds an untested second path.
- Orchestration is task-level only: N wanted-but-incomplete jobs in →
  bounded processing → N artifacts out, sorted once upfront, no
  prioritization or special cases. The `ChunkQueue` "finish started chunks
  first" rule is deleted — no behavioral contract depends on it (pairs of
  one chunk share no data).
- Config values are clamped, not warned-and-kept: consumers receive a ready
  value; values above the CPU count are useless or damaging.
- Validation lives at config load (pydantic), never at phase start.
- One CLI concurrency flag: when given, it assigns the user value to every
  concurrency knob; consumers read only their own domain's field. Per-domain
  control is the config file's job.
- `metrics.yaml` root wrapper is dropped (flatten) — the file holds nothing
  else.
- No `parallelism` field in the dump: the effective multiplier is derivable
  from Σ sub-time ÷ wall-clock (this is the §2 reconciliation, downward).
- TODO §12 (per-chain audio dotted timing) folds into this spec.
- Orchestration memory shape: live task count proportional to the bound
  regardless of job count (a movie-scale run is 2–2.5k scenes × 4–5
  strategies ≈ 10k+ pairs; fan-out gather would materialize every wrapper
  as a live Task upfront — works, but pays bookkeeping memory for nothing).
- Bar-mode doctrine: count-based bars for domains with many small items
  and non-deterministic internal steps (encoding, optimization, audio) —
  one advance per item finalization, weighted by the item's duration, NOT
  unweighted counting; duration-based bars for domains with few
  deterministic long-running steps (merge, measure — fine-grained in-job
  `PROGRESS` ticks are honest there).
- Measure task shape: screenshots and measurement are ONE task per video
  (the source included — screenshots only); progress comes solely from the
  measurements' fine-grained ticks. Merge's shape, not a two-sweep
  structure.

## Config model

One shared annotated type, used by all three fields:

```python
def _clamp_to_cpus(v: int) -> int: ...

Concurrency = Annotated[int, Field(ge=1), AfterValidator(_clamp_to_cpus)]
```

- `_clamp_to_cpus`: `cpus = os.cpu_count()`; `None` → unchanged (Req 3);
  `v > cpus` → store `cpus`, log one WARNING `original → stored`.
- Fields: `EncodingConfig.concurrency`, `AudioConfig.concurrency`,
  `MeasurementConfig.concurrency`; `default_config.yaml` gains
  `audio.concurrency: 1` and `measurement.concurrency: 1` with comments
  stating the unit of work per domain and the clamp rule.
- The three subconfigs set `model_config = ConfigDict(validate_assignment =
  True)` so `_build_config`'s plain CLI assignments (`cli.py:375-399`) pass
  through the same validators (Req 4). Side effect, intended: other plain
  assignments (`--sampling`, `--scene-threshold`, …) now validate on write —
  late phase-start failures become early config failures.

CLI wiring (Req 5): `--concurrency N` writes `N` to all three knobs in
`_build_config`; each phase reads only its own domain's field. The flag
leaves `_add_quality_arguments` for a shared placement so `audio` and
`measure` receive it too — exact placement settled at implementation.

## Orchestration — one bounded runner

The contract is the same everywhere: a pre-filtered, pre-sorted list of
independent jobs in; processing respecting the bound; one result per job,
out in input order. Classification (which items need work — recovery
scans, chain invalidation, "always all") stays domain-side and upstream;
sorting happens once, before the call; no prioritization, no special
cases (Req 9).

```python
_ItemT   = TypeVar("_ItemT")
_ResultT = TypeVar("_ResultT")

async def run_bounded(
    items:   Sequence[_ItemT],
    bound:   int,
    process: Callable[[_ItemT], Awaitable[_ResultT]],
) -> list[_ResultT]:
    """At most `bound` of `process(item)` in flight; results in input order."""
    pending: deque[tuple[int, _ItemT]] = deque(enumerate(items))
    done:    dict[int, _ResultT]       = {}

    async def worker() -> None:
        while pending:
            i, item = pending.popleft()
            done[i] = await process(item)

    await asyncio.gather(*(worker() for _ in range(min(bound, len(pending)))))
    return [done[i] for i in range(len(items))]
```

`advance` is not a runner parameter: each domain binds it into its
`process` (closure or `functools.partial` — settled at implementation),
and **every** `advance` call is domain code — the runner makes none. The
division of labor that forces this: the runner could only ever make
count-shaped calls, while duration-based reporting lives at the ffmpeg
seam deep inside the job — any runner-side advancing would be
mode-dependent coupling. The domain wrapper owns the completion tick
(count-based domains: `advance(1, state)` per item; duration-based:
`advance(weight, state)` per task, state from the job's own outcome) and
any mid-job feed (only duration-based domains). The phase owns the bar's
lifecycle — creation, totals, units, and the final `COMPLETE` signal
after the batch. The pass-down seam is the existing one — merge already
threads its outer bar into measurements as `show_progress=advance`
(`merge.py:867`), where the measurer converts ffmpeg's progress callback
into weighted `PROGRESS` ticks (`visualization.py:1194`); completion
advances stay authoritative (one full-unit `SUCCESS` per job,
`SKIPPED`/`FAILED` at zero weight — merge's accounting).

`bound` workers consume a plain list — the task queue. Workers `popleft`
between awaits on the single-threaded loop, so starts happen in input
order and `bound == 1` is one worker walking the list — sequential in
input order (Req 6–8). Empty input degenerates cleanly (zero workers,
`gather()` of nothing, `[]` out). A raising `process` propagates through
the gather of workers loudly (siblings are cancelled; nothing shrinks
silently). Exact name and home settled at implementation (four call
sites; expectation is one shared helper).

`process(item)` = the domain task awaited, then its bookkeeping. It does
not raise in practice — per-job error containment is uniform (Req 10):
every wrapper records its own job's failure (encoding/optimization:
failed result rows, already the case; audio: the per-job try/except;
measure: per-video failure handling, extended from screenshots-only to
measurements too) and the batch continues; phases aggregate at end. A
raise is the bug path — `gather` propagates it loudly and aborts the
run; it is the safety net, not a control-flow mechanism.

### Per-domain task shape

- **Encoding/optimization** (video): job = `run_in_executor(None,
  encoder.encode_chunk, …)` (the sync search stays; it is task-internal
  mechanics, Req 9). Pre-call: COMPLETE-pair seeding from recovery and
  pending-list filtering (unchanged, moves out of the worker loop).
  Wrapper bookkeeping: result append, `reused/encoded` counters, bar
  advance, convergence `collector.step`. Post-call:
  `_assert_one_winner_per_chunk` unchanged. `ChunkQueue` is deleted; the
  pending list is the flattened (chunk, strategy) order — identical to the
  1-worker order today. The bar keeps its duration-weighted basis and
  advances once per completed pair, by that pair's chunk duration (Req 11);
  `advance` is received per the uniform contract but not fed mid-job —
  non-deterministic substeps (search attempts) make in-job weighting
  dishonest.
- **Audio**: job = `execute_chain(...)` awaited directly. The per-job
  `asyncio.run` in `_produce_one` dissolves into one `asyncio.run` over the
  batch; wrapper bookkeeping = state flip, counters, count-bar advance,
  per-job error containment (Req 10). Per-chain timing spans wrap the job:
  `collector.time(MetricKey.AUDIO, chain_name)` (Req 14). The count-based
  bar receives completion ticks only — in-chain `PROGRESS` feed would need
  new plumbing inside `execute_chain` and stays out of scope.
- **Measure**: one task per video — the source task (screenshots only,
  zero bar weight) followed by one task per distorted video (screenshots,
  metrics, sidecar in that order). One shared duration-based bar over the
  Σ distorted effective durations, fed by in-job `PROGRESS` ticks during
  the measurements through `advance` (the merge pass-down: `_run_metrics`
  switches from `show_progress=True` inner bars to the shared callable);
  completion advances settle the full duration per merge's accounting, and
  screenshot work rides unweighted. The screenshots-taken summary moves
  after the task run. At `bound == 1` the order is source screenshots,
  then per video: screenshots → metrics — outputs identical to today, log
  lines interleave per video.

### Progress bars

Bar-mode doctrine (Req 11): count-based where items are many/small with
non-deterministic internal steps — the bar is duration-weighted by the
items (encoding/optimization keep Σ chunk durations × strategies; audio
counts uniformly-weighted jobs) and advances once per item finalization,
by that item's duration — precise fractions and ETA without pretending to
know mid-item progress. Duration-based where steps are few and
deterministic — merge (unchanged, already fed) and measure: one shared
duration-based bar fed by the measurements' in-job `PROGRESS` ticks through
`advance`; screenshot work rides unweighted. `alive_progress` renders from
its monitor thread; `advance()` is safe from event-loop and worker
contexts.

## App metrics alignment

- `PipelineMetrics` → `AppMetrics` (class, `__all__`, internal uses) —
  Req 12.
- Persisted shape flattens: `{"time_distribution": …, "convergence": …}` at
  the file root; `_write_atomic`/`_try_resume` and their tests follow. An
  old-format file fails resume validation → WARNING → fresh start
  (run-once work dirs; acceptable).
- `METRIC_KEY_QUALITY_MEASURE = "quality_measure"` → value `measure`
  (renders `encoding.measure`, `optimization.measure`, `merge.measure`);
  constant renamed `METRIC_KEY_MEASURE` — Req 13. Old persisted keys ride
  along harmlessly until the next fresh work dir.
- Per-chain audio dotted timing (Req 14) wraps each audio job.
- The wall-vs-sub-time invariant (Req 15) is stated once on `AppMetrics`
  and echoed in `docs/architecture.md`; no `parallelism` field, and the
  stale note at `metrics.py:241` is deleted with it.

## Vocabulary and renames

- `max_parallel` retires with the machinery it named: `_encode_chunks_parallel`
  is deleted (Req 16, the per-domain bullet), and `encode_all_chunks` takes
  `concurrency` — passed by the phase from `config.encoding.concurrency`, as
  today.
- No new glossary terms: `concurrency` is the established term (2026-08-12
  config-settings rename), now applied to three domains.

## Docs

- `docs/architecture.md` §Parallelism and §Pipeline parallelism default of
  1 rewritten to the landed model (three knobs, uniform bounded execution,
  clamp rule; the pre-chains fan-out claims removed) — Req 17.
- `docs/cli-reference.md`: the `--concurrency` row becomes per-intent;
  README's slow-encoding guidance gains the audio/measure mentions.
- Each knob's doc comment carries the measured effect from the validation
  plan (or "no measured benefit" where the experiments show none).

## Cross-spec impact (task-gated review)

- `2026-04-22 app-metrics-two-tier` — root flatten, `AppMetrics` name,
  `measure` key, `parallelism` field reconciled down.
- `2026-08-12 config-settings-alignment` — new fields, clamp + assignment
  validation semantics.
- `2026-09-11 audio-chains` — parallel (track, chain) execution restored.
- `2026-10-05 cli-intent-commands` — `--concurrency` on `audio`/`measure`;
  `auto` widening.
- `2026-03-19 progress-bar-revamp` — shared-bar-at->1 note for measure.

## Validation

Static gates (`uvx ruff check`, `uvx ty check`) + `uv run python -m pytest`
per `commands-and-gates.md`; test rules per `agent-testing.md` (black-box,
properties first, no internal mocking — the `_encode_chunks_parallel`
mocks in existing tests are retargeted to the new seam or dropped in favor
of behavior tests). Empirical plan per `requirements.md`.
