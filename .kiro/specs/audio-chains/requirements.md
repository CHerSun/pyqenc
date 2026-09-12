# Requirements Document

<!-- markdownlint-disable MD024 -->

- Spec: Audio Chains
- Created: 2026-09-11
- Completed:

## Cross-Spec Notes

### What this spec supersedes

| Superseded requirement | Original spec | What changed |
|---|---|---|
| Combinatorial strategy graph (`AudioEngine.build_plan` BFS producing every variation) | audio-phase (original merge-in) | Replaced by explicit user-defined **chains**. One chain produces exactly one output per matched track; no automatic fan-out. |
| Filename-driven auto-targeting via `check()` predicates + `NORMALISED_PREFIXES` | audio-phase (original) | Replaced by explicit chain targeting through the **select** tree, matched against a stream object's conventional string. |
| `AudioConfig` flat model (`convert_pattern`, `codec`, `bitrate_per_channel`, `extension`, `peak_target_dbfs`) | audio-phase (original) | Replaced by structured `audio.filters` / `audio.chains` / `audio.select`. |
| `AudioParams` sidecar storing only `codec` + `bitrate_per_channel` | audio-phase (original) | Replaced by a sidecar storing the fully-resolved chains + resolved select, enabling per-chain invalidation. |
| Step-by-step FLAC-intermediate processing (one ffmpeg call per strategy) | audio-phase (original) | Replaced by a single combined `-af` chain per (track, chain), split only at two-pass filters. |
| `_two_pass_loudnorm` (dead code) / `_two_pass_peaknorm` hard-wired per strategy | audio-phase (original) | Two-pass behaviour becomes a per-filter property; chain execution splits generically at any two-pass filter. |

### Forward references (out of scope here, planned next)

- **Merge consumption of audio outputs.** Merge remains video-only. Selecting produced audio outputs for muxing is a future spec. This spec only makes the produced outputs well-formed (tagged artifacts with metadata) so a future merge spec can consume them.
- **In-memory stream objects (no on-disk extraction).** A future spec moves targeting to source stream objects and lets `passthrough` avoid producing a file entirely. This spec adds `passthrough` to the config surface but leaves its executor as a `NotImplementedError` stub.

## Introduction

Audio processing today produces every possible transformation variation and offers no way to express which outputs are actually wanted. The engine fans out combinatorially through hard-coded strategy classes whose eligibility is decided by parsing filenames, and the only user lever is a single `convert_pattern` regex that picks which of the auto-generated variations get a delivery file. The phase itself was merged from another project and does not follow the project's Phase pattern (no proper result object, no artifact metadata, no uniform logging).

This spec replaces that model with an explicit, user-driven design built from three configurable pieces:

1. **Filters** — named, reusable transformation definitions (a palette). Each is an instance of a *type* (`peaknorm`, `dynaudnorm`, `loudnorm`, `downmix`, `encode`, `passthrough`) with its own parameters. Users can tune a filter or add their own without redefining the whole set.
2. **Chains** — ordered lists of filter references. A chain is deterministic: applied to N matched tracks it produces exactly N outputs. Multiple chains may independently target the same or different tracks.
3. **Select** — an ordered priority tree deciding which extracted audio tracks each processing run operates on, with coarse gates (`for`), exclusions (`exclude`), and preference tiers (`prefer`) with implicit fallback.

Chains are executed as a single combined ffmpeg `-af` invocation, broken only where a filter genuinely needs two passes (measurement then application). Recovery and invalidation are driven by a sidecar holding the fully-resolved chain/select definitions, compared by equality against the current resolved config. Output filenames preserve the source stem and append a ` chain=<name>` suffix. Finally, the audio phase is brought up to the project's standard Phase pattern.

## Glossary

- **Filter**: A named configuration entry under `audio.filters`, keyed by user-chosen name, carrying a `type` and type-specific parameters. Filters are the palette; they are referenced by name from chains.
- **Filter type**: One of `peaknorm`, `dynaudnorm`, `loudnorm`, `downmix`, `encode`, `passthrough`. The type determines the ffmpeg behaviour and which parameters are valid.
- **Chain**: A named entry under `audio.chains`, defining an ordered list of filter names to apply. Produces exactly one output per matched track.
- **Select**: The ordered list under `audio.select` that decides which extracted audio tracks are processed. Empty (default) = process all extracted audio tracks.
- **`for`**: A regex gate on a select entry; tracks whose conventional string matches are candidates for that entry.
- **`exclude`**: A regex on a select entry; candidate tracks whose conventional string matches are dropped from that entry.
- **`prefer`**: An ordered list of regex tiers on a select entry. The first tier that matches at least one candidate wins; *all* tracks matching that tier are selected. If no tier matches, the implicit fallback selects all candidates that passed `for`/`exclude`.
- **Conventional string**: A stable, regex-friendly string describing an audio track (e.g. containing `lang=eng`, `ch=5.1`, title text), produced by the audio stream object. Targeting matches against this string, not against filenames.
- **Two-pass filter**: A filter type whose ffmpeg realisation requires a measurement pass before an application pass (`loudnorm`, `peaknorm`). Chain execution breaks the combined `-af` string at each two-pass filter.
- **Downmix (downmix-only)**: A `downmix` filter with a target layout `to:`. It only reduces channels — if the source has ≤ the target channel count it is a no-op (emits no filter). `matrix:` selects the fold coefficients for the 5.1→2.0 (and derived 7.1→2.0) stage.
- **Encode filter**: A filter of type `encode` carrying `codec`, `bitrate_per_channel`, and `extension`. It maps to ffmpeg output arguments (`-c:a`, `-b:a`) and determines the output file extension — it is not part of the `-af` string.
- **Passthrough filter**: A filter of type `passthrough` meaning stream-copy with no filtering. In this spec its executor is a stub raising `NotImplementedError`; the config surface accepts it for forward compatibility.
- **Resolved chain**: A chain with every referenced filter's parameters inlined, plus its effective encode target. Used for sidecar persistence and invalidation.
- **Chain output**: One produced audio file per (matched track, chain), named `<source-stem> chain=<chain-name>.<ext>`.

---

## Requirements

### Requirement 1: Named filter palette (`audio.filters`)

**User Story:** As a user, I want to define named, reusable audio filters with their own parameters, so that I can tune behaviour in one place and reference it from multiple chains without repetition.

#### Acceptance Criteria

1. THE config SHALL provide `audio.filters` as a mapping of user-chosen filter name → filter definition.
2. EACH filter definition SHALL carry a `type` field whose value is a filter-type id present in the filter-type registry (Requirement 2). The registry — not a hardcoded list in the config model — is the authority on which types exist.
3. THE `audio.filters` mapping SHALL use dict-merge semantics across config layers, so a later config layer MAY add a new named filter or override parameters of an existing named filter without redefining the whole palette.
4. WHEN a filter definition has a `type` not present in the registry, config load SHALL raise a validation error naming the offending filter and listing the registered type ids.
5. WHEN a filter definition contains a parameter not valid for its type's param model, config load SHALL raise a validation error naming the offending filter and parameter.
6. THE bundled `default_config.yaml` SHALL define a starter palette covering at least: `peaknorm`, `dynaudnorm`, `loudnorm`, three `downmix` variants (`std`, `lfe`, `boosted` matrices to 2.0), one `encode` (aac), and one `passthrough`.

---

### Requirement 2: Filter-type registry (extension point)

**User Story:** As a developer, I want filter types defined by self-contained, registered classes rather than a hardcoded list, so that adding a new filter type is a single-class change with no edits to the config model, validators, or executor.

#### Acceptance Criteria

1. THE system SHALL maintain a filter-type registry mapping a type id (string) → the class implementing that type. Each filter-type class SHALL own: (a) its type id, (b) its Pydantic parameter model, and (c) its realisation behaviour (Requirement 3).
2. Registration SHALL be declarative (e.g. a `@register_filter` decorator or equivalent registration at import) so that defining and registering a new type requires no changes to `AudioConfig`, the chain executor, or existing filter classes.
3. THE registry SHALL reject duplicate type ids at registration time (import-time failure), so two classes cannot claim the same id.
4. Config validation SHALL resolve a filter definition's `type` against the registry and validate the remaining fields against that type's parameter model; unknown type or invalid params raise a `ValidationError` (per Requirement 1.4 / 1.5).
5. THE executor and config model SHALL NOT contain any hardcoded enumeration of the concrete filter types; they SHALL dispatch through the registry and the filter class interface only.
6. THE set of types registered by default SHALL be exactly: `peaknorm`, `dynaudnorm`, `loudnorm`, `downmix`, `encode`, `passthrough`. (These are the initial registrations, not a closed set.)

---

### Requirement 3: Filter-type parameters and behaviour (initial set)

**User Story:** As a user, I want each of the initially-registered filter types to accept the parameters that matter for it, so that I can express real audio-processing intent (normalisation targets, downmix matrices, encode codec/bitrate).

#### Acceptance Criteria

1. EACH filter type SHALL fully own its own behaviour through the single `resolve(layout, last_output) -> FilterStep` contract (Requirement 6): its pass count, how it builds each measurement pass, how it scrapes the needed value out of that pass's ffmpeg output, and how it folds that value into its final `-af` fragment. No filter-type-specific logic SHALL live in the executor.
2. `peaknorm` SHALL accept `target_dbfs: float` and internally realise a two-pass peak normalisation (measure peak via `volumedetect`, then apply a `volume` gain), preserving source sample rate and bit depth.
3. `loudnorm` SHALL accept `i: float`, `tp: float`, `lra: float` and internally realise a two-pass EBU R128 normalisation (measure via `loudnorm=…:print_format=json`, then linear normalise using the measured values).
4. `dynaudnorm` SHALL accept the dynamic-normalisation parameters (`f`, `g`, `p`, `m`, `r`, `b`) and internally realise a single-pass `-af dynaudnorm=...`.
5. `downmix` SHALL accept `to:` (target layout, e.g. `2.0`, `5.1`, `7.1`) and an optional `matrix:` (`std`, `lfe`, `boosted`).
6. `downmix` SHALL be downmix-only: WHEN the source channel count is less than or equal to the `to:` target, the filter SHALL emit an empty `-af` contribution (pure passthrough of channels).
7. `downmix` `matrix:` SHALL affect the 5.1→2.0 fold (and the derived 7.1→2.0 fold); a 7.1→5.1 reduction SHALL use a plain channel map with no matrix. ALL downmix matrices SHALL be **index-addressed** (`pan=stereo|c0=…|c1=…`), NOT channel-name-addressed, and SHALL assume the canonical 5.1 order `c0=FL, c1=FR, c2=FC, c3=LFE, c4=BL, c5=BR` (and 7.1 order extended with `c6=SL, c7=SR`). Index addressing is required because named addressing depends on ffmpeg's per-encoding layout interpretation and can mis-map.
8. THE built-in matrix set SHALL include at least: `std` (canonical ITU-R BS.775 / ATSC Lo/Ro fold, LFE dropped), `lfe` (the historical "night" fold), and `boosted` (the historical "nboost" fold). `lfe` and `boosted` are community-sourced formulas preserved **verbatim** as distinct named matrices (they differ in FC weight, surround weight, and LFE handling — not merely LFE gain). The matrix set SHALL be open: further named matrices MAY be added later as registered entries without changing the filter or executor.
9. `encode` SHALL accept `codec: str`, `bitrate_per_channel: str`, and `extension: str`; it SHALL contribute no `-af` fragment and SHALL instead set the `FilterStep.output_format` terminal target (codec, scaled bitrate, extension). `bitrate_per_channel` SHALL be scaled by the output channel count at execution time.
10. `passthrough` SHALL accept no transformation parameters and SHALL represent stream-copy with no filtering.

---

### Requirement 4: User-defined chains (`audio.chains`)

**User Story:** As a user, I want to define named ordered chains of filters, so that I can produce specific, deterministic audio variants from selected tracks.

#### Acceptance Criteria

1. THE config SHALL provide `audio.chains` as a list; each item SHALL carry a unique `name` and an ordered `filters` list of filter names.
2. THE `audio.chains` list SHALL use list-replace semantics across config layers (a later layer defining `chains` replaces the earlier list wholesale).
3. Chain `name` uniqueness SHALL be enforced at config load; a duplicate name SHALL raise a validation error.
4. EACH filter name referenced by a chain SHALL exist in `audio.filters`; an unknown reference SHALL raise a validation error naming the chain and the missing filter.
5. A chain applied to N matched tracks SHALL produce exactly N outputs (one per matched track) — never a combinatorial expansion.
6. Multiple chains MAY independently target the same track set (each producing its own output) or disjoint sets; overlap is permitted and determined solely by targeting.
7. WHEN a chain contains no `encode` filter, its output SHALL default to FLAC. This SHALL be realised as the executor's initial `output_format` (a FLAC default), NOT by injecting a synthetic FLAC filter into the chain.
8. WHEN a chain contains one or more `encode` filters, the last `encode` in the chain SHALL determine the output codec/extension (overriding the FLAC default); earlier `encode` filters SHALL be ignored.
9. WHEN a chain contains a `passthrough` filter, `passthrough` SHALL be the only filter in that chain; any other combination SHALL raise a validation error at config load.

---

### Requirement 5: Track selection (`audio.select`)

**User Story:** As a user, I want to select which extracted audio tracks are processed using language/channel/title priorities with fallback, so that I get the tracks I want (e.g. English 7.1 if present, else 5.1, else any) without a custom query language.

#### Acceptance Criteria

1. THE config SHALL provide `audio.select` as an ordered list; each entry SHALL carry `for` (regex gate) and MAY carry `exclude` (regex) and `prefer` (ordered list of regex tiers).
2. THE `audio.select` list SHALL use list-replace semantics across config layers.
3. WHEN `audio.select` is empty or absent, ALL extracted audio tracks SHALL be selected for processing (default behaviour).
4. Targeting SHALL match regexes against each audio track's conventional string (Requirement 7), NOT against filenames.
5. FOR a select entry, candidate tracks SHALL be those whose conventional string matches `for` and (if present) do NOT match `exclude`.
6. WHEN `prefer` is present, tiers SHALL be evaluated in order; the first tier matching at least one candidate SHALL win, and ALL candidates matching that winning tier SHALL be selected.
7. WHEN `prefer` is present but no tier matches any candidate, the implicit fallback SHALL select all candidates that passed `for`/`exclude`.
8. WHEN `prefer` is absent, all candidates that passed `for`/`exclude` SHALL be selected.
9. Select entries SHALL be additive, not fallbacks: EACH entry independently contributes its picked tracks (per criteria 5–8), and the contributed sets SHALL be combined into the working track set. A track picked by more than one entry SHALL appear once (deduplicated). (Selection produces the working track set; chains then apply to it per their own definition.)

---

### Requirement 6: Generic chain executor with filter-owned passes

**User Story:** As a developer, I want a generic executor loop that drives each filter through its passes via a single contract, so that all pass logic lives in the filters and the loop never knows one filter type from another.

#### Acceptance Criteria

1. THE filter contract SHALL be a single stateless method `resolve(layout, last_output) -> FilterStep`, where `FilterStep` carries `af: str` (the `-af` contribution; `""` for a no-op, never `None`), `needs_pass: bool`, `out_layout: ChannelLayout`, and `output_format: EncodeParams | None`.
2. THE executor SHALL maintain the accumulating (invariant) `-af` chain, the current layout, and the effective `output_format` (last non-`None` wins). For the current filter it SHALL call `resolve`; WHEN `needs_pass` is true it SHALL run a measurement pass with `accumulated_af + step.af` (output to null) and call `resolve` again; WHEN `needs_pass` is false it SHALL append `step.af` to the invariant chain, apply `out_layout`, and advance to the next filter.
3. `last_output` passed to `resolve` SHALL be ONLY the current filter's most recent measurement pass result (never accumulated history), and SHALL be cleared to `None` the moment a filter finishes, so it can never leak to the next filter.
4. WHEN a chain contains K filters that each request a measurement pass, execution SHALL use exactly K measurement passes + 1 final application pass (K+1 invocations); K=0 → a single application invocation. Stacking is permitted with no artificial limit.
5. THE executor SHALL contain NO filter-type-specific logic; it SHALL dispatch only through `resolve`/`FilterStep`. All measurement building and scraping lives in the filter classes.
6. ALL ffmpeg invocations SHALL go through the unified runner in `pyqenc/utils/ffmpeg_runner.py` (no direct subprocess calls); measurement passes SHALL write no output file, and the final application pass SHALL use the `.tmp`-then-rename protocol.
7. Processing SHALL preserve source properties by default per project goals: no sample-rate change, no bit-depth change, no channel change beyond what a `downmix` filter explicitly performs. THE executor SHALL NOT convert sources to a FLAC intermediate as a separate up-front step.

---

### Requirement 7: ChannelLayout type and conventional stream string for targeting

**User Story:** As a developer, I want channel layout represented by a proper type (not free text) and a stable conventional string an audio track exposes for select regexes, so that layout decisions are canonical, the user's regex sees the faithful layout, and targeting is ready for the future move to in-memory stream objects.

#### Acceptance Criteria

1. THE system SHALL define a `ChannelLayout` value type carrying both `original` (the exact ffmpeg token, e.g. `5.1(side)`, `stereo`, `7.1`) and `normalized` (canonical base layout, e.g. `5.1`, `2.0`), plus a derived channel count.
2. Normalization SHALL map `stereo` → `2.0` and strip qualifiers (e.g. `5.1(side)` → `5.1`); `normalized`/count SHALL drive downmix target/no-op comparison, matrix lookup, and bitrate scaling.
3. `ChannelLayout` SHALL replace free-text channel layout wherever a layout is represented internally: `AudioMetadata`, extraction audio metadata (`AudioStream.channels_layout`), phase results, downmix params/matrices, and bitrate scaling. This is a representation change with no behaviour change to extraction.
4. THE audio metadata object SHALL expose a conventional string containing at least: language (`lang=<code>`), channel layout (`ch=<layout>`), and title text when present. The `ch=` token SHALL use `ChannelLayout.original` so the user's regex matches the faithful source layout (e.g. `ch=5.1(side)`).
5. Select-entry regexes (`for`, `exclude`, `prefer` tiers) SHALL be matched against this conventional string, case-insensitively by default (consistent with extraction filtering).
6. THE conventional string SHALL be derivable from the `ExtractionPhaseResult` audio metadata without re-probing.

---

### Requirement 8: Output filenames and extension

**User Story:** As a user, I want output files to keep the original track name and clearly indicate which chain produced them, so that outputs are recognisable and unambiguous.

#### Acceptance Criteria

1. EACH chain output SHALL be named `<source-stem> chain=<chain-name>.<ext>`, preserving the source stem unchanged.
2. `<chain-name>` SHALL be the chain's configured `name`.
3. `<ext>` SHALL be `flac` when the chain has no `encode` filter, otherwise the `extension` from the chain's effective (last) `encode` filter.
4. Chain names SHALL be constrained to characters safe for filenames; a name containing filesystem-unsafe characters SHALL raise a validation error at config load.
5. ALL produced files SHALL be written via the `.tmp`-then-rename protocol; no non-`.tmp` partial output SHALL ever exist on disk.

---

### Requirement 9: Recovery and invalidation via resolved-chain sidecar

**User Story:** As a developer, I want the audio phase to recover produced outputs and reprocess only what changed, so that reruns are cheap and correct without manual cleanup after a config edit.

#### Acceptance Criteria

1. THE audio phase SHALL maintain a sidecar containing ONLY the fully-resolved definition of each committed chain (all referenced filter parameters inlined, effective encode target included), keyed by chain name. The sidecar SHALL NOT store `select` — selection is recomputed each run from the current tracks and `select` config, so there is nothing to persist.
2. ON rerun, THE phase SHALL compare, by equality, each configured chain's resolved definition against the sidecar entry of the same name.
3. WHEN a chain's resolved definition is unchanged AND its expected output exists on disk, the output SHALL be recovered as COMPLETE and not reprocessed.
4. WHEN a chain's resolved definition changed OR the chain was removed from config, THE phase SHALL delete that chain's on-disk artifacts, matching by **exact chain name** parsed from the ` chain=<name>` filename suffix — never by substring or prefix (so a chain whose name contains another chain's name is not affected).
5. THE phase SHALL write the updated sidecar (reflecting the current resolved chains) **at invalidation time, before producing any output** — not after processing. This guarantees a mid-run crash leaves the sidecar consistent with the current config so the next run resumes the missing outputs instead of re-invalidating completed ones. WHEN nothing differs, THE phase MAY skip the rewrite (the sidecar is already correct).
6. Completion SHALL be determined solely from the presence of output files on disk, never inferred from the sidecar. THE sidecar records committed intent, not completion.
7. THE sidecar SHALL be written via the `.tmp`-then-rename protocol.
8. Recovery SHALL be exhaustive over on-disk audio artifacts: each artifact SHALL be classified as wanted-and-complete, wanted-but-absent/partial, or present-but-unwanted, following the project Phase Contract.
9. THE phase SHALL respect force invalidation (`force_wipe`) by removing all audio artifacts and the sidecar before recovery.

---

### Requirement 10: Audio phase brought to the standard Phase pattern

**User Story:** As a developer, I want the audio phase to follow the same Phase pattern as the other phases, so that its result, artifacts, logging, and lifecycle are uniform and predictable.

#### Acceptance Criteria

1. THE audio phase SHALL expose an `AudioPhaseResult(PhaseResult)` carrying typed artifact objects (one per produced chain output) with metadata (source track, chain name, resolved output `ChannelLayout`/codec, path, state).
2. THE phase SHALL emit a phase banner at start, consistent with other phases (`emit_phase_banner`).
3. THE phase SHALL emit uniform per-chain log lines using shared formatting helpers, so each chain reports in a consistent format.
4. THE phase SHALL emit an end-of-phase summary (counts of produced/reused/failed outputs) consistent with other phases.
5. THE phase SHALL classify artifact states using the shared `ArtifactState` values (COMPLETE / ABSENT / PARTIAL) and set `wanted` correctly.
6. THE phase SHALL use the project `ProgressBar` for the processing loop with a **count-based** total equal to the number of pending (track, chain) jobs (known up front), advancing one unit per completed job and indicating the current job. Duration-weighted progress is out of scope.
7. Logging SHALL respect the level guidance: `debug` for internal ffmpeg steps, `info` for milestones/summary, `warning`/`error`/`critical` for failures.
8. THE phase SHALL respect the configured cleanup level and forced invalidation per the Phase Contract; delivery outputs and the recovery sidecar SHALL survive cleanup.

---

### Requirement 11: Passthrough stub (forward-compatible)

**User Story:** As a user, I want to declare a passthrough chain now, so that the config is future-ready, while understanding it is not yet functional.

#### Acceptance Criteria

1. THE config SHALL accept a `passthrough` filter type and a chain consisting solely of it (per Requirement 3.9).
2. WHEN the executor is asked to run a `passthrough` chain, it SHALL raise `NotImplementedError` with a clear message referencing the future in-memory-stream spec.
3. THE `passthrough` stub SHALL include a code comment/docstring noting the intended future behaviour (no file produced; source stream object surfaced directly into the phase result).
4. A `passthrough` chain SHALL NOT silently produce an incorrect file; failing loudly is required until implemented.

---

### Requirement 12: Config validation and defaults surface

**User Story:** As a user, I want invalid audio configs to fail fast at startup with clear messages, and a sensible default config, so that mistakes are caught before any processing.

#### Acceptance Criteria

1. ALL `audio` config validation (unknown filter type, invalid parameter, unknown filter reference, duplicate chain name, passthrough-not-alone, unsafe chain name) SHALL occur at config load and raise a `ValidationError` before any phase runs.
2. THE bundled `default_config.yaml` SHALL ship a working `audio.filters` palette, at least one example `audio.chains` entry, and an empty `audio.select` with commented examples (rus-dub-prefer, eng-7.1-then-5.1).
3. THE default config SHALL include a comment noting that changing a chain's definition triggers automatic reprocessing on the next run (via the resolved-chain sidecar).
4. Per project standards, no function/constructor default parameter values SHALL diverge from the canonical defaults defined by the config; the default config remains the single source of truth.

---

### Requirement 13: Documentation

**User Story:** As a user, I want documentation describing filters, chains, and select, so that I can configure audio processing correctly.

#### Acceptance Criteria

1. Documentation SHALL describe each filter type and its parameters.
2. Documentation SHALL describe chain definition, one-output-per-track determinism, and the implicit-FLAC / last-encode-wins rules.
3. Documentation SHALL describe the `select` tree (`for`/`exclude`/`prefer`) with the winning-tier and implicit-fallback semantics, including the two worked examples (rus dubs, eng 7.1→5.1).
4. Documentation SHALL note the output filename convention and the automatic-reprocess-on-change behaviour.
