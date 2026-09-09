# Requirements Document

Artifact State Refactor

<!-- markdownlint-disable MD024 -->

- Created: 2026-09-09
- Completed: 2026-09-10

## Cross-Spec Notes

This spec partially supersedes every earlier spec that described the four-value `ArtifactState`
(`ABSENT`/`ARTIFACT_ONLY`/`STALE`/`COMPLETE`) or per-phase recovery-message logic:

| Spec | Created | Relationship |
|------|---------|--------------|
| `phase-recovery-refactor` | 2026-03-17 | **Superseded in part.** Introduced `ArtifactState` as `ABSENT`/`ARTIFACT_ONLY`/`COMPLETE`. This spec renames `ARTIFACT_ONLY` → `PARTIAL` and re-scopes the enum to completeness only. |
| `phase-object-model` | 2026-03-20 | **Superseded in part.** Defined the four-value enum (Req 5 added `STALE`; `pending` included `STALE`). This spec removes `STALE` entirely — invalidation now uses `wanted=False` with the correct completeness. |
| `pts-preservation` | 2026-04-29 | **Superseded in part.** Its glossary/design describe the four-value enum and `TimestampArtifact` states. `ARTIFACT_ONLY` → `PARTIAL` and `STALE` removal apply here too; `TimestampArtifact` remains `COMPLETE`/`ABSENT` (now with `wanted`). |
| `project-cleanup` | 2026-06-11 | **Completed here.** Its Section 7/8.3 flagged the duplicated `_recovery_message()` pattern across phases. This spec removes all five per-phase `_recovery_message()` helpers and unifies reporting in `log_recovery_line()`. |

What this spec changed relative to those earlier specs:

- Removed the `STALE` `ArtifactState` value (selection now lives in `Artifact.wanted`).
- Renamed `ARTIFACT_ONLY` → `PARTIAL`.
- Introduced `Artifact.wanted: bool = True` to carry selection separately from completeness.
- Unified recovery reporting through `log_recovery_line()` and removed the per-phase `_recovery_message()` helpers.

Not affected: `merge-phase-revamp`, `probe-phase-refactor`, and `config-refactor` do not reference the
`ArtifactState` enum or per-phase recovery-message logic.

## Introduction

`ArtifactState` currently conflates two orthogonal concepts into a single four-value enum:

- **completeness** — are all of the artifact's expected components present, so that the artifact is ready to be worked on by later stages?
- **wanted status** — is the artifact selected by the current run, as derived from external input (stream filters plus pipeline mode for extraction, scene detection for chunking)?

These two concepts are independent. Completeness is computed the same way for every artifact regardless of whether it is wanted. Wanted is a derived value: it is decided by external input, never chosen or mutated by a phase on its own.

`STALE` is the primary symptom: it simultaneously encodes "the file is on disk" (a completeness fact) and "it is no longer wanted under the current parameters" (a selection decision). This coupling forces callers to interpret `STALE` as both "present but skip" and "unwanted", making recovery logic harder to reason about and stream-table rendering more indirect than it needs to be.

The completeness check is cheap by design: it relies on at most a single directory listing per artifact group and then checks whether each artifact's expected components appear in that listing. Because it costs almost nothing, completeness is computed uniformly for every artifact — wanted and unwanted alike. Wanted status affects only the action taken after the check, not the check itself: a phase never attempts recovery or work on an unwanted artifact.

`ARTIFACT_ONLY` is a secondary issue: the name describes the physical symptom (only the artifact file is present, sidecar is missing) rather than the abstract state (the artifact is partially complete). `PARTIAL` is clearer.

This spec defines requirements for splitting the concept cleanly:

1. `ArtifactState` covers only **completeness** — three values: `ABSENT`, `PARTIAL`, `COMPLETE`.
2. `Artifact.wanted: bool` captures **selection** — whether this artifact should be produced in the current run.
3. All `STALE` usages are replaced by `wanted=False` with the correct completeness.
4. Downstream phase APIs and derived properties (`pending`, `complete`) filter on `wanted=True`.
5. The stream table in `ExtractionPhase` is derived purely from the internal artifact list, with no separate `all_tracks` parameter.

The codebase is pre-alpha; no backward-compatibility constraints apply.

## Glossary

- **ArtifactState**: Enum in `pyqenc/state.py` classifying the completeness of a single artifact file.
- **Artifact**: Base dataclass in `pyqenc/phase.py`; the common representation of all phase outputs. Every phase defines a concrete subclass (e.g. `VideoArtifact`, `AudioArtifact`, `ChunkArtifact`, `MergeArtifact`).
- **wanted**: Boolean field on `Artifact` indicating whether the artifact is selected by the current run. It is a derived value: it comes from external input — the user's stream filter plus pipeline mode (e.g. video_required) for extraction, and scene detection for chunking — and is never chosen or mutated by a phase on its own during recovery. Orthogonal to completeness. `True` = must be produced if not already `COMPLETE`; `False` = present or expected on disk but not needed by the current run. The artifact is retained in place regardless; it is not a deletion candidate — deletion only ever happens when the user explicitly sets a cleanup level, applied uniformly.
- **ABSENT**: Completeness state — the artifact's components are not present. Either nothing has been produced yet, or whatever exists is trivially reproducible with no investment worth protecting. There is no separate state for cheaply reproducible leftovers; they are simply `ABSENT`.
- **PARTIAL**: Completeness state — a protected investment. Expensive or valuable work is partly done, but the artifact is not yet ready to be worked on by later stages because a required component is missing. It is kept to avoid discarding that investment and to allow resuming. Examples of the principle: an extracted stream file is present but its sidecar is missing (the extracted stream is a large investment worth keeping, yet without the sidecar it is not ready for later stages); encoding CRF attempts exist but no winning attempt has been finalised. Error mapping: a failed attempt that left no file is `ABSENT`; a failed attempt that left a valuable-but-incomplete result is `PARTIAL`; a suboptimal result (e.g. encoding accepted best-effort when quality targets were not reachable) is `COMPLETE` — the artifact is fully produced and ready, just not optimal. Replaces `ARTIFACT_ONLY`. `.tmp` files are NOT `PARTIAL` — they are transient crash remnants cleaned up at phase startup before recovery runs.
- **COMPLETE**: Completeness state — all of the artifact's expected components are present, so the artifact is fully ready to be worked on by later stages (sidecar present where required).
- **STALE**: Former combined value encoding both "present on disk" and "no longer wanted". Removed by this spec; replaced by `wanted=False` with the appropriate completeness.
- **internal artifact list**: The full list of all artifacts produced by `_recover()` inside a phase, covering both wanted and unwanted artifacts. The single source of truth for that phase's completeness.
- **ExtractionPhase**: Phase in `pyqenc/phases/extraction.py` responsible for extracting all streams from the source MKV.
- **AudioPhase**: Phase in `pyqenc/phases/audio.py` responsible for audio processing and delivery.
- **ExtractionArtifact**: Type alias for `VideoArtifact | AudioArtifact | OtherArtifact | TimestampArtifact` — all artifact types produced by `ExtractionPhase`.
- **stream table**: The `info`-level log table emitted by `_log_stream_table()` showing wanted/present status for each stream in the source file.
- **PhaseResult**: Dataclass in `pyqenc/phase.py` returned by every phase's `scan()` / `run()`. Carries `artifacts`, `outcome`, `message`, and `error`. Exposes derived properties `pending`, `complete`, `is_complete`, and `did_work`.
- **log_recovery_line**: Helper in `pyqenc/utils/log_format.py` that emits a formatted recovery summary log line. Under this spec it becomes the single unified recovery-reporting helper: it accepts a phase's internal artifact list (the full list, including `wanted=False` entries) plus a `unit` label, derives all counts itself, and emits one `info` line reporting total, unwanted, complete, partial, and absent counts (complete/partial/absent counted over wanted artifacts only), always showing all five counts even when zero.

## Requirements

### Requirement 1: Split ArtifactState into completeness only

**User Story:** As a developer, I want `ArtifactState` to represent only completeness, so that selection logic is not mixed into completeness comparisons.

#### Acceptance Criteria

1. THE `ArtifactState` enum SHALL contain exactly three values: `ABSENT`, `PARTIAL`, and `COMPLETE`.
2. THE `ArtifactState.STALE` value SHALL be removed from the enum.
3. THE `ArtifactState.ARTIFACT_ONLY` value SHALL be renamed to `ArtifactState.PARTIAL`.
4. THE `ArtifactState.ABSENT` value SHALL mean that the artifact's components are not present — either nothing has been produced yet, or whatever exists is trivially reproducible with no investment worth protecting. `.tmp` files are NOT `PARTIAL` — they are transient crash remnants cleaned up at phase startup before recovery runs, so their presence leaves the artifact `ABSENT`.
5. THE `ArtifactState.PARTIAL` value SHALL represent a protected investment: expensive or valuable work is partly done, but the artifact is not yet ready to be worked on by later stages because a required component is missing, and it is kept to avoid discarding that investment and to allow resuming. An extracted stream file present without its sidecar, and CRF attempts present without a finalised winning attempt, are examples of this principle rather than its definition.
6. THE `ArtifactState.COMPLETE` value SHALL mean that all of the artifact's expected components are present, so the artifact is fully ready to be worked on by later stages (sidecar present where applicable).
7. THE completeness state of an artifact SHALL be determined by a cheap check: at most a single directory listing per artifact group (for encoding, at most the winning and current attempt directories), followed by checking whether each artifact's expected components are present in that listing; the check SHALL NOT perform per-artifact individual scanning or `stat` calls.
8. THE completeness state SHALL be computed for every artifact regardless of its `wanted` status, and the check SHALL NOT be refined or skipped based on `wanted`.
9. THE `state.py` module docstring and `ArtifactState` class docstring SHALL be updated to reflect the three-value model and the removal of `STALE`.

### Requirement 2: Add `wanted: bool` to `Artifact`

**User Story:** As a developer, I want every artifact to carry an explicit `wanted` flag, so that selection status is always co-located with completeness and I never need to infer it from the state value.

#### Acceptance Criteria

1. THE `Artifact` base dataclass SHALL gain a `wanted: bool` field with a default value of `True`.
2. THE `wanted` field SHALL be a derived value, computed from external input (stream filters plus pipeline mode for extraction, scene detection for chunking); a phase SHALL NOT choose or mutate `wanted` on its own during recovery.
3. WHEN `wanted` is `True`, THE `Artifact` SHALL represent an artifact that is selected by the current run — it must be produced if not already `COMPLETE`.
4. WHEN `wanted` is `False`, THE `Artifact` SHALL represent an artifact that is present or expected on disk but is not selected for this run — it must not be produced, and it SHALL be retained in place unchanged; being unwanted SHALL NOT make the artifact a deletion candidate.
5. THE default value of `True` SHALL ensure that all existing artifact construction sites that do not explicitly set `wanted` continue to behave as before, requiring no changes at those callsites.
6. THE `phase.py` module docstring and `Artifact` class docstring SHALL be updated to document the `wanted` field, its derived nature, and its semantics.

### Requirement 3: Replace all `STALE` usages with `wanted=False`

**User Story:** As a developer, I want all filter-change and codec-change invalidation logic to set `wanted=False` rather than `STALE`, so that completeness and selection status are expressed through the correct fields.

#### Acceptance Criteria

1. WHEN `ExtractionPhase._recover()` detects a filter change (persisted include/exclude patterns differ from current patterns), files on disk that do not match the current filter SHALL be classified with `wanted=False` and `state=COMPLETE`.
2. WHEN `ExtractionPhase._recover()` detects a filter change, files on disk that do match the current filter SHALL be classified with `wanted=True` and `state=COMPLETE`.
3. WHEN `AudioPhase._recover()` detects a codec or bitrate change (persisted `audio.yaml` codec differs from the current config), audio delivery files on disk produced under the previous codec SHALL be classified with `wanted=False` and `state=COMPLETE`.
4. WHEN `AudioPhase._recover()` finds existing audio delivery files that are no longer in the current `terminal_outputs` set (their planned output path has changed), those files SHALL be classified with `wanted=False` and `state=COMPLETE`.
5. IF any other phase or helper currently assigns `ArtifactState.STALE` to an artifact, THEN that assignment SHALL be replaced with `wanted=False` and the appropriate completeness (`COMPLETE`, `PARTIAL`, or `ABSENT`).
6. THE `_execute_extraction()` method in `ExtractionPhase` SHALL retain unwanted artifacts in the final artifact list (they remain on disk) with `wanted=False` and their correct completeness, so that they are represented accurately in recovery and the stream table; they remain in place unchanged and are subject only to the user-configured cleanup level, if any.
7. WHERE an artifact is `wanted=False`, THE phase SHALL NOT attempt recovery or any work on that artifact; the artifact SHALL still receive a completeness value from the same cheap listing, but SHALL never be acted upon.
8. WHERE an artifact is `wanted=True` and not `COMPLETE`, THE phase SHALL perform the recovery action for that artifact — attempt full recovery first, then salvage partial work if full recovery fails, then plan the remaining work.

### Requirement 4: Phase results expose only wanted artifacts to downstream phases

**User Story:** As a developer, I want the artifacts exposed in `PhaseResult` to downstream phases to contain only wanted artifacts, so that callers never need to filter by `wanted` themselves.

#### Acceptance Criteria

1. THE `PhaseResult.artifacts` list SHALL contain only artifacts where `wanted=True`. A phase builds a full internal artifact list (including `wanted=False` entries) during `_recover()`, then filters to `wanted=True` before constructing `PhaseResult`.
2. THE `PhaseResult.pending` and `PhaseResult.complete` derived properties operate on `PhaseResult.artifacts` unchanged — since artifacts already contains only wanted entries, no additional `wanted` filtering is needed in those properties.
3. THE `PhaseResult.is_complete` derived property SHALL continue to be based on `outcome`, not the artifact list, and SHALL remain unchanged.

### Requirement 5: Stream table is derived from the internal artifact list

**User Story:** As a developer, I want the stream table logged by `ExtractionPhase` to be derived entirely from the internal artifact list, so there is one source of truth for stream state and the `all_tracks` parameter is no longer needed.

#### Acceptance Criteria

1. THE `_log_stream_table()` function signature SHALL be changed to accept only the internal artifact list (`list[ExtractionArtifact]`) — the separate `all_tracks: list[StreamBase]` parameter SHALL be removed.
2. THE `_log_stream_table()` function SHALL derive the "wanted" column value from `artifact.wanted`: `True` maps to `✔`, `False` maps to `✘`.
3. THE `_log_stream_table()` function SHALL derive the "present" column value solely from `artifact.state` (completeness), independent of `wanted`: `COMPLETE` maps to `✔`; `ABSENT` or `PARTIAL` maps to `✘`. The `wanted` status SHALL NOT influence the present column; it is shown only in the wanted column.
4. THE `_log_stream_table()` function SHALL iterate the artifact list in the order it was produced by `_recover()`, which preserves the original track enumeration order.
5. WHEN `ExtractionPhase._recover()` builds its internal artifact list, THE list SHALL include an artifact entry for every stream in the source file — both those selected by the current filter (`wanted=True`) and those excluded (`wanted=False`) — so that the stream table shows all streams with their correct wanted/present status.
6. THE `TimestampArtifact` row SHALL continue to be included in the stream table when a `TimestampArtifact` is present in the artifact list; its `wanted` and `state` fields drive the column values in the same way as stream artifacts.
7. THE stream table SHALL present every possible stream variant as an end-user overview showing its `wanted` and completeness status; because completeness for all rows is derived from the same single directory listing, including unwanted rows SHALL add no extra scanning cost.

### Requirement 6: Unified recovery reporting derived from the internal artifact list

**User Story:** As a developer, I want one shared recovery-reporting routine that takes a phase's internal artifact list and derives every count itself, so that no phase duplicates recovery-count or recovery-message logic and all phases report recovery uniformly.

#### Acceptance Criteria

1. THE unified recovery-reporting helper in `pyqenc/utils/log_format.py` SHALL accept the phase's internal artifact list (the full list, including `wanted=False` entries) plus a `unit` label, and SHALL derive all recovery counts itself; no phase SHALL compute recovery counts on its own.
2. THE unified recovery-reporting helper SHALL report exactly five counts — total (all internal artifacts), unwanted (`wanted=False`), complete, partial, and absent — where complete, partial, and absent SHALL be counted over the wanted artifacts only, and SHALL always show all five counts even when a count is zero. The emitted line SHALL follow the form `Recovery: 15 total, 2 unwanted — 10 complete, 1 partial, 2 absent — resuming`, retaining a trailing `resuming` or `full run needed` suffix.
3. THE five `_recovery_message()` helper functions in `pyqenc/phases/extraction.py`, `pyqenc/phases/audio.py`, `pyqenc/phases/chunking.py`, `pyqenc/phases/merge.py`, and `pyqenc/phases/encoding.py` SHALL be removed, and the human-readable recovery message and log-line content SHALL be produced uniformly by the unified recovery-reporting helper.
4. THE unified recovery-reporting helper SHALL NOT expose a `stale` parameter or `stale` count; the `unwanted` count SHALL supersede the former `stale` concept in recovery reporting.
5. WHEN a phase's `run()` or scan path reports recovery, THE phase SHALL call the unified recovery-reporting helper with its internal artifact list rather than computing complete or pending counts locally.
6. THE `PhaseResult` docstring in `pyqenc/phase.py` SHALL state that `artifacts` contains only wanted artifacts, and that `pending` and `complete` derive from it without additional filtering.
