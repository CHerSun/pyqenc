# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: Concurrency — per-domain knobs, uniform bounded execution, and app-metrics alignment
- Created: 2026-10-18
- Completed:

## Cross-Spec Notes

### What this spec supersedes

| Superseded | Where | What changed |
|---|---|---|
| `PipelineMetrics` class name and the `pipeline_metrics` root wrapper in `metrics.yaml` | `pyqenc/metrics.py`, `metrics.yaml` | Renamed to `AppMetrics` and flattened to the file root (Req 12) — the file holds nothing else, and the glossary term is **app metrics** (`docs/glossary.md`). |
| The `quality_measure` dotted key | `pyqenc/constants.py`, encoding/merge timing spans | Becomes `measure` in every prefix (Req 13) — the `quality_` prefix is redundant under the glossary's domain split. |
| The app-metrics `parallelism` field | `2026-04-22 app-metrics-two-tier` `design.md:299`, TODO §2 | Reconciled downward: not persisted — the effective multiplier is derivable as Σ sub-time ÷ wall-clock from data already in the dump (Req 15). |
| `ChunkQueue` start-prioritization ("finish started chunks first") | `_encode_chunks_parallel`, `pyqenc/phases/encoding.py` | Deleted (Req 9): no behavioral contract depends on it — pairs of one chunk share no data, and recovery granularity is per pair. |
| Serial audio execution (one `asyncio.run` per (track, chain) job) | `_produce_one`, `pyqenc/phases/audio.py` | Replaced by bounded parallel execution over the pending list (Req 6–8); per-job error containment preserved (Req 10). |
| Pre-`audio-chains` parallelism claims in the architecture doc | `docs/architecture.md` §Parallelism, §Pipeline parallelism default of 1 | Rewritten to the landed model (Req 17); the fan-out filter-graph description was true before the chains rework and stale since. |
| Per-video inner progress bars in the `measure` command | `run_measure` / `_run_metrics` (`show_progress=True, bar_title=…`) | Replaced by one shared duration-based bar fed by the measurements' `PROGRESS` ticks (Req 11) — the merge-phase mechanics, no new pattern. |
| Screenshot sweep, then metrics sweep, as separate `run_measure` stages | `run_measure` (`measure.py:1129`, `1164`) | Folded into per-video tasks — screenshots and measurement in one task (Req 11); outputs identical, log lines interleave per video. |
| Metrics failures abort the `measure` run | `run_measure` metrics loop (`measure.py:1164` — no containment) | Contained per video, like screenshot failures (Req 10); failed videos surface in the summary table. |
| TODO §2 (parallelism spec/code reconciliation) | `TODO.md` | Consumed by this spec (the `parallelism`-field row above). |
| TODO §12 (per-chain dotted audio metrics) | `TODO.md` | Folded in as Req 14. |

### Related, not superseded

- `2026-08-12 config-settings-alignment` — the `concurrency` vocabulary and the plain-flag CLI doctrine stand; this spec adds fields to the same models and moves value validation into the models themselves (Req 1–4). Flags stay plain assignments.
- `2026-03-19 progress-bar-revamp` — bar mechanics unchanged; measure's shared count-based bars (Req 11) are the same call pattern audio already uses. alive_progress renders from its own monitor thread, so cross-thread `advance()` feeding (already production-exercised by encoding) needs no change.
- TODO §51 / QualitySearch V4 — per-chunk search is task-internal mechanics under Req 9; untouched.
- TODO §102 (anytime/deficit scheduler) — would change job *order* semantics; the bounded runner here is the substrate such a scheduler would replace, but scheduling stays strictly FIFO in this spec.
- TODO §59 (parallel metrics across processes) — a multi-process file-collision problem; unaffected by in-process concurrency.
- TODO §117 (observability research) — the wall-vs-sub-time invariant is *stated* here (Req 15); presentation, spans, and dump redesign ride there.

## Introduction

One concurrency knob exists today: `encoding.concurrency` (default 1), consumed
by the optimization and encoding worker pools. Audio processing is strictly
serial — one `asyncio.run(execute_chain(...))` per (track, chain) job — and the
standalone `measure` command runs its screenshot and metrics sweeps serially.
`docs/architecture.md` still describes the pre-chains fan-out parallelism that
no longer exists.

Three verified facts shape the design:

1. **The async substrate is production-proven.** `run_ffmpeg_async` is a real
   subprocess coroutine; `execute_chain` and `run_measure` are genuine
   coroutines over it; `measure_chunk_async` exists for in-loop use. The
   encoding path already runs the async machinery at `concurrency=1` in every
   e2e run, including `ProgressBar` updates fed from non-main threads.
2. **Orchestration reduces to one shape.** Optimization, encoding, audio, and
   measure all consume a static, pre-filtered, pre-sorted list of independent
   jobs; classification (which items need work) is domain recovery logic that
   already lives upstream. Nothing spawns new jobs mid-run.
3. **Un-validated config values reach consumers.** The CLI applies overrides by
   plain attribute assignment, bypassing pydantic validation entirely
   (`cli.py:375-399`); no concurrency field has a `ge=1` guard.

This spec gives each work domain its own validated knob, replaces all four
execution sites with one bounded runner (identical order and machinery at
every value), and aligns the app-metrics naming and persisted shape that the
same motion touches.

## Glossary

- **Domain** — one concurrency scope: video encoding work
  (`encoding.concurrency` — optimization + encoding phases), audio work
  (`audio.concurrency`), standalone measurement work
  (`measurement.concurrency` — the `measure` command).
- **Job** — one unit of bounded execution, opaque to the runner: a
  (chunk, strategy) pair with its quality search; a (track, chain) pair with
  its multi-pass filter chain; one video's screenshots and measurement.
- **Pending list** — the domain's jobs, filtered to wanted-but-incomplete and
  sorted once, before the runner is called.
- **Bound** — the domain's concurrency value: how many jobs may be in flight.
- **Wall-clock vs sub-time** — top-level app-metrics entries are wall-clock
  spans owned by the phase template; dotted entries are inclusive sub-spans
  that may overlap under concurrency and are judged against their internal
  sums, never against wall-clock.

## Requirements

Requirement statements use EARS notation: each normative sentence follows one
of the five templates — ubiquitous `The ⟨subject⟩ shall ⟨response⟩`;
`While ⟨state⟩`; `When ⟨trigger⟩`; `If ⟨condition⟩, then`; `Where ⟨feature⟩` —
with conditions combinable (`If ⟨precondition⟩, when ⟨trigger⟩, …`). The
subject is the concrete component (`the CLI`, `the runner`, `the audio
phase`), never a generic "the system". Lead sentences in bold-titled
paragraphs are context, not requirements.

### Configuration

**Req 1 — Per-domain knobs.**

- The app config shall provide integer fields `encoding.concurrency`,
  `audio.concurrency`, and `measurement.concurrency`, each with bundled
  default `1`.
- The `audio` and `measurement` fields shall document their unit of work
  (one (track, chain) job; one video's sweep) in `default_config.yaml`.

**Req 2 — Lower bound.**

- If a concurrency field value is less than `1`, then the config validation
  shall reject the configuration with a validation error.

**Req 3 — CPU ceiling clamp.**

- When a concurrency field value exceeds the machine's CPU count, the config
  validation shall store the CPU count instead and shall log one warning
  naming the original and stored values.
- When the CPU count cannot be determined, the config validation shall store
  the value unchanged.

**Req 4 — Validated assignments.**

- When a value is assigned to any field of a subconfig carrying a concurrency
  knob, the assignment shall pass through the same validation as
  construction, so that consumers never observe an unvalidated value.

**Req 5 — One CLI flag writes every knob.**

- The CLI shall expose at most one concurrency argument per intent.
- When `--concurrency N` is given, the CLI shall write `N` to every
  concurrency field (`encoding.concurrency`, `audio.concurrency`,
  `measurement.concurrency`); each consumer shall read only its own domain's
  field. Per-domain control is the config file's job.

### Bounded execution

**Req 6 — Sequential at 1.**

- When a domain's concurrency is `1`, the runner shall execute that domain's
  jobs sequentially in pending-list order, observably identical to the
  current sequential behavior.

**Req 7 — Bounded, input-ordered above 1.**

- When a domain's concurrency is greater than `1`, the runner shall execute
  at most that many jobs simultaneously and shall start jobs in
  pending-list order.
- The runner shall return one result per job, in input order, at every
  concurrency value.
- Each job shall fully own its progress reporting through the phase's
  advance callable (completion ticks and any in-job `PROGRESS` ticks); the
  runner shall make no progress calls.

**Req 8 — One machinery.**

- The runner shall use the same execution machinery for every concurrency
  value, differing only in the bound; no synchronous twin path shall exist.
- The runner shall keep its live task count proportional to the bound,
  regardless of the job count.

**Req 9 — Task-level orchestration.**

- The runner shall treat each job as an opaque awaitable: quality-search
  attempts, multi-pass filter chains, and screenshot/metrics sweeps are
  task-internal mechanics invisible to it.
- The phases shall classify (wanted-but-incomplete) and sort their pending
  lists before invoking the runner; the runner shall perform no
  prioritization or reordering of its own.

**Req 10 — Per-job error containment.**

- When a job fails at any concurrency, the job wrapper shall record only
  that job's failure (a FAILED artifact or a failed result row) and shall
  not abort the remaining jobs; the phases shall aggregate failures and
  report them at phase end (each phase's outcome strictness is unchanged).
- The `measure` command shall contain per-video measurement failures the
  same way (today they propagate and abort the run); failed videos surface
  in the summary.

**Req 11 — Progress-bar modes and shared bars.**

- The phases shall use count-based bars for domains with many small items
  and non-deterministic internal steps (encoding, optimization, audio), and
  duration-based bars for domains with few deterministic long-running steps
  (merge, measure).
- The encoding and optimization bars shall measure progress by completed
  (chunk, strategy) pairs.
- The `measure` command shall treat each video (source and distorted) as one
  task comprising that video's screenshots and, for distorted videos, its
  quality measurement and sidecar.
- The `measure` command shall use one shared duration-based progress bar,
  receiving in-job `PROGRESS` ticks from the measurements through the
  advance callable (the merge-phase pattern); the per-video inner bars shall
  be gone, and screenshot work shall carry no bar weight.

### App metrics

**Req 12 — `AppMetrics`, flattened dump.**

- The app-metrics top-level model shall be named `AppMetrics`.
- The persisted `metrics.yaml` shall carry the model's fields
  (`time_distribution`, `convergence`) at the file root, without a wrapping
  key.

**Req 13 — `measure` dotted key.**

- The measurement-span dotted key shall be `measure` in every metric prefix
  (`encoding.measure`, `optimization.measure`, `merge.measure`).

**Req 14 — Per-chain audio timing.**

- The audio phase shall record per-chain dotted timing under
  `audio.<chain>` for every executed (track, chain) job.

**Req 15 — Wall-vs-sub-time invariant; no `parallelism` field.**

- The `AppMetrics` docstring shall state the wall-vs-sub-time invariant
  (Glossary), and the app-metrics dump shall not persist a `parallelism`
  field — the effective multiplier is derivable as Σ sub-time ÷ wall-clock.

### Naming and docs

**Req 16 — `max_parallel` retired.**

- The `_encode_chunks_parallel` helper shall be deleted in favor of the
  bounded runner.
- The `encode_all_chunks` entry point shall name its concurrency parameter
  `concurrency`, retiring `max_parallel`.

**Req 17 — Docs describe the landed state.**

- `docs/architecture.md` shall describe the landed parallelism behavior only;
  the pre-`audio-chains` fan-out claims shall be gone.
- The CLI and README docs shall document each knob's measured effectiveness
  (or the absence of a measured benefit).

## Validation plan (empirical)

- **Video:** fresh e2e `auto` runs with a single `h264+ultrafast` strategy at
  fixed quality, `--concurrency 1` vs `2`, fresh work dirs under
  `D:\_encoding\pyqenc_parallel`; wall-clock and the
  `encoding.h264+ultrafast` vs `encoding.measure` split decide the knob's
  documented benefit.
- **Audio:** post-implementation, 2 streams × 2 chains at concurrency `1`
  vs `4`; same folder convention.
- **Measure:** post-implementation, several distorted videos at `1` vs
  `measurement.concurrency > 1`; the knob's documented notice states the
  measured effect.
