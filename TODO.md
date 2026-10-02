# Project TODO — findings needing thought

Items are **observations with evidence, not decisions** — each needs a design
call before any implementation. Nothing here is blocking. Entries are removed
outright once a plan covering them is finalized or they are fixed — git and
spec history are the record.

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

Also, it looks like parallelism should be separate for video and for audio. For video in general parallelism should be == 1 by default.
For audio it can be higher. Probably 2 or 4 by default.

Worth noting too. With parallelism==1 we could stick to sync mode. While parallelism >1 requires async.

parallelism could also be named concurrency.

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

## 🤔 6. Metrics I/O failures are silent (WARNING only)

**Status:** needs thinking (deliberate trade-off, worth revisiting)

- `_write_atomic` swallows `OSError` (`pyqenc/metrics.py:752-760`);
  `_try_resume` swallows load failures (`pyqenc/metrics.py:590-592`).
  Non-fatal by design — but a user can finish a multi-hour encode and
  silently end up with a stale or missing `metrics.yaml`.

**Questions to think about:** is WARNING sufficient? Surface
"metrics not written / resumed from stale file" in the end-of-run summary?

Human input: metrics are not what the user wants to get - he wants to get audio/video processed. So it's just an extra nice-to-have info.
Warning is enough, but maybe make it log rarer, like exponential delay (first failure - ignore, happens; on second failure - log warning; on forth failure - log failure that still failing after N attempts; max at say 8 failures to write - after that keep logging every few failures; on success - reset counter))

---

# Imported from `D:\todo pyqenc.md` (verified 2026-09-20)

## Correctness / invalidation

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

## 🤔 11. Per-phase invalidation is unvalidated

**Status:** needs thinking (review)

- Each phase hand-lists its invalidation criteria, effects and sidecar
  fields. Review them from a logical perspective — per phase: which criteria
  and effects invalidation SHOULD have, and which sidecar fields that really
  requires — versus what the code does today. Refresh
  `parameters-phases-invalidation.md` from the result (it is stale).

**Questions to think about:** rebuild the matrix from first principles;
drop stale rows and fields; is config-fingerprint-based invalidation
warranted anywhere?

Need to build first the inputs per phase (what it consumes from user/cli, config, dependencies phases).
Then we need to articulate what changes on each input change. Does that really affect our invesment and needs a new investment? Or is that just an instant selection change for example?
Say, tolerance for optimization phase - its change - does it really need reinvestment, if we previously fully completed optimization? My best guess is - no - we have all previous results on sidecar, it is instant re-evaluation.

After that we need to check code to see which invalidations are actually in-place (only after logical part, not to interfere with logical thinking).
Cross-check results from logical vs code.

One thing to note here. We must reuse existing objects. I don't want introduction of custom objects. So if tolerance is part of that - it's ok to keep it for example. But if it is just a standalone field on the sidecar - definitely worth assesment.
After that we must stop and validate with human the reasoning.

This is also a chance to reestablish sidecars using class model introduced in file-stream-model and artifact-model recent specs, or the prior config rework spec - to use standard objects on sidecars.

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
- Related (ex-§73): with p10 now targetable, adaptive sampling must keep
  enough readings for it — min 20 readings so `vmaf-p10` spans >=2 frames;
  when the chunk has too few frames, revert to a lower sampling value.

**Questions to think about:** auto-set sampling=1 below a frame-count
threshold (with a log line)? Adapt sampling down when frames/sampling < 20?

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
<raw list>`; presets defined in config (metrics per preset)? What would be a good UX for the end user? Easy to understand, difficult to make mistake. Maybe make `--targets` naming more verbose intentionally?

Something like:
- ultra - should match approximately CRF 12-14 quality (say, 0.25 bits per pixel expected bitrate)
- high - should match CRF 16-18 approximately (~0.17 - those are just examples)
- medium - should match CRF 20-22 approximately (~0.1 - for the free space estimation)
- low - should match CRF 25+ approximately (~0.08 - if we don't have other means to estimate)

With some default profile being default (similarly to codec's default preset in config) - used if user told nothing. Probably `high`

CRF above is when using H.265-anime at slow preset. One problem is how to estimate that. Pipeline runs on chunks - more granular quality assessment.
But we can use built-in profile quality range limiting for running fixed-CRF encodes on chunks to get the quality measures.

Another question is I want targets to favor retaining natural film grain, not favoring blurred results. h.265-anime is particularly tuned for this, so probably we should run 2 encodes at fixed CRFs - with plain h.265 and with h.265-anime variant at the same CRF values. And compare measured metrics to better understand the difference - where we could give more slack, where we should make things tighter (in favor of retention of original looks, but at sane quality levels).

---

## 🤔 22. End-user output-name template system ({fps}, {title}, …)

**Status:** needs thinking (feature, low priority)

- Not a naming-ownership concern — the merge-output name family has a single
  owner (`MergedVideo`, `2026-09-28 artifact-model` spec). This item is the
  end-user feature: config-defined templates for final output file names
  that the owner consumes.

**Questions to think about:** template syntax and config shape; which
parameters to expose (title, fps, year, strategy, …)?

---

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

## 🤔 33. Disk-space estimation is JobPhase-only, log-only

**Status:** needs thinking (partially implemented)

- A size-estimation module exists (`utils/disk_space.py`, `SpaceEstimate`,
  pixel/bpp heuristics) but is invoked only from `JobPhase.run` on cached
  `job.yaml` metadata (`pyqenc/phases/job.py:150-166`); the
  insufficient-space FAILED branch is commented out — "I don't want to block,
  just notify" (`job.py:167-181`). Not per-phase, not based on actual
  extraction/chunking results, no partial scanning.
- 2026-09-27: landed with `2026-09-25 file-stream-model` (Task 4) — estimation
  now runs in ExtractionPhase on the enumerated `VideoStreamInfo` (attempts +
  finals only; the FFV1/remux/extraction terms are deleted). The remaining
  open question is unchanged:

**Questions to think about:** per-phase re-estimates later in the pipeline?
Keep log-only?

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

## 38. Single source of truth enforced via explicitly required function arguments

One of my key mottos is - one source of truth. For config - that's default_config.yaml, which must provide all needed values.

```
    def __init__(
        self,
        quality_evaluator: QualityEvaluator,
        work_dir:          Path,
        collector:         MetricsCollector,
        crop_params:       CropParams | None = None,
        cleanup_level:     CleanupLevel      = CleanupLevel.NONE,
        visual_hash:       bool              = True,
        metrics_sampling:  int               = 3,
        metric_prefix:     MetricKey         = MetricKey.ENCODING,
    ):
```

Defaults in functions for values, which must come from config - is the way to shoot your leg. metrics sampling - is part of config, it must never have its own default in code. Visual hash is in config - it must never have its own default in code. Metric_prefix is a persistent value, init default doesn't look like correct place, but that's ok in this paradigm - it is not in the config (giving as an example; key rule - single source of truth; default_config is the first source of truth for any value defined there).

Need to check footprints and adjust accordingly for explicitly required values (what is provided from config).

---

## 39. 🤔 Forced wipe idempotency

Currently forced run is a flag on Job phase result, which must be respected by each phase. Problem is, if we don't run till the end and exit in the middle,
but we've already written new job.yaml sidecar - on rerun we won't know there was a source mismatch. Later phases which didn't reach running on previous
correctly flagged run - won't have this extra bit of info to invalidate artifacts and we will get inconsistent output.

The right approach is probably something like `finalize` but reversed (finalize runs after succesfully finishing the job) - `invalidate` maybe or something like that.
Explicitly triggered once in reversed order (from end). And only after that is triggered - the forced wipe flag becomes unneeded and we can write new job.yaml sidecar.

Or... should we remove it completely?
- The only true usecase is when crop changed between runs. And here it works as a safeguard against accidentally deleting a lot of work (all attempts become invalid; not detectable with current light invalidation checks; don't want per-attempt sidecar reading for heavy invalidation checks for this usecase as that will affect all runs).
- If user wants another file - he can either use new dir or purge current dir. So this one isn't a true usecase.

---

## 42. Adding ability to fix quality range has broken optimization phase

Previously we were selecting the best strategy via optimization phase - encoding a small subset of chunks using all strategies. Our principle was - we reach targets.
With fixed quality (range min=max) the principle of reaching targets is broken.

Probably a composite score should be added and used for selection of the best (optimal) variant. Maybe with a cli option allowing either strict min or generic optimum score
for selection:
- Min (current default and future default?) - negative score on any missing target and calculate only based on missing targets. Positive only if all targets matched - then score the positive delta.
- Optimum - score all metrics (both missing and matching) for a single weighted score.
- Should the score include the size? Size is basically a price for the score, where the score is the profit. Must reach some balance - smaller size is prefered,
  while too high score doesn't outweight the size (so that we don't blindly always pick largest size - quite the contrary, target is to reach smaller size while
  keeping good enough quality).

---

## 45. Summary table for audio

Like for extraction phase - I want a similar table for audio phase - a summary on which streams were picked up (wanted flag), maybe with a chain counter per stream. Maybe also add after the table - the selector - selected tracks (as a clear reason for the choice).

With the `2026-09-28 artifact-model` spec the data lives in one place: the audio ledger rows (`Artifact[AudioOutput]` = source stream + chain, with `wanted`) — pure UX on top.

---

## 47. Scene detection threshold search

Default doesn't always produce wanted results.
Add a searching mechanic, similar to quality search? With something like 0.15 - 0.5 range and a starting point of say 0.27?
Target is to get small chunks - not over a minute probably (it is a rare thing for movies to have such a long scene).
But not to split on every min frames (too low value).
With all params being configurable in the config, including ability to disable the search.

Why? I'm often seeing a movie being split like - titles, full movie as SINGLE HUGE chunk, finals as separate chunks.

---

## 48. Remove include/exclude filters?

Previously include/exclude filters were made specifically for extraction phase - it was costly to extract everything. Also, this was the only mean to
actually control which audio gets processed.

Now, we have video/audio streams passthrough mechanics.
Now, we have audio selectors, which give a more controllable choice with sub-preferences.

It looks like we should consider:
- completely removing the include/exclude filters (materialize all non video/audio things; video and audio has separate processing)
- or maybe keep filters but re-add the ability to materialize audio/video (not for processing, but for the end-user) via some special option? Maybe even with video/audio stream retartgetting to extracted files, if actually materialized.

---

## 50. Better space estimations

In general I like how space estimation looks. Maybe we don't need required/recommended if we already have a min-max values.
The numbers on space estimations look to be quite off the actual. We need some way to better estimate space knowing the config (cleanup levels, bitrates, etc; what to do with targets?) and streams available (number of audio streams; video stream presence, etc).

> 2026-09-28 12:13 [[96mINFO[0m] Source video size            0.35 GB
> 2026-09-28 12:13 [[96mINFO[0m] Estimated required space     1.49 ... 4.48 GB
> 2026-09-28 12:13 [[96mINFO[0m] Estimated recommended space  1.79 ... 5.38 GB
> 2026-09-28 12:13 [[96mINFO[0m] Available space              198.82 GB

Just a few thoughts:
- maybe the phase itself should give estimation? Like finalize, but the first call instead to ask for space estimation. Not sure, looks difficult
- maybe for targets we should add profiles. Something like targets profile "high", "medium", "low", with their own sets of targets each. But also a hint on approximate bits per pixel? like 0.1 for medium, 0.15 for high, 0.08 for low?
- need a research there

## 51. QualitySearchV3 is outright broken.

One of examples - it gets the same values on CRF 12.0 and 11.5 and starts treading back upwards, even though the direction wasn't exhausted and no passing (opposite result) was achieved.

```
2026-09-28 17:40 [[96mINFO[0m] 👊 ｟ultrafast+h265-anime ｠ 00꞉05꞉49․140-00꞉06꞉32․601 attempt #1: ✘ miss with CRF 18.0 (psnr_min=16.6  psnr_median=44.4  ssim_min=60.2  ssim_median=98.4  vmaf_min=0.0✘ vmaf_median=93.6  vif_min=4.9  vif_median=92.2)
2026-09-28 17:42 [[96mINFO[0m] 👊 ｟ultrafast+h265-anime ｠ 00꞉05꞉49․140-00꞉06꞉32․601 attempt #2: ✘ miss with CRF 12.0 (psnr_min=16.6  psnr_median=48.8  ssim_min=59.9  ssim_median=99.2  vmaf_min=0.0✘ vmaf_median=96.1  vif_min=4.9  vif_median=96.2)
2026-09-28 17:44 [[96mINFO[0m] 👊 ｟ultrafast+h265-anime ｠ 00꞉05꞉49․140-00꞉06꞉32․601 attempt #3: ✘ miss with CRF 11.5 (psnr_min=16.6  psnr_median=48.8  ssim_min=59.9  ssim_median=99.2  vmaf_min=0.0✘ vmaf_median=96.1  vif_min=4.9  vif_median=96.2)
2026-09-28 17:46 [[96mINFO[0m] 👊 ｟ultrafast+h265-anime ｠ 00꞉05꞉49․140-00꞉06꞉32․601 attempt #4: ✘ miss with CRF 15.0 (psnr_min=16.6  psnr_median=46.7  ssim_min=60.0  ssim_median=98.9  vmaf_min=0.0✘ vmaf_median=95.2  vif_min=4.9  vif_median=94.5)
2026-09-28 17:49 [[96mINFO[0m] 👊 ｟ultrafast+h265-anime ｠ 00꞉05꞉49․140-00꞉06꞉32․601 attempt #5: ✘ miss with CRF 13.5 (psnr_min=16.6  psnr_median=47.2  ssim_min=60.0  ssim_median=99.0  vmaf_min=0.0✘ vmaf_median=95.4  vif_min=4.9  vif_median=94.9)
2026-09-28 17:51 [[96mINFO[0m] 👊 ｟ultrafast+h265-anime ｠ 00꞉05꞉49․140-00꞉06꞉32․601 attempt #6: ✘ miss with CRF 13.0 (psnr_min=16.6  psnr_median=48.1  ssim_min=60.0  ssim_median=99.1  vmaf_min=0.0✘ vmaf_median=95.8  vif_min=4.9  vif_median=95.6)
2026-09-28 17:54 [[96mINFO[0m] 👊 ｟ultrafast+h265-anime ｠ 00꞉05꞉49․140-00꞉06꞉32․601 attempt #7: ✘ miss with CRF 12.5 (psnr_min=16.6  psnr_median=48.1  ssim_min=60.0  ssim_median=99.1  vmaf_min=0.0✘ vmaf_median=95.8  vif_min=4.9  vif_median=95.6)
```

We need to rework this again and make a v4:
- We need to merge V2 and V3 ideas
- Basic idea is the same as it was in V2 - by default we work in 2-point mode, reducing the window range for quality, until we converge - either left or right side. And at some point we go for 3-way attempt to find sweet spot - question is when.
- Idea from V3 with allowing extrapolation outside the range - holds, but we probably need to add a min step - half of leftover range to that side. Min - and extrapolation - are for the same reason, to make initial steps larger, until we find a breaking point or exhaust search range.
- Idea from V3 that if we reached boundary without success and need to do 1 binary step back - holds. If we go too fast - we could miss a sweet spot. But this only applies to moving towars higher quality (i.e. all attempts failed yet). For going towards worse quality we DO NOT CARE about sweet spot at all - we reduce quality as long as it still passes our metrics.
- Criterias for 3-way sweet spot change:
    - When go towards lower quality (i.e. we do have a success already) - continue as 2-point up to any point until we reach boundary or get a miss.
    - 3 way activates only when we do NOT have a success (not once) + reached the highest quality boundary + made a step back. This is the only way to activate 3-way. But needs 1 extra condition - best matching point (a failure still) is in the middle, i.e. we have 2 other points with worse score.

Scoring, points selection, extrapolation - all look working in v3. Just conditions are wrong. And the code is too complex, I believe it could be simplified.

One note worth attention in example above are points CRF 12.0 and CRF 11.5 - they get absolutely the same score. This ruins the line slope and can't really use 3-way algorithm there. Need to think how to work with that. Maybe dedup such points during 3-point selection (i.e. try picking next point in that direction till it differs from middle point, where middle point is the best score (Failing)?)? Not sure, but this happens quite often (often binary-identical files).

I also want your take on search algorithm:
- we have starting point and range to explore
- encoding is heavy-weight, so we need to minimize number of steps
- we need to pick attempt with optimal score
   - optimal score currently is the closest to 0, but positive score is better then negative score.
- quality must be granular (respecting quality granularity)
- duplicate quality must never be seen for the same attempt
- purpose is to select least size output (normally - size is directly related to quality value), while matching the score; with minimal possible number of steps
- a fast exit on miniscule difference is acceptable
- there could be cases, where we never reach wanted targets - in this case we should find the best scoring quality value (could be non-linear; like a quadratic / polynomic curve, but considering we have limited points (3-4 max normally) we can't really find its true form)
- one extra note to consider - after the first attemp we do have multiple points and measurements.

Current V3 search normally converges in ~4.1 attempts.
Old V2 search converged normally in ~3.8 attempts, but had problems with attempts where we never reach a passing score (binary convergence is too slow for this case, like 8-10 attempts there).
I belive a value of ~3.5 is possible for convergence. This directly affects speed of the pipeline, as each encode is very costly.

---

## 52. h264 codec private data differs between winning encoding attempts

h264 uses a single instance of codec private data. If it differs between chunks' winning attempts - h264 video cannot be merged and played later, unlike h265 or av1, which have local copies in blocks.
Need to investigate why this is happening and if we can fix this without ruining processing for other codecs.

---

## 53. Ensure mkvextract with fallback

We are targetting MKV source and output as the key targets. Other outputs are not supported. Other inputs - well, we should try to use them.
So the policy is - we should use mkvextract first where possible. But have a fallback in-place if that fails. Prefer try-except flow semantics (i.e. if we failed to get fps - that's a failure -> try next variant).

Other acceptable tools: ffprobe, ffmpeg. Maybe something else.

This is not applicable to actual encoding/processing - both audio and video processing we aim to do with ffmpeg only.

For the outputs - we support only mkv and we prefer mkvmerge.

Need to check code. Especially metadata extraction, files materialization (extraction phase), screenshotting.

---

## 54. Validate Phase's DEPENDS_ON by actual usage

Validate Phase's class field DEPENDS_ON vs actual inputs used from those phases via `_deps` or similar mechanics. We need a clean concise dependencies graph.
Also need to build a table or a graph with actual dependencies - inputs, internals, results - both artifacts and settings. between phases and phase's internals.

---

## 55. Fixed-crf encoding

Current UX for fixed CRF encoding is rather complicated for end-user, involving config editing (duplicate profile and fix there; or do profile override for quality range).
There should be some easier way. Problem is how to fluently do that without affecting multi-strategy settings. Maybe enforce single strategy if fixed crf key is used?
Needs thinking.

---

## 59. Parallel metrics

Currently, if we run 2 jobs onto the same folder (say, separate video and audio passes) - metrics will get garbled.
Not sure this is a really required thing, but we can think of how to alleviate this. My current thoughts:
- a lock file (as a marker that metrics are being written right now). If the dump sees this - it can either postpone the write (with limit how far) or wait a bit for the write.
- in-memory store of only a delta since last dump. Writing becomes - read-modify by delta-write
- same old atomic writing
- release of lock file

On the other hands, other sidecars could also be modified. Probably not worth the effort. Need to think this over.

---

## 60. Logging review for debug and higher

Current logging is inconsistent.

What should be:

- every long job and every external call (ffmpeg, mkvextract, etc) should get a DEBUG level starting and finished line. Starting line should include full command (or details on what is being ran if that's not a command running). Ending line - same details (as a way to identify for parallel logging) and results of the run - success, failure, etc.
- INFO level and higher are only allowed from public footprint functions. Runner, phases basically, they control what user sees.
- if there's an error - we categorize it if this prevents further work or not. If we can continue - that's a warning (like, missing targets on quality). INFO line is not a place for warnings (like using emoji warnings).
- CRITICAL level if reserved only for catastrophic failures.
- Exceptions shouldn't be printed probably, at least to info and higher levels. Ordinary users are affraid of such walls of text. But there must be a way for us (developers/AI) to get them.
  Could be flag-walled, or debug level printed (reusing our printing level flag).
- For exceptions there must be clear concise proper-level messages logged, which include - what happened, how that will affect end-user (pipeline broken, exitting, can't continue, quality can't be reached; something end-user can understand).

Ideally, internal things shouldn't log anything but debug messages and propagate their problems via exceptions higher, where proper logging should take place. But if they already log - need to review if it should really log there or if it should be moved.

---

## 62. Sidecars - are owned by the Phase

Phase sidecars and internal machinery of the phase. Not part of model or stream_model. No1 else by their respected phase should ever be accessing them.
Probably worth moving to the phase.

---

## 64. Audio chain as flt

Need to check if full audio chain is converted to flt or only on downmix filter. Probably always using flt is better for precise. But only downmix using custom weights should be capable of producing clipping?

---

## 65. Cleanup 2026-09-30 — deferred findings (from cleanup-report.md)

Covered by the cleanup branch and closed: old §4 (ruff gate), §5 (metrics spy
tests → metrics.yaml behavior tests), §43 (stale audio tests), §58 (comment/
dead-code sprawl). Audit: `cleanup-report.md`.

Deferred as costly/structural:

- encoding⇄optimization import cycle (marked `# deferred: circular import` at
  3 sites) — worth breaking properly.
- CLI `_cmd_*` bodies ×6 near-identical (crop-parse → build config → api → log).
- Test fixture factories duplicated across files (Strategy/CodecConfig,
  extended-stream, encoded-chunk builders) → conftest consolidation.
- `EncodedChunk` composition duplicated (`_pair_placeholder` vs
  `build_encoded_chunk` shapes).
- ASCII summary-table scaffold duplicated (optimization vs merge).
- measure→extraction private imports (`_probe_streams_json`, `_video_info`)
  and api→measure `_parse_duration` — re-homing per §57.
- Broader test-surface rework beyond metrics (string-format pinning etc.);
  skipped integration tests need a real run on a media sample.
- ty: 83 diagnostics remain (type-modeling noise on pydantic patterns; one
  false positive on `MetricsCollector.step(*parts)` with zero parts).

---

## 66. CLI subcommands review

Do we really need all current subcommands? From UX point of view for end-user.
Current setup was mirroring the initial phases structure and allowed better testing for devs.
But ordinary user likely doesn't need that.

What would ordinary user need? I'd guess it should be intent based. Something like:
- auto - kept for full default pipeline
- video - for video only processing
- audio - for audio only processing
- measure - for measuring, including EXTERNAL videos (i.e. things produced not by pyqenc).

anything else?

I was thinking about a way to give user mechanics to extract (materialize) anything from source really.
Maybe this should be the function of `extract` subcommand (a bit different from extract mechanics in auto/video/audio), if we moved to virtual streams in main phases.
This is also a question specifically for audio - user might want source audio available as standalone files to use in external audio editors.
I was thinking of maybe introducing a dump filter (like passthrough, but to a file). But direct extract command might be a better way.
As another point for extract subcommand working like this - it was always a problem to just extract everything from mkv. Most CLI tools require explicit
streams listing, which is quite painful when making commands manually. with `extract` subcommand it could be something like `pyqenc extract source.mkv --exclude "video-" -y` to dump everything.
Ideally reusing the current extract phase mechanics.

other considerations?

---

## 67. The very first frame often gets very different measured quality from the rest of the video

I'm not sure what happens there. But many graphs look to start at nearly the same measured quality point at the first frame, as if it was fixed & controlled
by other means then the other video. I'm not sure if it is i-frame controls, or maybe measuring specifics, but ideally we need a way to affect that
or at least better understand it. It often affects min or max statistics, making them not very reliable.

Investigation needed.

---

## 68. Support invalidation of encoding/optimization results on config change for strategy

We already support config changes for audio chain via a full-string preservation and per-chain comparisson. This allows end-user to change the chain and get proper results still, even if previous outputs exist.

We should consider similar approach for encoding/optimization phases, where we use strategy name alone currently. Should we also support for params change invalidation (needs force wipe flag).

---

===

## 75. Flaky: test_pts_conversion_correctness under load — needs thinking

2026-10-01: failed once mid-suite (4-file batch: probe + merge_mkvmerge + pts_preservation + metrics_integration), then passed on immediate re-run of the same batch and in isolation. Suspect timing/IO-load sensitivity or shared-state assumption. Not reproducible on demand.

2026-10-02 (later): THIRD observation — 3-at-once mid-suite failures right after a mass file rewrite (LF normalization touched ~700 files; suite ran against regenerating pycache under heavy IO). Immediate clean rerun: 722/9. Escalating pattern: flakes track system IO/CPU load, not test logic. Candidate next step: run the suite under repetition (`pytest --count`-style or a stress-loop) to catch a name.

2026-10-02: second same-shaped flake in a targeted 3-file run (quality + vif + metrics; 1 failed of 129, name not captured) that passed on two immediate re-runs AND in the full suite. Pattern so far: ~1 flake per full-suite-scale run under load — worth a session with `-p xdist`-style repetition or last-failed + load when it recurs.

---

## 76. Reevaluate Python 3.15.x (lazy imports) — needs thinking

2026-10-02, after migrating to 3.14.7: 3.15 ships lazy imports (deferred module
import) — should significantly cut CLI startup time; worth a dedicated
evaluation+migration pass in a few months. Also worth watching in the same pass:
free-threaded builds (PEP 779, officially supported since 3.14) for CPU-bound
Python sections.

---

## 77. Major dependency upgrades review — needs thinking

2026-10-02: routine `uv lock --upgrade` is deliberately confined to within-major
bumps; taking a NEW major of any dependency must be a reviewed decision, not an
upgrade side-effect. First candidate: OpenCV 5 (constraint
`opencv-python-headless<5` in pyproject `[tool.uv]`; scenedetect transitive;
major API break — lift only deliberately). Then a scan of pydantic / matplotlib
/ pandas / pytest / alive-progress for new majors with relevant changelogs.

---

## 78. QualityPoint sentinel/measured split — refactor with the V4 spec — needs thinking

2026-10-02: QualityPoint overloads one class for two concepts — a measured
attempt (metrics dict) and an untested boundary marker (metrics=None,
is_sentinel, fake score=0 that also means "winner"). Split into
`QualityBoundary(q)` (sentinel) and a measured `QualityPoint(q, score,
metrics: dict)` with metrics REQUIRED. Kills: metrics | None, is_sentinel,
the `not self.is_sentinel` clauses in is_pass/is_fail/is_winner, the score=0
overload, and the narrowing asserts. Blast radius is quality.py only
(17 is_sentinel checks + 8 sentinel constructions; no direct test usage).
Do it as part of the QualitySearch V4 spec so the new algorithm builds on
the clean record type.

---

## 79. FilterInstance discriminated union — replace the registry cast — needs thinking

2026-10-02: `FilterInstance` (app_config) is an open registry model — `type: str`
+ `params: SerializeAsAny[BaseModel]` — so chain.py needs a
`cast(EncodeFilterParams, inst.params)` at the encode-conversion site. Proper
fix: per-filter Instance models (`type: Literal["<id>"]`, concrete params) +
pydantic discriminated union on `type`, defined in `audio/filters.py` (import
direction allows it: app_config already imports filters). Kills the cast
(isinstance/match narrows natively), mostly collapses the hand-rolled
`_resolve_and_validate` dispatch (keep error wording if needed). Openness stays
single-file: Params + Filter + Instance + union member. Blast radius:
app_config, chain, 3 test files. Until then the cast (or an assert-isinstance)
is the sanctioned interim.

---

## 80. LongPathYaml redefinition — cleaner editor experience + pyright-gate enabler — needs thinking

2026-10-02, pyright cross-check evaluation DONE and stance DECIDED (recorded in
agent-commands.md): ruff + ty are the static gates; pyright/Pylance stays an
editor aid + occasional probe (`uvx pyright --pythonpath .venv/Scripts/python.exe`).
Its real findings were fixed (LongPath operator params widened to StrPath,
dead Strategy.raw branch, Artifact wrap, Decimal literals in tests); the known
non-actionable families are documented in steering.

The one open idea from the probe: redefine `LongPathYaml` as
`Annotated[Path, AfterValidator(... -> LongPath)]` — the field then honestly
accepts `Path`, runtime still yields `LongPath`, both checkers satisfied, zero
call-site churn. It deletes the 51-squiggle `Path -> LongPath` family from the
VS Code experience outright (editor value today), and is the prerequisite step
if a pyright gate is ever wanted (recipe: this, then widen done, test Decimals
done, config `reportPrivateImportUsage: none`, a handful of per-checker ignores
for negative tests + lib gaps).
---

## 81. Quality targets should accept any separator, but use single stable form when serialized from our code

User should be able to use any standard separator for quality targets between metric and statistic, i.e. `vmaf_min` or `vmaf-min` or `vmaf.min` - all should be acceptable deser variants
probably `-_.` are enough. For the actual value separator `vmaf_min:97` or `vmaf_min=97` or `vmaf_min>97` probably also should be ok with no extra added meaning. Just a convenience.
But when we serialize to str - we should use one stable across all code form - which exactly? Not sure. Maybe `-` or `.`. Don't like the underscore `_` - it is usually used as a word separator inside single name, rather then between names.

Central name ownership (both serializing and deserializing) should also be considered per DRY rule.

---

## 82. Research: multi-metric quality scoring/evaluation — joint paper review for possible improvements

Born from the fixed-quality mode design discussion (2026-10-02). In fixed mode the
optimization phase loses its "targets met -> compare sizes" assumption (knob pinned,
strategies sit at different actual quality levels), so cross-strategy judging needs a
defensible multi-metric story. Current working direction: anchor-relative deltas
(size-winner strategy as reference) + Pareto dominance pruning, no composite scalar.
This item collects sources for a dedicated joint research pass over the papers.

Articles:

- Multi-objective Pareto vs scalar selection (Univ. of Oviedo, PDF):
  https://digibuo.uniovi.es/dspace/bitstream/handle/10651/85820/1-s2.0-S0167865526002977-main.pdf
  — empirical: Pareto advantage over scalarization is real but partial and
  metric-specific.
- Zwei: self-play RL for perceptual video coding (IEEE TMM 2021, PDF):
  https://godka.github.io/tmm21-zwei.pdf
  — VMAF used as THE perceptual scalar objective; canonical example of
  VMAF-as-trained-fusion being the industry's composite.
- Zhang et al., "Enhancing VMAF through New Feature Integration" (arXiv, 2021) —
  integrates new video features / alternative metrics into VMAF; documents
  base-metric gaps.
- "Gain of Grain: A Film Grain Handling Toolchain for VVC" (2024, ResearchGate) —
  conventional metrics don't model grain perception; FGS-aware evaluation.
  Directly matches our av1 observation (VMAF rewards grain removal/smoothing).
- Cloudinary, "Using VMAF with other metrics" (blog) — practical multi-metric
  pairing (VMAF primary + PSNR/SSIM as checks).
- AWS Elemental MediaConvert — per-frame quality metrics docs (PSNR/SSIM/VMAF/QVBR
  interpretation thresholds; production practice).

Known context going in: VMAF is spatial-only (no temporal effects) and treats grain
as distortion, so it rewards denoise/smoothing — our av1-at-same-CRF trap; VIF is
the texture-retention guard (already in our measured set; all metrics normalized
0-100). Open questions for the research pass: principled scalar fusion vs
per-metric constraints; grain-aware / temporal metrics worth adopting; BD-rate
applicability at single operating points.

---

## 83. Metrics-absence tolerance across consumers — prerequisite for measurement-skip

Born from the fixed-quality spec design (2026-10-02). Decision there: the
measure_attempts control ships with default = measure (current behavior kept).
Flipping the default — or skipping per-chunk measurements in fixed encoding runs —
additionally requires every consumer of attempt/winner metrics to tolerate their
absence: ChunkEncodingResult fields, winner sidecars, the winner-scan limiter
tallies (those self-extinguish — empty metrics -> find_worst_target returns None),
summary/log formatting, optimization's reads of encoding results, metrics-collector
keys / dashboards. A deliberate sweep, not just the flag. Reference: fixed-quality
spec (2026-10-02) defers this here; adjacent: §82 scoring research.

---

## 84. Re-home quality ranges: codec = actual codec range, profile = reasonable working band

Background: profiles originally had no `quality_range` override, so the practical
working band had to be defined on the codec itself — default_config.yaml comments
say it outright ("full codec range is 0–51; 6–30 is the practical working band").
Now that profiles narrow ranges (and the fixed-quality CLI override layers on top),
each value should live in its right layer:

- codec `quality_range` — the codec's ACTUAL domain (x264/x265/NVENC-CQ/QP: [0,51];
  AV1: [0,63]; NVENC-VBR: needs thinking — the 99.5 Mbit/s cap is already a
  practical cap, not a factual bound; vulkan QP: real range is −1–255, but values
  above ~50 produce visible artefacts — even "actual" needs a sanity discussion
  there)
- profile `quality_range` — the reasonable working band (today's [6,30]-style
  values move here; bundled profiles should all carry one, otherwise
  profile-less searches roam the full codec range)

Follow-ons to consider while at it: `default_quality` must stay inside the moved
bands (the fixed-quality spec's starting-point auto-adjust already covers
exclusion); `quality_log_padding` derives from range width (cosmetic); search
convergence from wider codec bounds (phase-0 half-range steps get coarser —
probably fine, verify); the fixed-quality `-q` override is bounded by the codec
range, so it gains the wider freedom too (consistent — CLI replaces the profile
band).

---

## 85. Exhausted-search log message is missing the limiter annotation

The success path logs the limiter: "success ✅ with CRF 11.5 after 3 attempts —
limited by vmaf_median". The exhaustion path ("CRF search space exhausted for
chunk ... after N attempts — accepting best attempt (CRF=...)") has no such
annotation. Add the same "limited by {metric_stat}" there — the worst /
constraining metric of the accepted best attempt (find_worst_target over the
best attempt's metrics, exactly like the success path computes it).

---

## Last known = 85

Keep this updated, so that we can keep continuous numbering even on last todo item deletion.
Keep this the last entry for easy human updates.

---
