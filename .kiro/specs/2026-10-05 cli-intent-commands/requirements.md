# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: CLI Intent Commands — intent-based command surface, dependency-derived registry, and explicit stream materialization
- Created: 2026-10-05
- Completed:

## Cross-Spec Notes

### What this spec supersedes

| Superseded | Where | What changed |
|---|---|---|
| Phase-mirroring CLI subcommands (`extract`, `chunk`, `encode`, `merge` as user commands) | `cli.py`, `docs/cli-reference.md` | Replaced by the intent set `auto` / `video` / `audio` (Req 1). The `api.py` named functions remain the phase-level surface for dev/tests. |
| `AudioPhase` as a declared dependency of `MergePhase` | `phases/merge.py` `DEPENDS_ON` | Dropped (Req 7). Merge is video-only by design and by behavior; the dependency existed solely to schedule the fast audio work early in `auto` runs. That scheduling moves to multi-terminal ordering (Req 5). |
| Hand-set `video_required` per api function | `api.py` (`_drive` and the six wrappers) | Derived per run from the terminals' dependency closure (Req 8): a run materializes video-derived components iff its terminals reach the video chain. |
| The static hand-ordered phase registry, including the `video_required`-driven Probe omission | `phase.py` `_build_registry` | The registry is constructed from the dependency closure of the run's terminals (Req 6). No hand-maintained construction list, no omission special case. |
| "Materialize audio/video via some special option" (deferred idea) | TODO §48, bullet 2 | Consumed: `extract` materializes every selected stream kind, video and audio included (Req 9). |
| DEPENDS_ON validation as a standalone backlog item | TODO §54 | Folded into this window as Req 13 — closure-derived construction turns an undeclared dependency into a silent absence, so the audit is a prerequisite of the registry switch, not a follow-up. |

### Related, not superseded

- `2026-09-28 artifact-model` — the Want/Present ledger is the substrate `extract`'s materialization rows join; virtual video/audio streams gain their first sanctioned file-forming path.
- `2026-09-25 file-stream-model` — timestamps and chapters as extracted materials; the timestamps gating on video-need is preserved, only its setting becomes derived.
- TODO §48, bullet 1 (removing include/exclude filters from processing runs) — stays open; filters keep their processing meaning, unchanged by this spec.
- TODO §54 — folded in as Req 13 (see supersedes table); its dependency-graph deliverable lands with this window.
- TODO §33 + §50 (space estimation rework) — the extract dry-run size report (Req 9.4) borrows their spirit but is presentation-only; the estimation rework itself stays open and unaffected.
- TODO §45 (audio phase summary table) — future UX on the audio ledger; the `audio` command's plain output gains nothing here, and §45 stays open.
- TODO §53 (mkvextract-first with fallback) — this spec applies the policy to track materialization (Constraints); the broader sweep it asks for (fps probing, screenshotting, metadata paths) stays open.
- TODO §95 (CLI condensation) — this spec is its plan; the declarative subcommand table is Req 10.
- TODO §96 (merge measuring silence, added 2026-10-05) — UX inside MergePhase; explicitly out of this spec's scope, but noted there as gaining weight once `video` makes Merge a first-class user-facing terminal.
- Future final-materialization phase (mux of merge output + audio + subs into one file) — deliberately **not** designed here (Req 11).
- TODO §59 (parallel metrics) — the two-pass workflow (`video` pass + `audio` pass) this command set serves is the same scenario; no metrics locking is introduced.

## Introduction

The CLI today mirrors the phase list: six pipeline subcommands (`auto`, `extract`,
`chunk`, `encode`, `audio`, `merge`) each drive one terminal phase, plus `config`
and `measure`. That structure served development, but an end user thinks in
intents, not phases — "process the video", "process the audio", "do everything" —
and the per-command handlers are near-identical bodies differing in three axes.

Three verified facts shape the redesign:

1. **Merge is already video-only.** `phases/merge.py` states it in its module
   docstring and behaves so: audio muxing is intentionally omitted, audio
   delivery files are kept alongside the output for the user to mux. MergePhase
   never reads the `AudioPhase` result — its presence in `DEPENDS_ON` is purely
   a scheduling device so `auto` runs the fast audio work before the slow
   encode chain.
2. **The dependency walk is already the scheduler.** Phases memoize per
   instance; a second walk over a shared registry re-visits shared dependencies
   as no-op fast exits. Execution order comes from `DEPENDS_ON` declarations,
   not from registry construction order.
3. **The video stream has a material component.** Extraction materializes the
   video timestamps (and chapters); `video_required=False` is what keeps
   audio-only runs from touching the video stream at all. The flag is a real
   need — what is wrong is setting it by hand per command.

This spec makes the command surface intent-based, makes `DEPENDS_ON` the single
source of truth for what a run constructs and executes, and gives the end user
an explicit materialization tool: `extract` — including for the pass-through
video and audio streams that processing never writes to disk.

## Glossary

- **Intent** — what the user wants done, as opposed to which phase terminates
  the work: "everything" (`auto`), "the video chain" (`video`), "the audio
  chain" (`audio`), "standalone stream files" (`extract`).
- **Terminal phase** — a phase the run drives to completion; the dependency
  walk from a terminal executes everything it depends on. A run may have an
  ordered sequence of terminals.
- **Dependency closure** — the set of phase types reachable from a set of
  terminals through static `DEPENDS_ON` declarations, computable without
  constructing any phase.
- **Video chain** — the phases downstream of extraction that consume
  video-derived materials: Probe, Chunking, Optimization, Encoding, Merge.
- **Materialization (run)** — an extraction-terminated run whose purpose is
  user-facing stream files rather than pipeline inputs: every stream kind
  matching the include/exclude filters is materialized — subtitles,
  attachments, chapters, timestamps as in any run, plus video and audio, the
  pass-through streams that other modes consume directly from the source and
  never write to disk.
- **Pass-through streams** — video and audio streams, which processing runs
  consume virtually (from the source) and never copy to `extracted/`.

## Requirements

Requirement statements use EARS notation: each normative sentence follows one
of the five templates — ubiquitous `The ⟨subject⟩ shall ⟨response⟩`;
`While ⟨state⟩`; `When ⟨trigger⟩`; `If ⟨condition⟩, then`; `Where ⟨feature⟩` —
with conditions combinable (`If ⟨precondition⟩, when ⟨trigger⟩, …`). The
subject is the concrete component (`the CLI`, `the runner`, `the extraction
phase`), never a generic "the system". Lead sentences in bold-titled
paragraphs are context, not requirements.

### The command surface

**Req 1 — Intent-based command set.**

- The CLI shall offer exactly six subcommands: `auto`, `video`, `audio`,
  `extract`, `measure`, `config`.
- If an invocation names a removed subcommand (`chunk`, `encode`, or `merge`),
  then the CLI shall report it as an unknown command — removed with no
  compatibility path (pre-alpha).
- A phase-level execution surface shall remain available to development and
  tests (its naming is TODO §98's question, not this spec's).

**Req 2 — `auto`.**

- When the `auto` command is invoked, the runner shall drive the terminals
  `(AudioPhase, MergePhase)` in that order, so the audio work completes before
  the heavy encode chain begins (scheduling preserved from today).
- The `auto` run shall produce video-only merged outputs plus processed audio
  delivery files alongside for the user to mux (product unchanged from today).

**Req 3 — `video`.**

- When the `video` command is invoked, the runner shall drive `MergePhase` as
  its sole terminal.
- The `video` run shall not execute `AudioPhase`.
- The `video` run's product shall be the same video-only merged output `auto`
  produces.

**Req 4 — `audio`.**

- When the `audio` command is invoked, the runner shall drive `AudioPhase` as
  its sole terminal.
- The `audio` run shall not touch the video stream: the timestamps artifact
  shall not be materialized and the video row shall not be wanted.
- The `audio` command shall not resolve an encoding plan.

**Req 4.1 — Incremental composition.** The two-pass workflow on one workdir is
first-class:

- If the `video` and `audio` work in a workdir has completed, when `auto` is
  then invoked, the run shall perform only the remaining work.

### The execution model

**Req 5 — Multi-terminal runs.**

- When a run declares multiple terminals, the runner shall walk them in order
  over one registry and one run lifecycle (metrics, finalize).
- When a later walk reaches an already-run shared dependency, the walk shall
  read the memoized result — a shared dependency executes once.
- The uniform `Phase.run()` contract shall remain unchanged.
- `RunResult` shall aggregate over every phase that produced a result, and the
  runner shall report `success` true if and only if every terminal completed
  or was reused.

**Req 6 — Dependency-derived registry.**

- The registry shall contain exactly the dependency closure of the run's
  terminals, computed from static `DEPENDS_ON` declarations alone.
- The registry construction order shall be a deterministic topological order.
- If the dependency declarations contain a cycle, then registry construction
  shall fail loudly.
- The static hand-ordered construction list and the `video_required`
  Probe-omission branch shall be deleted.

**Req 7 — Honest merge dependencies.**

- `MergePhase.DEPENDS_ON` shall declare exactly the phases it reads: Job,
  Extraction, Probe, Optimization, Encoding.
- `MergePhase.DEPENDS_ON` shall not declare `AudioPhase`.

**Req 8 — Derived video-need.**

- While the terminals' dependency closure reaches the video chain, the run
  shall treat video as required: the extraction phase shall materialize the
  timestamps artifact and want the video row.
- While the closure does not reach the video chain, the extraction phase
  shall not materialize the timestamps artifact and shall not want the video
  row.
- No api function or command shall set the video-required flag explicitly;
  the flag shall be derived per run and threaded to extraction as today.

### Explicit materialization

**Req 9 — `extract` subcommand.** An end-user tool that materializes streams
from the source into standalone files under `extracted/`, solving the
"every other CLI demands explicit stream lists" pain: by default everything
selected is dumped, and deselection is explicit (e.g. `pyqenc extract
source.mkv --exclude "video-" -y`).

- **9.1 — Every stream kind, including pass-through.**
  - The `extract` run shall materialize every stream kind matching the
    filters — subtitles, attachments, chapters, timestamps as in any run,
    plus video and audio streams as real files. `extract` is the sanctioned
    path where the virtual streams become files.
- **9.2 — Phase-bound.**
  - The `extract` run shall execute through the job → extraction machinery
    against the source, so that `job.yaml` semantics (source fingerprint,
    mismatch handling, force) apply; it is not a side tool operating outside
    the workdir state.
- **9.3 — Selection by the existing filters.**
  - While in a materialization run, the video and audio rows' wanted shall
    follow the shared include/exclude filter.
  - While in a processing run, the video row's want shall remain mode-driven,
    never filter-driven.
- **9.4 — Dry-run contract.**
  - When `extract` is invoked without `-y`, the command shall list the stream
    table and each planned destination file and shall write nothing;
    with `-y`, it shall materialize the listed streams.
  - The plan shall state the cost picture before anything is written:
    materializing video and audio can duplicate a source-sized tree.
    (Amended 2026-10-05 at implementation: per-stream exact sizes are not
    cheaply available — they need a packet-level scan — so the plan logs the
    per-stream destinations and the source-size upper bound instead.)
- **9.5 — Plan-free, cleanup-free.**
  - The `extract` command shall not resolve an encoding plan (no
    strategies/targets arguments).
  - The `extract` command shall not offer `--cleanup` — the materialized
    files are the product.
- **9.6 — Sidecar honesty and preservation.**
  - The `extract` run shall record materialized video/audio entries in
    `extraction.yaml` as what they are.
  - If a later processing run finds materialized video/audio entries in the
    workdir, it shall neither consume them nor delete them as stale.

### Mechanics and hygiene

**Req 10 — Declarative subcommand table (§95).**

- The pipeline commands shall be driven by one command handler over a
  declarative spec table (name, help, api runner, argument groups, output
  flavor), and parser construction shall loop over that table.
- `config`, `measure`, and `extract` shall keep dedicated handlers.
- Beyond the set redesign, the condensation shall introduce no behavior
  change.

**Req 11 — No final-materialization phase.** A phase muxing merge output,
audio, subtitles, and chapters into one complete file is future work: the
user's pre-mux decisions (chapter translation, subtitle rework, best-of
selection) have no settled UX, so nothing in this spec precommits its shape
beyond sitting after Merge and Audio.

- This window shall not introduce a final-materialization phase; it shall get
  its own spec.

**Req 12 — Docs.**

- `docs/cli-reference.md` shall be rewritten for the six-command set.
- `docs/architecture.md` shall be updated for the new execution model: the
  registry-construction description (closure-derived registry, derived
  video-need, multi-terminal runs), the extraction artifact/want-table row
  (materialization mode), the `api.py` surface listing, and the
  CLI/orchestrator diagram — plus the Req 13 dependency table lands there.
- The README examples (`auto` only) shall remain valid, and the README shall
  reflect the intent surface where it describes commands and the workdir
  tree (`extracted/` can now hold materialized video and audio via
  `extract`).
- Where other docs say "extracted audio tracks" to mean source tracks
  enumerated for processing (`docs/audio-processing.md`), or show extraction
  outputs (`docs/Pipeline flow overview.mmd`), the wording/diagram shall be
  disambiguated or extended for the materialization capability.
- `docs/quality-targeting.md` and `CONTRIBUTING.md` shall be verified against
  the new surface and updated only where stale.

**Req 13 — DEPENDS_ON honesty audit (§54).** An undeclared read is a silent
absence under closure construction; an over-declared read is scheduling debt.

- Before or together with the closure-derived registry switch, every phase's
  `DEPENDS_ON` declaration shall be audited against its actual dependency
  reads (`_dep_result` sites), and the registry switch shall land only on
  audited declarations.
- The audit shall fix gaps in the declarations.
- The audit's deliverable shall be a per-phase table of declared dependencies
  and actually consumed inputs (results and settings), recorded in
  `architecture.md` — the dependency graph §54 asked for.

## Constraints

- The uniform `Phase.run()` template is not modified; no no-op or scheduler
  phase is introduced (the rejected alternative to Req 5).
- Materialized file names follow the two-name doctrine: extraction owns
  composition; filesystem names via `safe_name()`, display via `display_name()`.
- Stream materialization uses mkvextract first with ffmpeg fallback per the
  established tool policy (§53); encoding/processing stays ffmpeg-only.
- Pre-alpha: removed commands and reshaped api signatures get no compat shims.
- Processing remains source-anchored: materialized files are never retargeted
  as pipeline inputs (the §48 "retargetting" idea is rejected).
