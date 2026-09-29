# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: Artifact Model — the generic recovery & contract layer over the file → stream object model
- Created: 2026-09-28
- Completed:

## Cross-Spec Notes

### What this spec supersedes

| Superseded requirement | Original spec | What changed |
|---|---|---|
| Per-phase `Artifact` subclasses (`SubtitleArtifact`, `AttachmentArtifact`, `ChaptersArtifact`, `TimestampArtifact`, `VideoStreamArtifact`, `AudioStreamArtifact`, `AudioArtifact`, `EncodedArtifact`, `MergeArtifact`) | `2026-09-09 artifact-state-refactor` (+ code drift since) | One generic `Artifact[PayloadT]` wrapper replaces every subclass. Identity and metadata live on the typed payload (stream-model entity); the wrapper adds only recovery/selection facts. |
| `Artifact.path` as a defining field | `2026-09-09 artifact-state-refactor` | Path is not what an artifact is about. The wrapper carries `payload` + `state` + `wanted`; file-backed locations are derived from the payload. Virtual artifacts no longer fake a path. |
| `PhaseResult.artifacts` as a wanted-only list assigned wholesale from recovery | `2026-09-09 artifact-state-refactor` | `artifacts` becomes a derived read-only concatenation of the result's declared typed fields (the external artifacts). Internal recovery rows never enter results. |
| "State phases" returning `Recovery(artifacts=[], pending=…)` (job, probe; chunking joined by drift) | code drift after `2026-09-25 file-stream-model` | Every phase emits a real artifact ledger: `File`, `ExtendedVideoStream` and chunk windows are artifacts (virtual or set-gated), uniformly recovered and reported. |
| Recovery line format `Recovery: N total, M unwanted — …` | `2026-09-09 artifact-state-refactor` (format) / TODO §49 (rewording) | Reworded, incorporated here: the `unwanted` count is replaced by the `wanted` count with the state counts grouped with it — `Recovery: 9 total, 8 wanted (3 complete, 0 partial, 5 absent) — resuming`. The mechanism (internal list in, counts derived internally, message returned) is retained. |
| `PhaseResult.error` as a second human-readable field | code (phase-object-model lineage) | Deleted (Req 6.6): 18 of 20 construction sites passed the identical string to both fields, and the only consumer coalesced them (`error or message`). `message` is the single string — on `FAILED` it is the error description. |
| Fake artifacts for reporting (`Artifact(path=Path(strategy_name))` in optimization) | code | Strategy test results are internal machinery. Optimization's ledger enumerates the winning attempts per (test chunk × strategy). |
| `TimestampArtifact` as a separate extraction artifact | `2026-04-29 pts-preservation` (as amended by later specs) | The per-frame PTS index is the video artifact's single expected material component, not a standalone artifact. |

### Related, not superseded

- `2026-09-25 file-stream-model` — the entity layer this spec wraps. Stream/chunk/attempt composition, naming ownership (Req 15), unique-property dumps and sidecar schemas are unchanged; this spec adds the artifact layer above them and defines what flows between phases.
- `2026-09-09 artifact-state-refactor` — retained in full: the completeness enum semantics (`ABSENT`/`PARTIAL`/`COMPLETE`, presence-based), the completeness × selection orthogonality, `wanted` as a value derived from external input, and the unified `log_recovery_line()` over the internal list.
- Thin-template phase run contract (`Phase.run()` steps, `Recovery`, `Recovery.from_artifacts`, dependency walk, dry-run branches) — unchanged; no new template hooks are introduced.

## Introduction

The file → stream model (`2026-09-25 file-stream-model`) rebuilt the entity layer — `File`, `Stream` family, `ExtendedVideoStream`, `VideoStreamChunk`, `EncodedChunk` — but left the artifact layer un-contracted. The result is a split brain: the same logical entity is represented twice (as a stream-model object and as an artifact subclass duplicating its identity fields), phases reconcile the two representations by hand, some phases abandoned artifacts entirely ("state phases"), optimization reports fake artifacts, and the runner discovers deliverables by sniffing path strings. The uniform flow — recovery as the single source of truth over artifacts, generic run decisions made on artifact facts — survived only in the template mechanics, not in the data model.

This spec re-contracts the artifact layer around the stream model:

1. **One generic wrapper** — `Artifact[PayloadT]`: a typed entity plus its recovery facts (`state`, `wanted`). Every per-phase artifact subclass is deleted.
2. **A complete internal recovery ledger** — every phase's `_recover()` enumerates every artifact it owns, wanted or not, external or internal. The ledger drives pending derivation, resumption and reporting.
3. **Typed result contracts** — a phase result declares explicit typed artifact fields; only artifacts a downstream consumer acts on appear there (one sanctioned exception: OptimizationPhase's winners, carried for uniformity — Req 5.2). Internal machinery does not leak.

An artifact is **the thing we act on and invest in**: winning encode attempts, audio outputs, extracted files, chunk windows, streams (zero-investment themselves, but the basis for downstream investment). Strategies are settings objects, not artifacts. Non-winning attempts are internal intermediates that make their winning-attempt artifact `PARTIAL`.

## Glossary

- **Artifact** — a typed entity (payload) wrapped with recovery facts. The unit of investment, recovery and inter-phase transfer.
- **Payload** — the stream-model entity an artifact wraps: `File`, `VideoStream`, `AudioStream`, `SubtitleStream`, `AttachmentStream`, `ExtendedVideoStream`, `VideoStreamChunk`, `EncodedChunk`, `Chapters`, `AudioOutput`, `MergedVideo`.
- **Recovery ledger** — the full, phase-internal artifact list built by `_recover()`: wanted and unwanted, external and internal rows. The single source of truth for pending derivation, resumption and the recovery line.
- **External artifact** — an artifact a downstream consumer (phase, runner, CLI, or the user as a deliverable) acts on; declared as a typed field on the phase result. One sanctioned exception exists (Req 5.2): OptimizationPhase's test winners are carried unconsumed, for uniformity.
- **Internal artifact** — a ledger row that never reaches the phase result; today these are the `wanted=False` rows (orphaned/surplus products), kept for honest reporting and cleanup visibility. The would-be example — optimization's test attempts — is instead carried in the result as the sanctioned exception (Req 5.2).
- **Settings subset** — a configuration-derived selection passed between phases as a plain typed field, never as artifacts (e.g. `selected_strategies`).
- **Material component** — a file whose presence defines an artifact's completeness when the payload itself is virtual (the video artifact's per-frame PTS index).

## Requirements

### Requirement 1 — Generic artifact wrapper

**User Story:** As a developer, I want a single generic artifact type parametrized over its typed payload, so that identity lives in exactly one place (the entity) and every generic mechanism (recovery, selection, reporting) works on one shape.

#### Acceptance Criteria

1. THE Pipeline SHALL define a single artifact class in `pyqenc/phase.py`:

    ```python
    @dataclass
    class Artifact[PayloadT]:
        payload: PayloadT
        state:   ArtifactState
        wanted:  bool = True
    ```

2. No subclass of `Artifact` SHALL exist anywhere in the codebase after migration; every artifact row and result field is a direct `Artifact[...]` instantiation with a concrete payload type.
3. `Artifact` SHALL NOT define a `path` field. File-backed locations SHALL be derived from the payload (e.g. `SubtitleStream.info.extracted_path`, `EncodedChunk.stream.file.path`, `MergedVideo`'s derived output path); virtual payloads have none.
4. The wrapper SHALL be mutable and SHALL NOT be persisted: state transitions (`→ COMPLETE` after production) are confined to the owning phase during `_execute()`. Sidecars persist payload info slices only, exactly as `file-stream-model` Req 5 defines; no artifact wrapper is ever serialized.
5. `ArtifactState` semantics, `wanted`-as-derived-external-input, and presence-based completeness (at most one directory listing per artifact group, uniform for unwanted rows) are inherited from `artifact-state-refactor` unchanged.

### Requirement 2 — Payload entity family

**User Story:** As a developer, I want every artifact payload to be an eager composition model in the stream-model family, so that payloads are instantiated once, typed statically and own their naming.

#### Acceptance Criteria

1. Payload types SHALL be: `File`, `VideoStream`, `AudioStream`, `SubtitleStream`, `AttachmentStream`, `ExtendedVideoStream`, `VideoStreamChunk`, `EncodedChunk` (all existing) plus three new models in `pyqenc/stream_model.py`: `Chapters`, `AudioOutput`, `MergedVideo`.
2. `Chapters` SHALL compose `file: File` (container-level — no track id, no selector, per file-stream-model Req 2.5); its file name is the fixed constant `chapters.xml` — not a generated name, nothing to pair (file-stream-model Req 15.10).
3. `AudioOutput` SHALL compose the source `AudioStream` with the producing chain's identity; its disk name follows the existing derivation — the stream's `safe_name()` plus `" chain=<name>.<ext>"` appended at the materialization site (file-stream-model Req 15.7) — with `display_name()` for logs and tables; resolved output facts (layout, codec) SHALL be accessible from the composition.
4. `MergedVideo` SHALL compose `strategy: Strategy` with the source identity needed for naming; its output name materializes at the existing single site from `File.path.stem` + the strategy's `safe_name()` (`<file stem> <strategy>.mkv`, file-stream-model Req 15.8), and it carries the measured facts consumers need (frame count, metrics, targets-met, plot path). The merge output directory SHALL rename `final/` → `merged/` (`FINAL_OUTPUT_DIR` → `MERGED_OUTPUT_DIR`), completing the past-participle output-dir family (`extracted/`, `encoded/`, `merged/`): `final` promised a fully finalized container (video + audio + subtitles + chapters) that the pipeline intentionally does not produce — the merged output is the merge phase's product, and end-user final assembly (chapters translation, audio selection, manual mux) happens outside the pipeline.
5. Named payloads SHALL follow the uniform two-name doctrine (file-stream-model Req 15.10): `display_name()` is the single generator, `safe_name()` serves the filesystem. The stream family, chunks and strategies already do; `AudioOutput` and `MergedVideo` join the pair; `Chapters` and the per-frame index are fixed-constant names (nothing to pair).
6. `stream_model.ContainerArtifact` SHALL be renamed `Chapters` (it is a payload entity, not a phase artifact), and `quality.QualityArtifacts` SHALL be renamed `QualityLogs` (transient `.tmp` metric logs, never artifacts).
7. No new name families are introduced: `Chapters` and the per-frame index are fixed constants, `AudioOutput` and `MergedVideo` use the existing derivation sites (file-stream-model Req 15.7/15.8); the existing round-trip property tests (Req 15.9) remain the trust basis for presence-based recovery.

### Requirement 3 — The video artifact: stream + per-frame index

**User Story:** As a developer, I want the per-frame PTS timestamps treated as the video artifact's material component rather than a standalone artifact, so that the artifact population reflects what actually exists: a virtual stream plus one extracted index that downstream phases act on.

#### Acceptance Criteria

1. The extraction phase's video artifact SHALL be one artifact: `Artifact[VideoStream]`. Its single expected material component is the per-frame PTS index (`extracted/timestamps.txt`, produced per video track via `mkvextract timecodes_v2`, with the ffprobe `packet=pts` fallback for non-MKV containers or an unavailable mkvextract).
2. The video artifact's state SHALL be `COMPLETE` iff the index file is present (non-empty), `ABSENT` otherwise. There is no `PARTIAL`: both producer paths write through the `.tmp`-then-rename protocol (the file-trust rule, file-stream-model Req 7.7) — presence at the final name always implies a complete, successful write. The virtual stream's existence in the source is a precondition of the row existing, not a state.
3. The video artifact's `wanted` SHALL be `video_required` (pipeline mode) — deliberately NOT the include/exclude stream filter, matching current behavior where timestamps extraction is gated only by mode. (The remaining scope of include/exclude is a separate question — TODO §48.)
4. `TimestampArtifact` (and any `Timestamps` entity) SHALL NOT exist. The index file-name/location convention SHALL have exactly one owning site.
5. `ExtractionPhaseResult` SHALL expose the index path as a derived property of the video artifact (`None` when the component is absent); ProbePhase (frame count) and MergePhase (PTS restoration) consume it through that contract.

### Requirement 4 — Internal recovery ledger, complete in every phase

**User Story:** As a user, I want every phase's recovery to enumerate and classify every artifact it owns — including rows no downstream phase consumes — so that recovery is the single source of truth and reporting is honest.

#### Acceptance Criteria

1. Every phase's `_recover()` SHALL return a `Recovery` whose artifact list contains one row per artifact the phase owns: wanted and unwanted, external and internal. `Recovery.from_artifacts` semantics are unchanged.
2. The former "state phases" SHALL emit real ledgers: JobPhase → one `Artifact[File]` row (`COMPLETE` by construction once the source is verified); ProbePhase → one `Artifact[ExtendedVideoStream]` row (`COMPLETE` iff `probe.yaml` is current); ChunkingPhase → one `Artifact[VideoStreamChunk]` row per chunk, all rows sharing one state (`ABSENT` while boundaries are absent, `COMPLETE` once the persisted scene boundaries are current — the set flips together because chunks derive wholly from the sidecar).
3. Non-winning encode attempts, scene boundaries, per-strategy aggregated test results, sidecar YAMLs and run parameters (work dir, force, config, cleanup) SHALL NOT appear as ledger rows. Non-winning attempts drive the winning artifact's `PARTIAL` state (protected investment: attempts exist, no winner finalized); the rest are settings, state or inputs.
4. Surplus/orphaned on-disk products whose producer no longer selects them (e.g. an `encoded/<strategy>/` directory for a strategy no longer configured) SHALL appear as `wanted=False` rows — retained in place, deletion only via explicit cleanup, exactly as the artifact-state contract defines.
5. The ledger SHALL remain phase-internal except for what Req 5 places in results; `log_recovery_line()` continues to receive the internal list (it is the only place the unwanted count is visible).

### Requirement 5 — External artifacts and the consumption-graph rule

**User Story:** As a developer, I want a phase result to carry exactly the artifacts downstream consumers act on — no more — so that results are contracts and internal machinery does not leak.

#### Acceptance Criteria

1. An artifact is **external** iff a downstream consumer (phase, runner, CLI, or the user as a deliverable) acts on it; otherwise it is **internal** (ledger-only). This is determined by the pipeline's consumption graph, not chosen per run by the phase. The term "exposed" SHALL NOT be used.
2. Each `PhaseResult` subclass SHALL declare its external artifacts as explicit typed fields with concrete payload types (Req 6). OptimizationPhase is the **single sanctioned exception**: its result carries `winners: list[Artifact[EncodedChunk]]` (the test winning attempts) even though no downstream consumer acts on them — carried for uniformity of artifact/result handling, so the phase keeps EncodingPhase's result shape and the base run mechanics (dry-run preview, pending, immediate reuse on nothing pending) with no special cases anywhere in the base machinery. The exception is documented here and in the result class's docstring. `strategy_results` is still deleted from the result (no consumer; the aggregated data persists in `optimization.yaml`); `selected_strategies: list[Strategy]` remains the settings subset Encoding and Merge consume.
3. Strategies SHALL flow between phases as settings objects (typed fields), never as artifacts. Optimization reduces the set; Encoding and Merge consume the reduced set.
4. The invariant "a winning encode result exists for each (chunk, strategy) pair" SHALL be guaranteed by outcome semantics plus the ledger (a `COMPLETED`/`REUSED` encoding result implies every wanted pair `COMPLETE`; MergePhase's post-dependency guard verifies), independent of the result's field shape.

### Requirement 6 — Phase results: typed fields as contract and storage

**User Story:** As a developer, I want explicit typed artifact fields on phase results as the single storage of external artifacts, so that the contract is static, duplicates are impossible and internal rows cannot leak.

#### Acceptance Criteria

1. Each `PhaseResult` subclass SHALL declare its external artifacts as explicit fields with concrete parametrizations, e.g. `ExtractionPhaseResult.video_stream: Artifact[VideoStream] | None`, `.audio_streams: list[Artifact[AudioStream]]`, `.chapters: Artifact[Chapters] | None`. Run parameters and settings fields remain plain typed fields.
2. The inherited `PhaseResult.artifacts` SHALL become a derived, read-only concatenation of the declared artifact fields (in declaration order). `complete`, `pending`, `is_complete` and `did_work` keep their semantics over the derived list.
3. `_make_result(outcome, wanted_rows, message)` SHALL place wanted rows into the declared fields by payload type; there SHALL be no list-shaped artifact field to dump rows into, and no zip/reconciliation between representations (the extraction result's stream/payload rebuild dance is deleted).
4. Mirror/duplicate fields SHALL be deleted: `AudioPhaseResult.outputs` becomes the single storage `outputs: list[Artifact[AudioOutput]]` (with `audio_files` derived); `MergePhaseResult.merged` becomes `merged: list[Artifact[MergedVideo]]` as the single storage; `EncodingPhaseResult.encoded` + `encoded_chunks` collapse into one typed field of `Artifact[EncodedChunk]` (lookup helpers may be derived).
5. The template `Phase.run()` SHALL be unchanged: the wanted filter, `_execute(wanted)` work list, `_reused_result` default and dry-run branch operate exactly as today. No exposure hook or template special case is introduced — results receive only what their declared fields name, so internal rows have no path into a result.
6. `PhaseResult` SHALL carry a single human-readable string: the `error` field is deleted, and on `PhaseOutcome.FAILED` the `message` IS the failure description. Partial-failure detail folds into one string (count plus identifiers, e.g. `"3 pair(s) failed: <ids>"`). Rationale (verified): 18 of 20 construction sites pass the identical string to both fields; the two divergent sites (encoding/merge partial failures) split summary vs detail, but the only consumer — the runner — coalesces with `error or message`, discarding the shorter summary; the phases log the detailed lists regardless. The runner derives `RunResult.error` from the target result's `FAILED` outcome plus `message`; the fallback chain dies.

### Requirement 7 — Per-phase artifact contracts

**User Story:** As a developer, I want each phase's ledger contents and external contract stated in one place, so that the uniform flow is auditable.

#### Acceptance Criteria

THE Pipeline SHALL implement the following contracts (ledger = internal rows built in `_recover()`; external = result fields):

| Phase | Ledger rows (internal, complete) | External contract (result fields) |
|---|---|---|
| Job | `Artifact[File]` × 1 | `file: Artifact[File]`; run parameters as plain fields |
| Extraction | `Artifact[VideoStream]` (mode-wanted, index-completeness), `Artifact[AudioStream]` per track (virtual, `COMPLETE` by construction, filter-wanted for display), `Artifact[SubtitleStream]` / `Artifact[AttachmentStream]` per track (file-backed, filter-wanted), `Artifact[Chapters]` when the source has chapters | `video_stream`, `audio_streams`, `subtitle_streams`, `attachment_streams`, `chapters`; derived `timestamps_path`, `chapters_path` |
| Probe | `Artifact[ExtendedVideoStream]` × 1 | `stream: Artifact[ExtendedVideoStream]`; derived `crop` |
| Chunking | `Artifact[VideoStreamChunk]` × N (set-flip semantics, Req 4.2) | `chunks: list[Artifact[VideoStreamChunk]]` |
| Optimization | `Artifact[EncodedChunk]` per (test chunk × strategy) winning attempt (Req 8) + orphaned-strategy `wanted=False` rows | `winners: list[Artifact[EncodedChunk]]` (sanctioned exception — carried for uniformity, unconsumed downstream) + `selected_strategies: list[Strategy]` |
| Encoding | `Artifact[EncodedChunk]` per (chunk × selected strategy) winner (`PARTIAL` while attempts exist without a winner) + orphaned-strategy `wanted=False` rows | winners `list[Artifact[EncodedChunk]]` (consumed by Merge); `quality_labels` (settings) |
| Merge | `Artifact[MergedVideo]` per expected output (selected strategies); `PARTIAL` when the output exists without its sidecar | `merged: list[Artifact[MergedVideo]]` (consumed by runner as deliverables) |
| Audio | `Artifact[AudioOutput]` per expected (track, chain) output + surplus files as `wanted=False` rows | `outputs: list[Artifact[AudioOutput]]` (deliverables; future merge consumption) |

### Requirement 8 — Optimization ledger honesty

**User Story:** As a user, I want the optimization recovery line to count the artifacts that actually must be produced, so that the report matches reality.

#### Acceptance Criteria

1. Optimization's ledger SHALL contain one row per (test chunk × strategy) winning attempt. For 3 test chunks and 3 strategies the recovery line SHALL read `Recovery: 9 total, 9 wanted (0 complete, 0 partial, 9 absent) — full run needed` on a fresh run — not `3 total` as the per-strategy aggregation reports today.
2. Row states SHALL be per-pair and presence-based (the shared attempt-recovery machinery already provides this). Internal derivation MAY aggregate from `optimization.yaml` per-strategy records, but a row SHALL be `COMPLETE` only when that pair's winner actually exists on disk.
3. `optimization.yaml` KEEPS its current role unchanged (test-chunk selection, per-strategy aggregated sizes, tolerance, selection, invalidation) — it is settings/state, not artifacts.

### Requirement 9 — Typed downstream consumption

**User Story:** As a developer, I want downstream consumers reading typed payloads instead of artifact-subclass fields or path conventions, so that the composition model is the single source of identity.

#### Acceptance Criteria

1. MergePhase's CRF plot SHALL read winning attempts via `Artifact[EncodedChunk]` payloads (`payload.crf`, `payload.chunk.start_timestamp` / `end_timestamp`). The hand-rolled timestamp re-parser (`_parse_ts`) is deleted — chunk-id parsing belongs to `VideoStreamChunk.parse_chunk_id` (file-stream-model Req 15.1).
2. MergePhase's post-dependency guard SHALL verify encoding completeness from the typed winners field.
3. The runner's output-file collection SHALL read the merge result's `Artifact[MergedVideo]` payloads (complete rows' derived paths). The `"final" in artifact.path.parts` directory-sniffing is deleted.
4. The extraction stream table SHALL derive row names from payload `display_name()` with no per-artifact-type dispatch; the Want/Present columns continue to read `wanted`/`state` only.
5. No consumer SHALL depend on an artifact subclass field, a stringly-typed identity copy, or a path-shape convention to find pipeline products.

### Requirement 10 — Uniform recovery reporting

**User Story:** As a user, I want every phase to report its recovery uniformly, so that run logs read the same way end to end.

#### Acceptance Criteria

1. Every phase, including Job, Probe and Chunking, SHALL emit the standard recovery line — their ledgers are non-empty by Req 4.2, so the template's existing condition (`if recovery.artifacts`) holds without change.
2. `log_recovery_line()` SHALL keep its mechanism — internal ledger in, every count derived internally, the emitted string returned as `PhaseResult.message` — with the output reworded: the `unwanted` count is replaced by the `wanted` count and the state counts are grouped with it, e.g. `Recovery: 9 total, 8 wanted (3 complete, 0 partial, 5 absent) — resuming`. The identity `wanted == complete + partial + absent` SHALL always hold; `total` still counts every internal row (internal artifacts and `wanted=False` rows included).
3. `_recovery_unit()` remains the per-phase noun hook (e.g. `"pair"`, `"chunk"`; optimization's becomes the attempt noun).

---

## Correctness Properties (summary — detailed in design)

1. No subclass of `Artifact` exists; every row/field is `Artifact[PayloadT]` with a concrete payload type.
2. Every artifact in a result's derived `artifacts` comes from exactly one declared field, and its payload type matches that field's annotation.
3. The video artifact's state equals the index file's presence; its `wanted` equals `video_required`.
4. All chunk rows in one chunking ledger share the same state.
5. Optimization's ledger size equals |test chunks| × |strategies|; row states are per-pair.
6. Every phase logs a recovery line; its counts derive from the phase's internal ledger, and the line's `wanted` count equals `complete + partial + absent`.
7. Presence-based completeness only (inherited): no artifact is classified by anything but on-disk component presence.
