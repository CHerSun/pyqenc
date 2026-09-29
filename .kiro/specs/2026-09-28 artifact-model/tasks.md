# Implementation Plan — Artifact Model: Generic Recovery & Contract Layer

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-28
- Completed:

## Overview

Staged so every task lands green: payload entities land first (purely additive), the core wrapper + reporting swap lands second (mechanical, no behavior change), then phases migrate in dependency order — each building generic ledger rows and typed result fields — and the derived-`artifacts` property + dead-code sweep closes the migration. Requirement ids reference `requirements.md`.

## Notes

- After each code task run `uv run ruff check .` and the relevant `uv run python -m pytest`.
- Transitional coexistence is allowed *between* tasks, never in the final state: while phases migrate one by one, each phase's `_make_result` still populates the (still real) `artifacts` field from its typed fields, so generic consumers (runner, merge's guard) stay fed at every stage; Task 9 swaps the field for the derived property and deletes the transitional population.
- Tests target observable behavior; each test names the bug it prevents. Property tests map to the design's Correctness Properties 1–7.
- E2E on real media (`D:\_encoding\source\*.mkv`, `--work-dir D:\_encoding\pyqenc_tmp`, `--strategies "h265*+ultrafast"` — never slow presets) after Tasks 3, 8 and 10; the reuse-run (second invocation on the same work dir) is part of every e2e check.
- Baseline note: naming follows the two-name doctrine landed with file-stream-model's closeout (`display_name()`/`safe_name()`; disk names derive from display; `chunk_id`/`Strategy.name` accessors are gone). No new name families are created here.

## Tasks

- [ ] 1. Payload entities + renames (Req 2)
  - `stream_model.py`: add `Chapters(file: File)` (renaming/replacing `ContainerArtifact` — update `ExtractionSidecar.chapters`), `AudioOutput(stream: AudioStream, chain_name: str)` and `MergedVideo(strategy: Strategy, source identity, frame_count, metrics, targets_met, plot_path)` — eager frozen composition models, `model_dump(exclude_none=True)`/`model_validate` only
  - Two-name doctrine (Req 2.5, file-stream-model Req 15.10): `AudioOutput` and `MergedVideo` expose `display_name()`/`safe_name()`; `Chapters`' file name is the fixed constant `chapters.xml` (nothing to pair)
  - `quality.QualityArtifacts` → `QualityLogs` (rename, sweep references)
  - Unit tests: composition round-trips; disk-name derivation via the existing sites (stream `safe_name` + `" chain=<name>.<ext>"`; `File.path.stem` + strategy `safe_name()`); fixed constants pinned

- [ ] 2. Core wrapper + reporting swap, no behavior change (Req 1, 6.6, 10)
  - `phase.py`: `Artifact` → generic dataclass `Artifact[PayloadT](payload, state, wanted=True)`; no `path` field; docstrings re-contracted (ledger vocabulary). Surviving per-phase subclasses mechanically redeclare their extra fields (`path` etc.) so all construction sites keep working — deleted with their phase's task
  - `log_recovery_line` rewording (Req 10.2): `Recovery: 9 total, 8 wanted (3 complete, 0 partial, 5 absent) — resuming`; `wanted == complete + partial + absent` identity; `unwanted` count dropped
  - `PhaseResult.error` deleted (Req 6.6): drop `error=` at every construction site; the two divergent sites (encoding/merge partial failures) fold count + identifiers into one `message`; runner's `error or message` fallback chain becomes outcome+message derivation — done here so later tasks never see the old kwarg
  - Update `test_phase_result.py`, `test_phase.py`, recovery-line tests

- [ ] 3. ExtractionPhase (Req 3, 4.1/4.4, 7-row, 9.4)
  - Delete the six artifact classes + `ExtractionArtifact` alias; ledger rows become `Artifact[VideoStream|AudioStream|SubtitleStream|AttachmentStream|Chapters]`
  - Video fold (Req 3): one `Artifact[VideoStream]` row, `COMPLETE` ⇔ index present, `wanted = video_required` (never the filter); `TimestampArtifact` machinery merges into the video row's extractor path; `timestamps.txt` name/location gets its single owning site; `timestamps_path`/`chapters_path` become derived properties
  - `ExtractionPhaseResult` typed fields (Req 7): `video_stream: Artifact[VideoStream] | None`, `audio_streams`, `subtitle_streams`, `attachment_streams`, `chapters`; `_make_result` sorts rows into fields — the zip/`model_copy` reconciliation dance is deleted; transitional `artifacts` population from the fields
  - Stream table generic (Req 9.4): row names from payload `display_name()` — no per-artifact-type dispatch
  - Update `test_extraction_pts.py`, `test_extraction_streams_filter.py`, stream-table tests; e2e smoke + reuse-run (first recovery-line + table check)

- [ ] 4. Job + Probe + Chunking ledgers — the "state phases" die (Req 4.2, 7-rows)
  - Job: `Artifact[File]` row (`COMPLETE` by construction); `JobPhaseResult.file: Artifact[File] | None`
  - Probe: `Artifact[ExtendedVideoStream]` row (`COMPLETE` ⇔ `probe.yaml` current); `ProbePhaseResult.stream: Artifact[ExtendedVideoStream] | None` (`crop` stays derived)
  - Chunking: `Artifact[VideoStreamChunk]` row per chunk, set-flip semantics (all `ABSENT` while boundaries absent, all `COMPLETE` when current); `ChunkingPhaseResult.chunks: list[Artifact[VideoStreamChunk]]`
  - `Recovery` docstring: drop the stale "(job, probe)" carve-out — every phase has artifacts now
  - Verify: every phase logs the recovery line (previously silent phases included)
  - Update `test_job_phase.py`, `test_probe_phase.py`, `test_chunking.py`

- [ ] 5. OptimizationPhase (Req 5.2, 8, 7-row)
  - Per-pair ledger: `Artifact[EncodedChunk]` per (test chunk × strategy), states from the shared `_recover_encoding_attempts` (or sidecar aggregation where a row is `COMPLETE` only with its winner on disk); delete `_strategy_artifacts` and its fake paths
  - Recovery line counts attempts: the 3×3 e2e case reads `9 total, 9 wanted (0 complete, 0 partial, 9 absent)` — pinned as a regression test
  - Result: `winners: list[Artifact[EncodedChunk]]` (the sanctioned exception — docstring says so) + `selected_strategies`; `strategy_results` deleted from the result (persists in `optimization.yaml` only); orphaned-strategy products surface as `wanted=False` rows
  - `_recovery_unit()` → the attempt noun
  - Update `test_optimization_phase.py`

- [ ] 6. EncodingPhase (Req 7-row, 5.4)
  - Delete `EncodedArtifact`; winners are `Artifact[EncodedChunk]` rows; `EncodingPhaseResult` collapses `encoded` + `encoded_chunks` into one typed winners field (+ derived lookup where callers want the dict)
  - `PARTIAL` stays: attempts-without-winner rows (protected investment) — unchanged mechanism, now over generic rows
  - `quality_labels` stays a settings field
  - Update `test_encoding_phase.py`, `tests/integration/test_encoding_quality.py`

- [ ] 7. AudioPhase (Req 7-row)
  - Delete `AudioArtifact`; outputs are `Artifact[AudioOutput]` rows (payload composes stream + chain); `AudioPhaseResult.outputs: list[Artifact[AudioOutput]]` is the single storage, `audio_files` derived
  - Surplus files stay `wanted=False` rows (unchanged behavior)
  - Update `test_audio_phase.py`

- [ ] 8. MergePhase + runner + `merged/` rename (Req 2.4, 9.1–9.3, 7-rows)
  - Delete `MergeArtifact`; outputs are `Artifact[MergedVideo]` rows; `MergePhaseResult.merged` is the single storage; sidecar-summary role stays with `merge.yaml`
  - CRF plot reads payloads (`payload.crf`, `payload.chunk.start/end_timestamp`); delete `_parse_ts` (chunk-id parsing belongs to `VideoStreamChunk.parse_chunk_id`)
  - Post-dependency guard reads the typed winners field
  - `final/` → `merged/`: `FINAL_OUTPUT_DIR` → `MERGED_OUTPUT_DIR`; sweep merge internals, docs, tests
  - Runner: `_collect_output_files` reads `Artifact[MergedVideo]` payloads — the `"final" in path.parts` sniff dies
  - Update `test_merge_mkvmerge.py`, runner tests; e2e smoke + reuse-run (deliverables collected from `merged/`)

- [ ] 9. Derived-`artifacts` swap + dead-code sweep (Req 6.2, 6.3, 5)
  - `PhaseResult.artifacts` field → derived read-only property (dataclass-fields introspection, Artifact-typed fields only, declaration order); delete every transitional `artifacts` population; `complete`/`pending`/`is_complete`/`did_work` operate on the derived list
  - Verify no subclass of `Artifact` exists project-wide; no `path=` construction of bare artifacts; no `error=` anywhere; no zip/reconciliation remnants
  - Property tests (design Correctness Properties): result/field correspondence incl. payload-type match; internal rows never in results; ledger completeness per phase
  - `uv run ruff check .`; full `uv run python -m pytest`

- [ ] 10. Docs + full e2e verification
  - `docs/architecture.md`: rewrite the artifact-states section for the generic model; uniform recovery reporting; `merged/` in the flow overview
  - Full e2e on real media + reuse-run: every phase logs the recovery line; optimization counts attempts; extraction table derives from payloads; merged outputs land in `merged/`; invariant chain intact (source == Σ attempts == final)
  - Sweep TODO §4/§37 leftovers if the migration erased their target sites (extraction ruff items, mid-file imports)

- [ ] 11. Cross-spec review + TODO reconciliation
  - Review this spec against `2026-09-09 artifact-state-refactor` (successor note already recorded) and `2026-09-25 file-stream-model` (related-successor note already recorded); verify both summaries still match what landed
  - Archive `2026-09-09 artifact-state-refactor` → `_archive/` (successor recorded, tasks complete)
  - Prune/annotate TODO entries covered here; confirm §4/§37 status

- [ ] 12. Finalize
  - Update `- Completed:` date in all three files
