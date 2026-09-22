# Project TODO — findings needing thought

Items are **observations with evidence, not decisions** — each needs a design
call before any implementation. Nothing here is blocking. Entries are removed
outright once a plan covering them is finalized or they are fixed — git and
spec history are the record.

Sources:
- §1–7: 2026-09-19/20 investigation of the extraction-metrics bug (fixed in 0.14.2).
- §8–37: imported 2026-09-20 from `D:\todo pyqenc.md`, after re-verifying every
  claim against the working tree (HEAD `1336cbf` + the uncommitted 0.14.2 fix).
  Items already resolved are listed at the bottom, not carried over.

Status legend: 🤔 needs thinking · 🔍 verified against code

---

# From the 2026-09-19/20 metrics investigation

## 🤔 2. `parallelism` field described by the metrics spec is not implemented

**Status:** needs thinking (spec/code reconciliation)

- `.kiro/specs/2026-04-22 app-metrics-two-tier/design.md:299` describes a top-level
  `parallelism` field; `PipelineMetrics` explicitly notes it is
  "NOT part of this model" (`pyqenc/metrics.py:258-259`).
- Related minor spec staleness: `requirements.md:189-191` still motivates
  "no dotted keys under `extraction`" with "(single mkvextract operation...)" —
  mkvextract was removed from the extraction phase in `4d0433f`.
  (The design.md call-site table row was already reworded in the 0.14.2 fix.)

**Questions to think about:** implement the field, or reconcile the spec down
to what exists? Worth a general pass over `app-metrics-two-tier` vs. current code.

---

## 🤔 3. Sub-second metric keys are lossy across runs

**Status:** needs thinking (accuracy quirk, verified live)

- Seconds are truncated to integers at report time and zero rows are omitted
  (`_compute_top_level_entries`, `pyqenc/metrics.py:467-482`).
- `_try_resume` restores accumulators from the *persisted truncated* values
  (`pyqenc/metrics.py:594-607`) — and an omitted zero row restores nothing.
- Observed live: extraction runs recording `recovery ≈ 0.4–0.8s` never
  persist that time, so repeated reuse-runs accumulate no `recovery` seconds
  at all, and cross-run totals drift low.

**Questions to think about:** persist floats and round only at display time?
Make zero-row omission display-only rather than load-bearing for resume?

---

## 🤔 4. Pre-existing ruff violations (21) in `extraction.py` / `test_metrics_integration.py`

**Status:** needs thinking (cleanup pass; identical on HEAD `1336cbf`, none
introduced by the 0.14.2 fix — verified via stash)

- `UP040` — `ExtractionArtifact: TypeAlias` should use the `type` keyword
  (`pyqenc/phases/extraction.py:707`).
- `BLE001` — blind `except Exception` ×2 (`pyqenc/phases/extraction.py:1000`,
  `:1205`).
- `F821` — undefined names in test-file annotations: `JobPhaseResult`,
  `ExtractionPhase`, `ChunkingPhase`, `AudioPhase`, `OptimizationPhase`,
  `EncodingPhase`, `MergePhase` and their results are used in return
  annotations but only imported function-locally
  (`tests/test_metrics_integration.py:180, 213, 351, 374, 393, 628, 782, 805,
  829, 1068, 1091, 1109, 1128, 1386, 1409, 1433, 1448`). These are latent
  annotation bugs, not just lint noise.
- `F841` — unused `success_result` (`tests/test_metrics_integration.py:1545`).

**Questions to think about:** one ruff-cleanup commit? For the test file:
`TYPE_CHECKING` imports vs. local imports — note coding-standards prefers
top-level imports.

---

## 🤔 5. Metrics integration tests assert on internal implementation

**Status:** needs thinking (test-quality concern)

- The metrics integration tests spy on `collector.time()` call args with
  `MagicMock` and assert on the key list
  (`tests/test_metrics_integration.py`, extraction cases around lines 226–320 —
  including the assertions added in the 0.14.2 fix, which follow the file's
  established pattern).
- `coding-standards.md`: "Tests should never check internal state, only
  observable behavior"; `steering/agent-commands.md`: public/external behavior
  must be tested, not internal implementation.

**Questions to think about:** restructure toward running a phase with a real
`YamlMetricsCollector` into a temp dir and asserting on the written
`metrics.yaml`? Keep spy tests as fast unit-level complements?

---

## 🤔 6. Metrics I/O failures are silent (WARNING only)

**Status:** needs thinking (deliberate trade-off, worth revisiting)

- `_write_atomic` swallows `OSError` (`pyqenc/metrics.py:752-760`);
  `_try_resume` swallows load failures (`pyqenc/metrics.py:590-592`).
  Non-fatal by design — but a user can finish a multi-hour encode and
  silently end up with a stale or missing `metrics.yaml`.

**Questions to think about:** is WARNING sufficient? Surface
"metrics not written / resumed from stale file" in the end-of-run summary?

---

# Imported from `D:\todo pyqenc.md` (verified 2026-09-20)

## Correctness / invalidation

## 🤔 8. Encode commands don't drop non-video streams (bin_data fix only landed in audio)

**Status:** needs thinking (correctness; half-fixed)

- Audio chains already emit `-vn -sn -dn` in both builders
  (`pyqenc/audio/chain.py:295,336`; rationale in `pyqenc/constants.py:204-210`
  citing stray bin_data).
- The chunk-encode command has no `-dn`/`-vn`/`-sn`/`-map_chapters` at all
  (`pyqenc/phases/encoding.py:689-697`), and OptimizationPhase reuses the same
  `ChunkEncoder` builder (`pyqenc/phases/optimization.py:878-904`) — so stray
  data streams / partial chapters can still land in encoded chunks.

**Questions to think about:** add `-dn -sn` (and `-map_chapters -1`?) to the
encode builder. What streams does chunk input actually carry today?

---

## 🤔 9. AudioPhase doesn't invalidate when its sidecar is deleted

**Status:** needs thinking (bug-ish, verified in code)

- Missing sidecar loads as `None` → `prior_sigs = {}` → nothing invalidated,
  sidecar silently rewritten (`_invalidate_and_commit`,
  `pyqenc/phases/audio.py:417-443`); `_classify` judges completion solely
  from output-file presence (`audio.py:477-499`) → stale outputs produced
  under an old chain config survive a sidecar deletion as "COMPLETE".

**Questions to think about:** detect sidecar-missing-with-outputs and
re-verify (chain signature embedded in output names?) or force re-run?

---

## 🤔 10. Chapters are filterable and never consumed downstream

**Status:** needs thinking (design gap)

- Chapters are a normal filterable stream ("as a stream for uniform
  filtering", `pyqenc/phases/extraction.py:276-297`; include/exclude applies
  to all types, `pyqenc/cli.py:162-169`), unlike timestamps which are
  extracted unconditionally (`extraction.py:550-621, 1066-1069`).
- Chunking has zero chapter handling (split cmd is `-ss/-t ... -an`,
  `pyqenc/phases/chunking.py:256-263`); merge concat uses mkvmerge append +
  `--timestamps` with no `--chapters` (`pyqenc/phases/merge.py:1215-1222`).
  The extracted chapters.xml artifact is read by no downstream phase.

**Questions to think about:** original idea — make chapters a mandatory
artifact (not filterable, like timestamps) and use the *original* chapters at
mkvmerge concat. Worth doing before someone hits per-chunk chapter explosion?

---

## 🤔 11. Invalidation on config edits is partial; invalidation-matrix doc is stale

**Status:** needs thinking (review)

- Real param-keyed invalidation exists per phase: audio chain signatures
  (`audio.py:417-443`), chunking mode mismatch (`chunking.py:645-670`),
  optimization targets/sampling (`optimization.py:196-246`), encoding
  probe/crop mismatch (`encoding.py:1730-1770`), merge
  (`merge.py:786-817`).
- But editing codec/profile `encoder_args`/`extra_args` invalidates nothing
  (`EncodingParams` persists only `probe`, `encoding.py:1740`).
  (2026-09-22, phase-run-template: strategy-list changes no longer leave
  *invisible* orphans — encoding recovery surfaces `encoded/<strategy>/`
  dirs outside the current selection as `wanted=False` artifacts; actual
  deletion remains gated by explicit cleanup levels.)
- `parameters-phases-invalidation.md` is itself partly stale: row 16 claims
  chunking_mode is untracked (now tracked at `chunking.py:645-670`), rows
  23–25 reference audio params removed by the audio-chains rework.
- The crop dich survives, now documented (`check_resolution = None if crop`,
  `encoding.py:780-783`).

**Questions to think about:** config-fingerprint-based invalidation instead
of hand-listed fields? Refresh/regenerate the matrix doc?

---

## Metrics & quality search

## 🤔 12. No per-chain dotted metrics from the audio phase

**Status:** needs thinking (spec/code gap; the ChunkEncoder prefix-injection mechanism now exists — time per chain under `audio.<chain>`?)

- Audio records only top-level `audio` and `recovery` — the only two
  collector calls in the file (`pyqenc/phases/audio.py:197,219`); the whole
  `_execute_audio` is timed under one key. No `audio.<chain>` dotted keys.

**Questions to think about:** time per chain under dotted keys? Needs the
the same prefix-injection mechanism the optimization phase already uses (metric_prefix on ChunkEncoder).

---

## 🤔 13. Winning-attempt log lacks the limiting metric; no per-attempt stats table

**Status:** needs thinking (partially addressed)

- Per-attempt lines already mark the bottleneck metric (`•`/`✘` via
  `_find_worst_target` + `fmt_metric_summary`, `pyqenc/phases/encoding.py:956-973`).
- But the final "success ✅ with CRF X after N attempts" line carries no
  "limited by <metric>" (`pyqenc/utils/log_format.py:192-193`, called at
  `encoding.py:984-987`), and the end-of-phase summary is counts only
  (`encoding.py:1458-1465`) — no quality-search stats table (PNG graphs are
  saved per attempt instead, `encoding.py:917,603-607`).

**Questions to think about:** append limiting metric to the final line; add a
summary table (attempt → CRF → key metrics → verdict)?

---

## 🤔 14. Measure summary table shows median only, no min

**Status:** needs thinking (UX polish)

- `_log_measure_summary` prints per-target rows with `... med` columns only
  (`pyqenc/phases/measure.py:809-858`); min+med appear together only in plot
  captions (`pyqenc/utils/visualization.py:1047`) and the merge miss-table
  (`pyqenc/phases/merge.py:277`).

**Questions to think about:** `{min}...{med}` columns per metric as originally
sketched?

---

## 🤔 15. No outlier rejection for low min values (VMAF ≈ 20 case)

**Status:** needs thinking (accuracy)

- `extract_key_stats` passes `min` through unchanged
  (`pyqenc/utils/visualization.py:833-849`); its only special case is
  substituting a non-inf percentile for PSNR's infinite max.
- Percentile statistics exist and are manually selectable as target stats
  (`pyqenc/models.py:310-315`), but nothing automatically ignores an outlier
  low min.

**Questions to think about:** drop/flag min when it's far below a percentile
band? Only when subsampling was active (cf. §16)?

---

## 🤔 16. Subsampling is never disabled for few frames

**Status:** needs thinking (accuracy)

- Subsampling applies unconditionally via `select='not(mod(n,{subsample}))'`
  (`pyqenc/quality.py:377,400`); the only validation is
  `metrics_sampling >= 1` (`pyqenc/phases/measure.py:933-934`). No frame-count
  guard anywhere; `merge.py:404-405` only logs that subsampling may miss
  outliers.

**Questions to think about:** auto-set sampling=1 below a frame-count
threshold (with a log line)?

---

## 🤔 17. QualitySearchV3: no worse-end tie-break; no saved best_failing / worst_passing

**Status:** needs thinking (algorithm tweak)

- `QualitySearchV3` has a BEST_SCORE point (`_best_score_point`,
  `pyqenc/quality.py:1422`, update rule `:1491-1497`) and keeps all measured
  points (`_attempted_points`, `:1421,1480`); it does 3-point dispatch
  (`:1584-1603`) and derives the nearest opposite-side tested point on the
  fly (`_extrapolate_outward`, `:1728-1733`) — the functional equivalent of
  worst_passing/best_failing.
- Missing: named best_failing/worst_passing attributes, and a tie-break
  preferring the point closer to the *worse* end on equal score (comparisons
  are strict `<`/`>` at `:1494-1495`).

**Questions to think about:** add the tie-break (cheap) vs. full named-point
rework?

---

## 🤔 18. BALANCED metrics mode (soft matching) does not exist

**Status:** needs thinking (feature)

- No "balanced" scoring anywhere (repo grep empty); scoring remains hard
  pass/fail with signed surplus/deficit (`_score_attempt`,
  `pyqenc/quality.py:498-571`).

**Questions to think about:** score = Σ |measured − target| / meaningful
range, so metrics can balance each other; "targets met" as separate sign?
How would it interact with CRF search monotonicity?

---

## 🤔 19. Optimization ranking is size-only; no success rate / score in summary

**Status:** needs thinking (partially addressed)

- `StrategyTestResult` carries only `strategy_name` + `total_size`
  (`pyqenc/state.py:269-278`); ranking is `sorted(..., key=total_size)`
  (`pyqenc/phases/optimization.py:461`) with tolerance selection
  (`_apply_tolerance`, `:645-668`). The summary table shows a passed/failed
  status column (`:670-707`) but no success-rate %, no score, and no
  exhausted-attempts vs targets-met distinction. Original motivation:
  AV1-FGS.

**Questions to think about:** rank by success % (then size)? Add average
negative score for failures?

---

## 🤔 20. Optimization test-chunk selection parameters are hardcoded

**Status:** needs thinking (config gap)

- `_select_test_chunks(chunks, percentage=0.01, min_chunks=3,
  exclude_start_percent=0.10, exclude_end_percent=0.10)` — hardcoded defaults
  (`pyqenc/phases/optimization.py:838-843`), called with no overrides
  (`:390,392`). `app_config.py` has only `optimize_tolerance` (`:188`) — the
  selection percentage is not configurable.

**Questions to think about:** config fields (percentage / min_chunks /
exclusions) under an `optimization:` subconfig?

---

## 🤔 21. Quality-target presets (high / medium / low) don't exist

**Status:** needs thinking (UX feature)

- CLI accepts only raw pairs `--targets vmaf-min:95,...`
  (`pyqenc/cli.py:43-45,227-231`); `profile[+preset]` patterns
  (`cli.py:239-243`) are encoder speed presets, unrelated. No named target
  bundles anywhere in config.

**Questions to think about:** `--target <preset>` alongside `--targets
<raw list>`; presets defined in config (metrics per preset)?

---

## 🤔 22. No filename template system ({fps}, {title}, …)

**Status:** needs thinking (feature)

- Final output name is a hardcoded f-string
  `f"{source_stem} {safe_name}.mkv"` (`pyqenc/phases/merge.py:838,941`);
  no template dict / `format_map` mechanism exists (only the unrelated codec
  `encoder_args` templating, `pyqenc/models.py:174-180`). `{fps}` does not
  exist.

**Questions to think about:** output-name templates as a config dict
(name → format string, case-insensitive keys)? Which parameters: title, fps,
year, strategy?

---

## Architecture

## 🤔 24. Phase registry is a static hand-ordered list, not derived from dependencies

**Status:** needs thinking (partially addressed)

- `_build_registry` is a static ordered construction list
  (`pyqenc/phase.py:320-438`); the Probe-omission-when-`video_required=False`
  part is done (`phase.py:355-360,410-436`). But nothing derives the registry
  from the terminal phase's dependency graph — `api.py` passes an explicit
  `target` (`api.py:52,100,114,119`); only execution is dependency-driven.

**Questions to think about:** auto-populate the registry from terminal-phase
dependencies (that's what the dependency declarations are for)?

---

## 🤔 25. Strategies degrade to strings; folder-name sanitize duplicated in 4 places

**Status:** needs thinking (consolidation; "+" idea abandoned)

- `EncodedArtifact.strategy: str` (`pyqenc/phases/encoding.py:1488`);
  `encode_all_chunks` takes `list[Strategy]` but immediately strips to names
  (`:1335` → `_recover_encoding_attempts(..., list[str])`, `:272-275`).
- `":"→"_"` mapping is duplicated: `Strategy.safe_name`
  (`pyqenc/models.py:169-171`), `_enc_encoded_strategy_dir`
  (`encoding.py:266-269`), and twice inline in merge (`merge.py:898,935`).
- Note: the earlier "`+` is a fine symbol / поменяляли, но артефакты
  теряются" concern is moot in the current tree — no `+` handling exists,
  all sites map identically, and `_recover_encoding_attempts` re-indexes
  `encoded/<strategy>/` from a fresh directory listing each run
  (`encoding.py:277-330`); no artifact-loss path found.

**Questions to think about:** sanitize once — validate in `Strategy` (or one
helper) and use `safe_name` everywhere? Keep merge string-based (artifact
boundary) as the earlier analysis concluded?

---

## 🤔 26. Metadata composition rework: File → Stream → fragment; eager, no lazy props

**Status:** needs thinking (big-ticket design; subsumes several old notes)

- Current state: flat lazy-property models — `VideoMetadata` is explicitly
  "transparent lazy-loading" over `PrivateAttr` fields
  (`pyqenc/models.py:450,481-517`), `ExtendedVideoMetadata(VideoMetadata)`
  (`models.py:725`), `ChunkMetadata(ExtendedVideoMetadata)` (`models.py:788`);
  `ChunkArtifact.metadata` lazy-loads from a sidecar YAML
  (`pyqenc/phases/chunking.py:353-368`).
- Lazy props force workarounds like the `chunk._resolution` lazy-probe dich
  (`encoding.py:775-778`).
- Subsumed old notes: (a) *don't extract video, reuse source file (−20–40
  GB)* — today `_extract_video_artifact` still copies the video track
  (`pyqenc/phases/extraction.py:1210-1235`) and chunking consumes the
  extracted file (`chunking.py:854-867`); (b) *extraction returns
  specialized Stream objects, extracts only subs/chapters/attachments*;
  (c) *RETHINK phase inputs/outputs / intermediate results*; (d) *Job phase
  owns fast probe, Probe phase slow probe, no hidden lazy attributes*.
  Artifacts already carry `stream: VideoStream/AudioStream` references
  (`extraction.py:655-690`) — a step in this direction.

**Questions to think about:** SourceMetadata = file info + fast video
metadata; extended = slow metadata; explicit ownership of fast/slow probe
results per phase. This is the natural entry point for the File → Stream →
Stream-fragment model.

---

## 🤔 27. Artifact subclass zoo vs. generic `Artifact[T]`

**Status:** needs thinking (API design)

- Base dataclass `Artifact(path, state, wanted)` (`pyqenc/phase.py:61-87`)
  plus per-phase subclasses adding typed fields: extraction tracks
  (`extraction.py:649-697`), `AudioArtifact` (`audio.py:74`), `ChunkArtifact`
  (`chunking.py:353`), `EncodedArtifact` (`encoding.py:1478-1490`),
  `MergeArtifact` (`merge.py:93`). No generic `Artifact[VideoMetadata]` /
  `Artifact[AudioMetadata]` payload typing.

**Questions to think about:** generic `Artifact[T]` with typed payload — what
breaks (per-type fields like crf/strategy)? Worth it?

---

## 🤔 28. ffmpeg command construction is plain functions + ~20 hand-built cmd lists

**Status:** needs thinking (refactor)

- `utils/ffmpeg_runner.py` is functions + result dataclasses
  (`run_ffmpeg_async` `:333`, `run_ffmpeg` `:431`, `get_frame_count` `:485`).
  Call sites hand-build `cmd` lists: `encoding.py:689`, `merge.py:1009`,
  `extraction.py:1220,1247,1288,1307`, `audio/chain.py:332`, `quality.py:440`,
  `models.py:564`, … No builder class owning input / filtering / output
  sections (and progress tracking) exists.

**Questions to think about:** an ffmpeg command class with explicit input /
filter-chain / output stages? Ties into §26 (accept Stream / Stream-fragment
as input).

---

## 🤔 29. Frame-count probe duplicated (the "0:v:0" ×2 note)

**Status:** needs thinking (dedup)

- `VideoMetadata.probe_extended` hand-builds the same
  `-map 0:v:0 -c copy -f null -` command and calls `run_ffmpeg` directly
  (`pyqenc/models.py:553-577`) — duplicating
  `ffmpeg_runner.get_frame_count` (`utils/ffmpeg_runner.py:485-511`).

**Questions to think about:** models.py should call `get_frame_count` (or get
the count from the ffmpeg run that already happens there).

---

## 🤔 30. Multi-pass video (audio chains already do it)

**Status:** needs thinking (feature)

- Audio chains run K measurement passes + 1 final application pass
  (`pyqenc/audio/chain.py:274,366-370,436-463`; measured filters incl.
  loudnorm/volumedetect, `audio/filters.py:21-29`). Video encoding is
  single-pass CRF search — no `-pass`/pass-log flags anywhere in
  `encoding.py` / `ffmpeg_runner.py`.

**Questions to think about:** is a 2-pass analog (or reuse of the CRF-search
attempts as "measurement") worth anything for video?

---

## 🤔 31. `to_yaml_dict` / `from_yaml_dict` — 13 hand-written pairs

**Status:** needs thinking (premise partially outdated)

- All 13 pairs live in `pyqenc/state.py` (ProbeState `:95/:108` …
  ChunkSidecar `:825/:837`). They are NOT trivial `model_dump` wrappers as
  the old note assumed — most are deliberate selective/delta serializers
  (e.g. ProbeState persists only `frame_count`+`crop`, `state.py:95-104`;
  JobState uses `model_dump_full` + Path→str, `:156-161`), though a few are
  near-trivial (EncodingParams `:399-413`).

**Questions to think about:** prune the near-trivial ones onto
`model_dump`/`model_validate`; document the delta-serialization contract for
the rest?

---

## 🤔 32. `dict[type[Phase], Phase]` pattern ×13; quoted `cast()` ×8 remain

**Status:** needs thinking (typing polish)

- `dict[type[Phase], Phase]` (and quoted variants) at `phase.py:330,378,387`,
  `runner.py:106,115`, `job.py:99`, and each phase `__init__`
  (`extraction.py:742`, `probe.py:96`, `audio.py:132`, `chunking.py:419`,
  `optimization.py:118`, `encoding.py:1523`, `merge.py:546`).
- Quoted forward-ref casts remain at `probe.py:107-108`, `extraction.py:752`,
  `encoding.py:1534-1537`, `merge.py:560` (others already de-quoted).

**Questions to think about:** is the type-keyed dict load-bearing (registry
lookup) or replaceable? De-quote remaining casts where imports allow.

---

## 🤔 33. Disk-space estimation is JobPhase-only, log-only

**Status:** needs thinking (partially implemented)

- A size-estimation module exists (`utils/disk_space.py`, `SpaceEstimate`,
  pixel/bpp heuristics) but is invoked only from `JobPhase.run` on cached
  `job.yaml` metadata (`pyqenc/phases/job.py:150-166`); the
  insufficient-space FAILED branch is commented out — "I don't want to block,
  just notify" (`job.py:167-181`). Not per-phase, not based on actual
  extraction/chunking results, no partial scanning.

**Questions to think about:** per-phase re-estimates after extraction /
chunking with real numbers? Keep log-only?

---

## 🤔 34. Emoji visual-hash pool is curated, not exhaustive

**Status:** needs thinking (minor)

- `visual_hash()` maps MD5(strategy:chunk_id) → emoji over a curated pool of
  290 wide + 14 narrow emojis with documented rendering exclusions
  (`pyqenc/constants.py:282-359`, `pyqenc/utils/log_format.py:158-173`).
  No expansion mechanism; "full emoji list" was the original wish.

**Questions to think about:** worth expanding (collision radius vs terminal
coverage)?

---

## Config & housekeeping

## 🤔 35. Config is plain BaseModel — no pydantic-settings, no ENV support

**Status:** needs thinking (partially done)

- Layered pydantic subconfig models exist with documented priority: bundled
  default → `~/.config/pyqenc/config.yaml` → `./pyqenc.yaml`
  (`pyqenc/app_config.py:49-83,812-864`), CLI args override config after load
  (`pyqenc/cli.py:284-340`). But everything is plain `BaseModel` — no
  `BaseSettings`, zero ENV support (grep empty).

**Questions to think about:** migrate to pydantic-settings with ENV vars?
(Old DeepSeek chat link in the source notes is the original sketch.)

---

## 🤔 37. Import cleanup (mid-file, stale, cyclic-import care)

**Status:** needs thinking (localized)

- Mid-file *module-level* imports persist in two of the largest files:
  `pyqenc/phases/chunking.py:328-348` (duplicate `TYPE_CHECKING` import of
  line 24, plus re-imports of constants/phase helpers) and
  `pyqenc/phases/extraction.py:527-533` (mid-file TypeAlias/constants/models
  imports incl. `from pyqenc.models import AudioMetadata` duplicating line
  31). encoding/optimization/job are clean at module level.
- Cyclic-import care is currently structural (deferred imports inside
  `_build_registry`, `phase.py:382-418`) — any cleanup must preserve that.

**Questions to think about:** one cleanup commit hoisting these to top?
Combine with §4's ruff pass?

---

# Checked against `D:\todo pyqenc.md` — already resolved (not carried over)

- **"no app metrics from extraction" / "no dotted app metrics from
  extraction"** — extraction now records top-level `extraction` + `recovery`
  (`extraction.py:808,864`, the 0.14.2 fix); dotted keys under `extraction`
  are intentionally absent per spec (staleness of that spec motivation is
  tracked in §2).
- **"remove phase.scan; run-only model; pipeline shouldn't hard-fail;
  recover internal, returns all artifacts; per-phase _recover audit"** —
  done. No `scan` exists; `Phase` has `run()` (`phase.py:207`);
  `Runner.run` never raises on FAILED (`runner.py:166,233-246`), dependents
  chain FAILED with a clear reason via `resolve_dependencies`
  (`phase.py:275-313`); `_recover` returns ALL artifacts with `run()` doing
  the single wanted-filter (extraction `extraction.py:947,820`, audio, merge;
  chunking/encoding equivalents verified).
- **"Attachments are not extracted by ffmpeg somewhy"** — fixed:
  `AttachmentStream` + `ffmpeg -dump_attachment:{track}`
  (`extraction.py:253-273,1328-1341`). Note: attachments are still never
  merged back — final output is video-only by design (`merge.py:8-10`,
  `--exclude` example at `cli.py:167-169`).
- **"h.264 codec_private_data differs between chunks on merge"** — handled
  via `-x264-params profile=high:level=5.1:sps-id=0`
  ("ensures consistent codec_private_data across chunks",
  `pyqenc/default_config.yaml:153-154`); PTS restored by mkvmerge
  `--timestamps` (`merge.py:1217`).
- **"DynAudNorm — set to 3…4 max"** — `maxgain: 3.0`
  (`pyqenc/default_config.yaml:76`). (No hard cap validation on
  `DynAudNormParams`, `audio/filters.py:242-246`, but the value is applied.)
- **"codecs / profiles rework (codec = encoder setup, profile = tuning incl.
  quality control)"** — implemented: `CodecConfig` owns quality control
  (`models.py:331-371`), `ProfileConfig` is the tuning layer
  (`app_config.py:100-134`), strategies resolve from `profile[+preset]`
  patterns (`app_config.py:175-273`, `Strategy` at `models.py:136-171`).
- **"Audio config — split into convert.* / normalize.* / dynaudnorm.*"** —
  superseded by the audio-chains rework: `AudioConfig` is now a filter
  palette + ordered chains + track select (`app_config.py:382-427`,
  `audio/filters.py`).
- **"quality.py `normalize_metric` — useless"** — removed; only the used
  `MetricInfo.normalize` remains (`quality.py:114`).
- **"`+` in file names — поменяляли, но теперь артефакты теряются"** — the
  `+` experiment is absent from the tree; `":"→"_"` is applied consistently
  and recovery re-indexes from disk each run; no artifact-loss path found
  (residual dedup concern tracked as §25).
- **VSCode Mermaid `/generate_diagram_from_code` tip** — tooling note for
  the editor, not a project task; intentionally not carried over.
