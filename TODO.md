# Project TODO — findings needing thought

- Items here are observations or considerations. NOT DECISIONS.
- Each items needs validation and design consideration before any implementation.
- Items are to be deleted, once they go into the code (after spec is approved or after implementation if done without spec).

## 6. Working with app metrics I/O errors

- `_write_atomic` swallows `OSError` (`pyqenc/metrics.py:752-760`); `_try_resume` swallows load failures (`pyqenc/metrics.py:590-592`). Non-fatal by design — but a user can finish a multi-hour encode and silently end up with a stale or missing `metrics.yaml`.

Human input: metrics are not what the user wants to get - he wants to get audio/video processed. So it's just an extra nice-to-have info.
Warning is enough, but maybe make it log rarer, like exponential delay (first failure - ignore, happens; on second consecutive failure (2^1) - log warning; after another 4 consecutive failure (delta of 2^2 from previous) - log failure that still failing after N attempts; after another 8 failures (2^3 delta from previous; this is probably max reasonable step, which shouldn't increase further; even worse - might log on every failed consecutive attempt after that); on succesful write - reset the counter)

---

## 16. Measuring frames sampling is never disabled for few frames

- Sampling applies unconditionally via `select='not(mod(n,{subsample}))'` (`pyqenc/quality.py:377,400`); the only validation is `metrics_sampling >= 1` (`pyqenc/phases/measure.py:933-934`). No dynamic chunk's frame-count guard anywhere; `merge.py:404-405` only logs that subsampling may miss outliers.
- Related (ex-§73): with p10 now targetable, adaptive sampling must keep enough readings for it — min 20 readings so `vmaf-p10` spans >=2 frames; when the chunk has too few frames, revert to a lower sampling value.

**Questions to think about:** auto-set sampling=1 below a frame-count threshold (with a log line)? Adapt sampling down when frames/sampling < 20? Do not allow less then 20 min frames threshold? Purpose is to guarantee that p10 has at least 2 frames for judgement (NOT 1) - i.e. that it differs from MIN stat. And min stat could be unreliable exactly for that single-frame readings.

---

## 18. BALANCED metrics mode (soft matching) does not exist

- No "balanced" scoring anywhere (repo grep empty); scoring remains hard pass/fail with signed surplus/deficit (`_score_attempt`, `pyqenc/quality.py:498-571`).

**Questions to think about:** score = Σ |measured − target| / meaningful range, so metrics can balance each other; "targets met" as separate sign? How would it interact with CRF search monotonicity?

---

## 19. Optimization ranking is size-only; no success rate / score in summary

- Selection is size-based (`_apply_tolerance` band in search, tolerance-epsilon dominance in fixed) over `StrategyRow` populations. The unified comparison table shows per-statistic deltas vs the basis row — still no success-rate %, no score, and no exhausted-attempts vs targets-met distinction. Original motivation: AV1-FGS.

**Questions to think about:** rank by success % (then size)? Add average negative score for failures?

---

## 21. (HIGH) Quality-target presets (ultra, high / medium / low) don't exist

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
- low - should match CRF 25+ approximately (~0.07 - if we don't have other means to estimate)

With some default profile being default (similarly to codec's default preset in config) - used if user told nothing. Probably `high`

CRF above is when using H.265-anime at slow preset. One problem is how to estimate that. Pipeline runs on chunks - more granular quality assessment.
But we can use built-in profile quality range limiting for running fixed-CRF encodes on chunks to get the quality measures.

Another question is I want targets to favor retaining natural film grain, not favoring blurred results. h.265-anime is particularly tuned for this, so probably we should run 2 encodes at fixed CRFs - with plain h.265 and with h.265-anime variant at the same CRF values. And compare measured metrics to better understand the difference - where we could give more slack, where we should make things tighter (in favor of retention of original looks, but at sane quality levels).

---

## 22. (low) End-user output-name template system ({fps}, {title}, …)

- Not a naming-ownership concern — the merge-output name family has a single owner (`MergedVideo`, `2026-09-28 artifact-model` spec). This item is the end-user feature: config-defined templates for final output file names that the owner consumes.

**Questions to think about:** template syntax and config shape; which parameters to expose (title, fps, year, strategy, …)?

---

## 33. (low) Disk-space estimation — single Probe-hosted call, log-only

- A size-estimation module exists (`utils/disk_space.py`, `SpaceEstimate`, pixel/bpp heuristics). The original JobPhase-era invocation (cached `job.yaml` metadata, commented-out insufficient-space branch) is fully gone — no estimate code remains in `job.py`.
- Timeline: JobPhase (original) → ExtractionPhase (`2026-09-25 file-stream-model` Task 4, on the enumerated `VideoStreamInfo`) → ProbePhase (`2026-10-05`, cc63e55): the estimate reads `plan.strategies` and Probe owns the plan (the video chain's entry context; audio consumption is negligible next to the strategy-multiplied video work, and the audio registry carries no plan). USER: "space estimation needs a complete rework. But right now - just a fast solution."

**Questions to think about:** per-phase re-estimates later in the pipeline?
Keep log-only?

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

---

## 47. Scene detection threshold search

Default doesn't always produce wanted results.
Add a searching mechanic, similar to quality search? With something like 15 - 50 range and a starting point of default_config default?
Target is to get small chunks - not over a minute probably (it is a rare thing for movies to have such a long scene). Something like p90 < 60 seconds, p10 > 2 seconds. But not to split on every min frames (too low value).
With all params being configurable in the config, including ability to disable the search.

Why? Movies regularly get too long chunks. Long chunk encoding kills the quality knob searching.

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
- 2026-10-05 (user): "Honestly, space estimation needs a complete rework. But right now - just a fast solution" — the current form (Probe-hosted, plan-aware, log-only; see §33) is explicitly a placeholder; fold any rework of this item with §33's open questions (required vs recommended ranges, config-awareness, where in the pipeline it runs).

## 51. (HIGH) QualitySearchV3 is outright broken

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

2026-10-21 CONFIRMED LIVE (§83 e2e matrix, search + `--no-optimize`): search-mode h264 winners
at differing CRFs → mkvmerge emits "No append mapping … default mapping" warnings for EVERY
appended chunk (107×) → exit 1 → pyqenc treats any nonzero exit as fatal → merge FAILS for
the strategy ("Failed strategies: h264", phase ERROR). Fixed runs never hit it (one CRF →
identical SPS); h265 never (in-block parameter sets); searched+optimize runs normally never
reach an h264 merge (tolerance eliminates it — only `--no-optimize` exposes all strategies).
Reproduce: `D:\_encoding\tmp\nomeasure-e2e\section52-evidence\REPRODUCE.md` — the exact
pipeline command plus a self-contained 3-winner minimal repro (exit 1 + append-mapping
warnings). Related bycatch: the "mkvmerge failed (exit N)" ERROR line drops mkvmerge's
stdout warnings — the actual cause never surfaces in the log.

---

## 53. (fast) Ensure mkvextract with fallback

Should be implemented already. Task to ensure that everywhere.

We are targetting MKV source and output as the key targets. Other outputs are not supported. Other inputs - well, we should try to use them.
So the policy is - we should use mkvextract first where possible. But have a fallback in-place if that fails. Prefer try-except flow semantics (i.e. if we failed to get fps - that's a failure -> try next variant).

Other acceptable tools: ffprobe, ffmpeg. Maybe something else.

This is not applicable to actual encoding/processing - both audio and video processing we aim to do with ffmpeg only.

For the outputs - we support only mkv and we prefer mkvmerge.

Need to check code. Especially metadata extraction, files materialization (extraction phase), screenshotting.

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

---

## 64. (fast) Audio chain as flt ffmpeg audio format

Need to check if full audio chain is converted to flt or only on downmix filter. Probably always using flt is better for precision. But only downmix using custom weights should be capable of producing clipping?

---

## 65. Cleanup 2026-09-30 — deferred findings (from cleanup-report.md)

Covered by the cleanup branch and closed: old §4 (ruff gate), §5 (metrics spy tests → metrics.yaml behavior tests), §43 (stale audio tests), §58 (comment/dead-code sprawl). Audit: `cleanup-report.md`.

Deferred as costly/structural:

- encoding⇄optimization import cycle (marked `# deferred: circular import` at 3 sites) — worth breaking properly.
- Test fixture factories duplicated across files (Strategy/CodecConfig, extended-stream, encoded-chunk builders) → conftest consolidation.
- `EncodedChunk` composition duplicated (`_pair_placeholder` vs `build_encoded_chunk` shapes).
- measure→extraction private imports (`_probe_streams_json`, `_video_info`) and api→measure `_parse_duration` — re-homing per §57.
- Broader test-surface rework beyond metrics (string-format pinning etc.); skipped integration tests need a real run on a media sample.

---

## 67. The very first frame often gets very different measured quality from the rest of the video

I'm not sure what happens there. But many graphs look to start at nearly the same measured quality point at the first frame, as if it was fixed & controlled
by other means then the other video. I'm not sure if it is i-frame controls, or maybe measuring specifics, but ideally we need a way to affect that
or at least better understand it. It often affects min or max statistics, making them not very reliable.

Investigation needed.

---

## 75. Flaky: test_pts_conversion_correctness under load — needs thinking

2026-10-01: failed once mid-suite (4-file batch: probe + merge_mkvmerge + pts_preservation + metrics_integration), then passed on immediate re-run of the same batch and in isolation. Suspect timing/IO-load sensitivity or shared-state assumption. Not reproducible on demand.

2026-10-02 (later): THIRD observation — 3-at-once mid-suite failures right after a mass file rewrite (LF normalization touched ~700 files; suite ran against regenerating pycache under heavy IO). Immediate clean rerun: 722/9. Escalating pattern: flakes track system IO/CPU load, not test logic. Candidate next step: run the suite under repetition (`pytest --count`-style or a stress-loop) to catch a name.

2026-10-02: second same-shaped flake in a targeted 3-file run (quality + vif + metrics; 1 failed of 129, name not captured) that passed on two immediate re-runs AND in the full suite. Pattern so far: ~1 flake per full-suite-scale run under load — worth a session with `-p xdist`-style repetition or last-failed + load when it recurs.

2026-10-19: FOURTH observation — first one with a NAME: `tests/unit/test_yaml_utils.py::test_properties_round_trip_preserves_arbitrary_text` failed mid-suite with Hypothesis DeadlineExceeded (909ms vs the 200ms deadline) under concurrent load; passed in isolation AND the full suite passed clean on immediate rerun. Same load-tracking pattern, now with the deadline mechanism identified as the trip-wire (examples run slower than the fixed per-example budget, not logic failures).

2026-10-19 (later): FIFTH observation, same mechanism, different test: `tests/test_metrics_properties.py::test_convergence_stats_math` DeadlineExceeded mid-suite under load (during the 0.27.3 gate run); passed in isolation in 1.1s. Family now spans two files.

---

## 76. Reevaluate Python 3.15.x (lazy imports) — needs thinking

2026-10-02, after migrating to 3.14.7: 3.15 ships lazy imports (deferred module import) — should significantly cut CLI startup time; worth a dedicated evaluation+migration pass in a few months. Also worth watching in the same pass: free-threaded builds (PEP 779, officially supported since 3.14) for CPU-bound Python sections.

3.15 initial release is 2026-10-09. Adds lazy imports, changes default file encoding to UTF-8, maybe more (need to check).

Pandas, numpy, opencv - all look already adapted to 3.15 with wheels built.

---

## 77. Major dependency upgrades review — needs thinking

2026-10-02: routine `uv lock --upgrade` is deliberately confined to within-major bumps; taking a NEW major of any dependency must be a reviewed decision, not an upgrade side-effect. First candidate: OpenCV 5 (constraint `opencv-python-headless<5` in pyproject `[tool.uv]`; scenedetect transitive; major API break — lift only deliberately). Then a scan of pydantic / matplotlib / pandas / pytest / alive-progress for new majors with relevant changelogs.

See also 76, 130.

---

## 78. (HIGH) QualityPoint sentinel/measured split — refactor with the V4 spec — needs thinking

2026-10-02: QualityPoint overloads one class for two concepts — a measured attempt (metrics dict) and an untested boundary marker (metrics=None, is_sentinel, fake score=0 that also means "winner"). Split into `QualityBoundary(q)` (sentinel) and a measured `QualityPoint(q, score, metrics: dict)` with metrics REQUIRED. Kills: metrics | None, is_sentinel, the `not self.is_sentinel` clauses in is_pass/is_fail/is_winner, the score=0 overload, and the narrowing asserts. Blast radius is quality.py only (17 is_sentinel checks + 8 sentinel constructions; no direct test usage). Do it as part of the QualitySearch V4 spec so the new algorithm builds on the clean record type.

Part of effort of design through types.

See also 51.

---

## 82. Research: multi-metric quality scoring/evaluation — joint paper review for possible improvements

Born from the fixed-quality mode design discussion (2026-10-02). In fixed mode the optimization phase loses its "targets met -> compare sizes" assumption (knob pinned, strategies sit at different actual quality levels), so cross-strategy judging needs a defensible multi-metric story. Current working direction: anchor-relative deltas (size-winner strategy as reference) + Pareto dominance pruning, no composite scalar. This item collects sources for a dedicated joint research pass over the papers.

Articles:

- Multi-objective Pareto vs scalar selection (Univ. of Oviedo, PDF): https://digibuo.uniovi.es/dspace/bitstream/handle/10651/85820/1-s2.0-S0167865526002977-main.pdf
  — empirical: Pareto advantage over scalarization is real but partial and metric-specific.
- Zwei: self-play RL for perceptual video coding (IEEE TMM 2021, PDF): https://godka.github.io/tmm21-zwei.pdf
  — VMAF used as THE perceptual scalar objective; canonical example of VMAF-as-trained-fusion being the industry's composite.
- Zhang et al., "Enhancing VMAF through New Feature Integration" (arXiv, 2021) — integrates new video features / alternative metrics into VMAF; documents base-metric gaps.
- "Gain of Grain: A Film Grain Handling Toolchain for VVC" (2024, ResearchGate) — conventional metrics don't model grain perception; FGS-aware evaluation. Directly matches our av1 observation (VMAF rewards grain removal/smoothing).
- Cloudinary, "Using VMAF with other metrics" (blog) — practical multi-metric pairing (VMAF primary + PSNR/SSIM as checks).
- AWS Elemental MediaConvert — per-frame quality metrics docs (PSNR/SSIM/VMAF/QVBR interpretation thresholds; production practice).

Known context going in: VMAF is spatial-only (no temporal effects) and treats grain as distortion, so it rewards denoise/smoothing — our av1-at-same-CRF trap; VIF is the texture-retention guard (already in our measured set; all metrics normalized 0-100). Open questions for the research pass: principled scalar fusion vs per-metric constraints; grain-aware / temporal metrics worth adopting; BD-rate applicability at single operating points.

Real-data observation (2026-10-03 smoke test, `-q 18` over h264 + three h265 profiles, comparison restricted to p10+median per metric): **dominance pruning selected 4/4 strategies — zero reduction.** The three h265 variants are mutually incomparable (each wins a different mix of compared stats at near-equal sizes; the full-stat set was even more permissive), and h264 is bigger but vmaf-better, so it also survives. Pareto pruning as implemented is honest but toothless for the "pick a few for me" job — the pre-spec concern confirmed. Extra angles for the research pass beyond the papers above:

- Reduction mechanisms with an actual bite: user-supplied size/quality budget (top-K within budget), epsilon-dominance (near-equal stats count as equal), explicit trade-off sliders, or a "dominance with tolerance" per metric.
- The anchor-relative table already gives the human the trade-off picture — maybe the right v2 is interactive/filtered presentation, not stronger auto-pruning (auto-picking needs the composite opinion the spec refuses).
- Stat-subset choice interacts here: p10+median compares worst-decile + central tendency; adding min/max-style stats made pruning strictly weaker.

---

## 84. Re-home quality ranges: codec = actual codec range, profile = reasonable working band

**Status note (2026-10-05, post-config-resolution):** range VALIDATION is now one direction-free rule on `AppConfig` (`_validate_quality_range` + `_codec_ordered_range`) — the re-homed values land in exactly one check site. The value re-homing itself (default_config.yaml codec/profile bounds) remains open.

Background: profiles originally had no `quality_range` override, so the practical working band had to be defined on the codec itself — default_config.yaml comments say it outright ("full codec range is 0–51; 6–30 is the practical working band"). Now that profiles narrow ranges (and the fixed-quality CLI override layers on top), each value should live in its right layer:

- codec `quality_range` — the codec's ACTUAL domain (x264/x265/NVENC-CQ/QP: [0,51]; AV1: [0,63]; NVENC-VBR: needs thinking — the 99.5 Mbit/s cap is already a practical cap, not a factual bound; vulkan QP: real range is −1–255, but values above ~50 produce visible artefacts — even "actual" needs a sanity discussion there)
- profile `quality_range` — the reasonable working band (today's [6,30]-style values move here; bundled profiles should all carry one, otherwise profile-less searches roam the full codec range)

Follow-ons to consider while at it: `default_quality` must stay inside the moved bands (the fixed-quality spec's starting-point auto-adjust already covers exclusion); `quality_log_padding` derives from range width (cosmetic); search convergence from wider codec bounds (phase-0 half-range steps get coarser — probably fine, verify); the fixed-quality `-q` override is bounded by the codec range, so it gains the wider freedom too (consistent — CLI replaces the profile band).

---

---

## 89. Check the code for `str` usages

**Status note (2026-10-05, post-config-resolution):** the config layer is swept — raw strings survive only as the YAML parse form; resolved state is objects (`EncodingPlan` on `ProbePhaseResult`; doctrine in coding-standards.md). The sweep remains open for the rest of the codebase.

Old code used `str` directly in many places. Instead of Paths, instead of strategies, profiles, etc. We've moved to objects & classes since then. Single instanciacion where possible. strings instead of objects could be used for serialization/deserialization, but only to directly recover to objects. Legitimate usages for `str` do exist, like messages. But if it masks object usage - this must not happen.

See also 104.

---

## 91. LongPath idempotence breaks for trailing-whitespace path strings — needs thinking

Found live 2026-10-05 by the Hypothesis property suite (example: `'0/ '`): `ntpath.abspath` (GetFullPathName) strips whitespace preceding a separator or end-of-string, so `LongPath('0/ ').__fspath__()` → `...\0\` while a second round-trip yields `...\0` — not idempotent. Pre-existing on main (reproduced on untouched code; the property run had simply not drawn this example before). Such paths are unrepresentable on NTFS via Win32 APIs; the property generator now excludes the domain (`\s[/\\]|\s$` filter) with a pointer here. Open question: is a LongPath-side normalization of such strings wanted at all, or is the generator exclusion the complete answer?

---

## 92. Long functions lose semantic clarity — continued cleanup: inventory + assess logic separation — needs thinking

Raised 2026-10-05 (config-resolution review): many functions are too long — for a human developer a long body loses the answer to "what exactly does this function do". Seed example: `_scan_winner_sidecars` (pyqenc/phases/encoding.py) — a flat loop that both LOADS one winner's facts (name-pattern match, sidecar read, frame count, worst-target evaluation) and SUMMARIZES into the running aggregates (limiter tallies, frames, frames_known). Natural split: a "load one" function with a clear per-item footprint (winner → sidecar facts or skip-reason), a summarize step over its result, and the aggregate owner calling them in a cycle — each function then states its own contract.

Effort shape (continuation of the 2026-09-30 cleanup lineage):

- Build an inventory first: long functions across `pyqenc/` (simple AST/line count scan; `scc` sizing per cleanup conventions) — do not eyeball.
- For each candidate: assess whether a CLEAN logic separation exists (per-item loaders vs aggregators, stages of a pipeline, decision vs mechanics). Candidates, not mandates — some long functions are honestly one thing and splitting them would only scatter their story.
- Rule of thumb to validate: the split is right when each part's footprint (inputs → outputs) can be stated in one sentence.

---

## 94. Config layer merge semantics — explicit intent markers + empty-container asymmetry — needs thinking

Raised 2026-10-05 (user, reviewing `_deep_merge`): layers can express "merge" (dicts, the default) and silent "keep" (null values), but never "drop previous and use mine wholesale"; lists are unconditional replacement with no way to ask for append.

Semantics inventory (verified in code):

- scalar → replace (the only option).
- dict + dict → recursive merge; a null section OR an empty `{}` silently keeps the base — base keys can never be REMOVED by a higher layer.
- list → replace; `[]` replaces with empty (failing later as e.g. "No strategies configured") while `{}` keeps base — an ASYMMETRY between empty containers, surprising in both directions, worth documenting or unifying regardless of the bigger question.

Assessment (updated 2026-10-05 after user review of the first take):

- Custom YAML tags (`!replace`/`!append`) REJECTED (user): end users must memorize exact nonstandard tags; a forgotten tag fails in the loader with a cryptic error before any of our messages can help; no discovery path.
- LISTS — accepted direction: the empty-first-item convention ("drop pre-existing, use only what follows"), familiar from other tools. The marker is a single list's FIRST ITEM being null — `strategies: [~, h265+slow]` (flow `~` = null; one property, one list — a duplicate `strategies:` key "first null, then list" is invalid YAML, hence the in-list marker). Prefer null over `""` — no list in the schema accepts nulls today, so collision-free by construction. Mechanics: consumed in `_deep_merge` at the boundary (strip + replace mode for that key), pydantic never sees it; failure mode is soft (a mistyped marker lands in OUR validation, whose message can teach the convention). Constraints: rule is global for all future lists (a nullable-element list would need to opt out); a marker in the BASE layer is just data (only meaningful in a higher layer). Live use case: owning `strategies`/`targets` wholesale from `pyqenc.yaml` without editing base.
- List MERGE stays never-implicit — for every current list replacement is the honest intent; append needs arbitrary order/dedup answers and must be opt-in per instance if it exists at all.
- DICTS — open, lean "no marker": (a) null-deletion per key (k8s merge-patch precedent) COLLIDES with our null-means-keep rule (`profiles:` null section = keep today, would mean delete-all → validation failure); a scoped carve-out ("deletes only at nested map level") is baroque and unteachable. (b) null-key first entry — mechanically possible (PyYAML preserves document order) but zero precedent anywhere. (c) reserved marker key — safe for schema-fixed dicts but the user-keyed maps (`codecs`, `profiles`, `filters`) are exactly where wholesale-replace would be wanted and exactly where reserved names can collide. Escape hatch weakening the whole dict case: the `pyqenc config` workflow copies the active base to home/cwd — owning the full document removes the need for layer-deletion; only "stay lean over bundled defaults" remains unserved, and that is cosmetic (unused entries are inert).
- Demand today is thin overall: the gap may still be more documentation than mechanism — the merge rules currently live only inside `_deep_merge`'s docstring; the config docs should state them (including the `{}`/`[]` asymmetry decision) whatever gets built.

---

## 98. API execution surface — small intent-based surface vs phase-mirroring wrappers

api.py exposes seven public functions: six near-identical `_drive` wrappers mirroring phases (`run_pipeline`, `extract_streams`, `chunk_video`, `process_audio`, `encode_chunks`, `merge_final` — the first and last are the same shape, both targeting Merge) plus the standalone `measure_quality`. `pyqenc/__init__.py` re-exports all seven as THE package surface (`__all__`). Consumers today: `cli.py` + three test files (a handful of call sites); zero external consumers — the surface is free to change.

With the CLI going intent-based, the phase-mirroring API becomes the odd one out. Candidate shape: intent-named entries mirroring the commands — auto / video / audio / extract (+ `measure_quality` unchanged) — or a single `run(intent=…)`. Considerations:

- Phase-level access for tests needing partial runs: through the intent set (the intents ARE the sanctioned partials now) or directly via `Runner`/`_build_registry` (the real low-level surface).
- Whether it lands inside the cli-intent-commands window (same files: api.py, `__init__.py`, cli re-pointing, the three test files) or as a follow-up.
- `plan` stays an explicit api argument, presumably (resolution is the CLI's job per config-resolution) — but video/audio/extract intents differ in needing it at all.
- `extract`'s materialize flag: public api parameter or CLI-only detail?

---

## 102. Evolutionary / anytime encoding — deficit-driven scheduler (idea)

**Status:** idea (discussed 2026-10-05; NOT for right now — needs a full scheduler rework, or an alternative scheduler. Deliberately separate from §51: QualitySearchV4 stays the per-chunk next-attempt search, untouched by this)

Today the encoding phase finishes chunks one at a time — each chunk runs its whole CRF search before the pipeline moves on. The idea inverts the order: encode ALL chunks once at an initial quality (wave 1 → a complete, valid video immediately), measure, then iteratively re-encode the WORST chunks toward their targets — always improving the largest deficit — until convergence. The user can stop anytime and materialize as-is.

Why it looks good:

- Anytime-readiness: a complete video exists after one encode per chunk; every later pass only improves it. "Enough, stop there, materialize as is" becomes a product knob.
- Deficit-driven priority IS quality-floor equalization — the same semantics our p10/min targets express (water-filling / largest-deficit-first).
- Attempt savings via temporal autocorrelation: neighbors inherit initial CRF guesses from already-encoded neighbors (the "smoothing to adjacents" effect — implemented as warm-starting, not averaging measured values); a single cheap global probe could seed wave-1 initial values the same way.
- Asynchronous shape, no wave barriers: a priority pool of (chunk, next-guess) jobs pulled continuously by the concurrency workers, prioritized by deficit.

What it costs / needs first:

- The scheduler rework itself: per-chunk search (§51) unchanged — this changes attempt ORDER, parallelism shape, and the global stop condition.
- Merge-on-current-best: "materialize as is" requires merging every chunk's current winner (wave 1 guarantees one attempt per chunk — no gaps); re-merging as winners change composes with the §86/§100/§101 invalidation work.
- Total encodes do NOT shrink by themselves (a wave = one attempt for that chunk, same count as depth-first) — the count win comes from warm-starts   and probe seeding, not from the wave structure.

Research vocabulary: anytime algorithms (Zilberstein), water-filling / largest-deficit-first scheduling, racing algorithms (successive elimination); Netflix Dynamic Optimizer is the batch-mode cousin (full grid, no anytime property — the interactive stop is the novel bit).

Research links (from the 2026-10-05 discussion):

- Netflix Tech Blog, "Dynamic Optimizer — A Perceptual Video Encoding System" (2018) — the per-shot parameter-grid system; boundaries still from shot detection: https://netflixtechblog.com
- Streaming Media, "The Past, Present, and Future of Per-Title Encoding" (2021) — probe-encode→complexity→parameters lineage: https://www.streamingmedia.com/Articles/Editorial/Featured-Articles/The-Past-Present-and-Future-of-Per-Title-Encoding-152221.aspx
- "Machine-Learning-Based Method for Content-Adaptive Encoding" (IEEE, 2022) — per-segment parameter prediction from features: https://videoprocessing.github.io
- VCA — Video Complexity Analyzer (per-frame SI/TI-style complexity without encoding; aimed at shot detection + per-title encoding): https://vca.itec.aau.at - EVCA — Extended Video Complexity Analyzer (ACM MMSys 2024) — complexity features for rate control and per-segment parameter selection: https://dl.acm.org/doi/10.1145/3625468.3652171
- IVCA — Inter-relation-aware Video Complexity Analyzer: https://arxiv.org/html/2407.00280v1
- "Video Encoding Complexity Characterization" (IEEE Access) — SI/TI vs encoding complexity; scene complexity ≠ encoding complexity: https://ieeexplore.ieee.org/document/9900917

Adjacent but separate (same discussion, NOT folded into this item): intra-chunk quality refinement via x264/x265 `--zones` (per-frame-range `q=`/`b=`; x265 `crf=` zones silently don't work; SVT-AV1 has none — our segment+concat architecture IS its standard workaround) — a per-chunk post-convergence refinement idea, also not now. Zone mechanics docs: x264 manpage (https://manpages.debian.org — frame-based `<start>,<end>,q=|b=`); x265 docs (https://x265.readthedocs.io — `q=`/`b=` only).

---

## 104. too many `str` usages as attributes with convention

We need to check class fields, which still use `str`.

Bad examples:

- fps, fps_fraction - looks like candidates for single class with 2 representations
- resolution - actually a width and height and a name (display_name / safe_name?). Probably also with support for Crop operations or resizes.

Good examples:

- include/exclude patterns - regex is text. Could probably also switch to compiled regex to make it obvious, but in general this is okeish.

See also 89.

---



## 114. e2e intent tests hang: ffmpeg 9.0.2 spins encoding the sample's chunk-3 region

`tests/e2e/test_intent_commands.py` never completes: the encode of the
sample's third chunk region hangs. Reproduced STANDALONE — the exact
ffmpeg command alone, no Python involved:

```
ffmpeg -ss 14.181 -t 13.847 -i samples\sample-e2e.mkv -map 0:0 \
  -c:v libx265 -preset ultrafast -crf 18.0 -pix_fmt yuv420p10le \
  -f matroska <out>
```

ffmpeg 9.0.2 (`9.0.2-full_build-www.gyan.dev`) burns CPU indefinitely
with a 0-byte output on this region; the first two chunk regions of
the same file encode in seconds. Points at the ffmpeg build (or its
interaction with that region's HEVC 10-bit content) — not at pyqenc:
the same command shape works elsewhere, and the real-source e2e
(test.mkv) was unaffected the same day. Candidate: pin/switch the
scoop ffmpeg version or regenerate the sample; re-verify the intent
e2e suite after. Also blocks any full-suite gate run that includes
`tests/e2e/`.

Related (2026-10-09 session): e2e tests should run rather fast, on a
premeditated source (per the standing speed rule) — not on whatever
sample happens to be around.

---

## 115. Full test-suite assessment against the QA & testing rules

Sweep the entire `tests/` tree against the steering QA rules and produce
a findings report (then a fix plan, gated on user review):

- **Pinning/characterization tests** — anything asserting file cosmetics
  (yaml text, field order, key sets/presence), internal stash state, or
  serialized forms where a round-trip/tolerance test is the honest
  contract. Drop or convert to behavior tests.
- **Underscore-surface violations** — tests importing or patching
  `_private` helpers (functions or methods); the rules allow only the
  process boundary (ffmpeg/mkvmerge runners, external shells) as the
  mock seam. Includes `patch.object(..., "_method", ...)`.
- **Property coverage gaps** — features with invariants but no
  Hypothesis `@given()` tests (happy-path/error-handling are the PyTest
  layers; properties are the search layer).
- **Coverage-type gaps** — features/modifications missing the mandatory
  happy-path / properties / error-handling structure.
- **Dead tests** — tests that pass no matter what (the failure-assertion
  check) and tests guarding bugs that no longer exist.

Note: the suite is large; the sweep is an audit, not a rewrite — group
findings by file with evidence, and let the user rule on each group
before mass changes. The §114 ffmpeg hang blocks `tests/e2e/` from
running at all until the ffmpeg version question is settled.

---

## 116. Metric+stat value class — retarget every stringly `"{metric}_{stat}"` / `"{metric}-{stat}"` site

**Status update 2026-10-12:** the minimal identity type landed with the unified-quality-summaries spec (`MetricStatKey` in `pyqenc/quality.py`: closed sets, canonical `vmaf_median` sidecar form, `%`-form display, tolerant parse, pydantic dict-key alias; `QualityTarget.key()`). The remainder below is the full migration this item still owns.

**Status:** needs thinking (user direction 2026-10-10; explicit-typing rule)

Measured metrics flow through the system as `dict[str, float]` keyed by convention strings, flattened/selected/compared at every layer — and every site re-derives the key by hand: `f"{t.metric}_{t.statistic}"` in the attempt pick-up, the targeted-subset filter, `_score_attempt`, `failed_targets`, the winner-limiter tallies, optimization's synthetic ruler, merge's rendered-keys/presentation bar. Two vocabularies exist for the same identity: underscore (`vmaf_min` — sidecar keys, metric dicts) and dash (`vmaf-min` — CLI/config/`QualityTarget.parse` targets). Nothing type-checks that a key is well-formed; nothing owns the pair.

Related uniformity gap (user, 2026-10-10): **the full measured set vs the targeted subset** — deriving one from the other is hand-rolled at several sites (`_winner_targeted_metrics`' mode-aware subset for winner sidecars, the pick-up's `targets_set_reused` filter, merge's `_summary_rendered_keys`, the search's target-key projection). ONE uniform method should own full→targeted (mode-aware: search = the targets' keys; fixed = the comparison stat set), expressed over the key class — every consumer asks it, nobody re-implements the filter.

Direction: a value class for the metric+stat identity —

- frozen, **hashable + comparable** → usable directly as the dict key of measured-metrics maps (replacing `dict[str, float]` with `dict[MetricStat, float]` or an annotated alias at the boundary);
- **strict canonical serialization** (its yaml/storage form) and **tolerant parsing** (both vocabularies + display), the `CropYaml` pattern applied to a key type — pydantic dict keys need the annotated boundary (canonical string on disk, class in memory);
- `QualityTarget` re-targeted onto it (fields `metric: str` + `statistic: str` fold into the class) and gains its own strict serialization / tolerant parse (`"vmaf-min:95"` config vocabulary included);
- **parsing included** (user, 2026-10-10): each metric becomes its own class (PSNR / SSIM / VIF / VMAF — MetricInfo-shaped) that KNOWS how to parse its own logs (`.log` stats files, VMAF JSON), emitting explicitly typed results — replacing the generic file-type-keyed parser in the visualization/metrics plumbing. The metric identity itself is an enum-like STATIC thing (one instance, never re-instantiated per parsed line — today `MetricType(...).info` constructs on access), and the static identity is what the new metric-stat keying, the old `MetricInfo` and `QualityTarget` all attach to via explicit typing;
- retarget sites: quality.py (`MetricType`/`flatten_metric_stats`/ search steering), encoding pick-up + winner targeting, optimization (synthetic targets, summaries), merge (rendered keys, replay bar), measure/visualization stats plumbing.

Open design questions: naming (`MetricKey` is TAKEN — the perf-metrics timing enum in `pyqenc/metrics.py`); whether the stat half becomes its own typed set (today a free-form string — `min`/`p10`/`median`…); whether per-metric classes carry the MetricInfo descriptor data (normalize/deficit/comparison_range/acceptance_delta) or compose with it; interplay with §111's summary-table rendering (the dash vocabulary is its display form); sequencing vs the QualitySearch V4 spec (§78 rides that same refactor wave — do the record type and the key type together?).

`FIXED_COMPARISON_STATS` should also become explicit targets?

and need a distinction between full metrics set and targeted metrics set at typing level, so that we don't have to guess.


Extra from human:

- Metric vs Stat vs Metric-Stat pair vs Target (metric-stat-value) vs Targets (a set of Target)
- Each owns its own mechanics, explicitly, (disowned functions are strongly discouraged). Naming. Targeted-set selection from full measured metrics, etc.
- Anchor targets also belong here (targets)
- Deltas / basic Scoring should be also based on those classes.
- Ideally the last place where we see plain values - are the measurements logs or user inputs (both are inputs). Since then full path should be class-based.
- Single instanciation where possible (i.e. user input targets are instanciated once and passed as that object ever since).

---

## 117. Observability research — traces/metrics for a CLI-first tool; replace the app-metrics stub

The current app-metrics (`metrics.yaml`, `MetricsCollector`) is a temporary stub, and it shows. Industry standard observability has three pillars — **traces, metrics, logs**. We have logs. Our metrics suck. We have no traces. And nothing is collectible externally (no Prometheus/OpenTelemetry export).

Constraint: pyqenc stays **CLI-first** (potential web UI later). Full OTel/Prometheus adoption is justified only if it ever becomes a service; while CLI, it must still be observable — with a local dump like today's `metrics.yaml`.

What the observability must answer (granular, of course):

- **how long** things work (durations), and
- **how many times** (convergence counts),
- per action (phase: audio/encoding/merge/probe…), per strategy/chain, per subaction (encode, recovery, optimize…),
- with a clear distinction between **actual wall time** (top-level metrics) and **sub-time** (parallelized flows — parts of the full time, never summable against it),
- all safely dumpable, **crash-resistant with minor allowed delta loss** (see the current `metrics.yaml` implementation).

Task: research best industry practices for standalone CLI tooling — hierarchical span/task timing (inclusive vs exclusive time is exactly the wall-vs-sub-time problem), OTel SDK in offline/local-file-export modes, what a trace-shaped local dump would look like vs the current flat dotted-key metrics — and their applicability to a local `metrics.yaml` dump. §59 (concurrent dumps) rides under this umbrella and should be re-evaluated against whatever shape lands. (Former riders §2 and §12 were consumed by the 2026-10-18 concurrency spec; the wall-vs-sub-time invariant is now stated on `AppMetrics`.)

---

## 119. Table formatting is hand-aligned everywhere — unify the mechanism (mini-research)

**Status update 2026-10-12 (decision recorded):** one INTERNAL formatter (`format_table`, `pyqenc/utils/tables.py`) won the research — rich/tabulate/wcwidth rejected (the aesthetic is ours; compound cells are pre-formatted strings regardless). The comparison tables render through it. The remainder below (re-homing the winning-limiter, measure, and key-value tables) is still open — that is §119's leftover scope.

**Status:** needs thinking (user direction 2026-10-10)

The remaining hand-aligned tables (the winning-limiter table, the measure summary, `fmt_key_value_table` call sites) each hand-roll column alignment — `%-30s %12s %8s`-style format strings, manual padding constants, per-site width tuning. The LOOK is right (neat, aligned, terminal-friendly); the repetition is the problem.

Research the options:

- **External**: `rich.table` / `tabulate` / `wcwidth`-based helpers — do they render the same aesthetic (plain text, aligned columns, no color dependency in logs), and what do they pull in as dependencies (CLI-first, minimal-deps preference)?
- **Internal**: one common formatter (e.g. a `format_table(headers, rows, aligns)` util) with the current visual conventions encoded once — probably the default answer given the aesthetic is already ours; the research should confirm an external lib buys nothing we don't have.

Constraint: the output must stay byte-identical-ish to today's tables (the user likes them); logs go through the plain logger (no ANSI color assumptions in `metrics.yaml`-adjacent output).

---

## 120. PhaseResult redesign — base artifacts field, frozen results, naming pass

Already implemented??

`PhaseResult.artifacts` is a derived property — dataclass introspection re-concatenating the subclass's declared `Artifact` fields on EVERY access (no cache), sanctioned as "the one dynamic access". Re-investigate the design:

- **At minimum**: cache the construction (results are never modified in flight — phases build them right before exiting), or back the property with a base private field.
- **Better — invert the storage**: a base `artifacts` field on ALL phase results (it is already baked into `.run`'s decision and the external contract). Explicit typing stays, but reversed: `__init__` accepts the base artifacts list and SPLITS it into the typed fields/views per phase (extraction: video/audio/subtitle rows; chunking: chunks; etc.). Typed views can be static properties over the frozen base.
- **Frozen**: consumers should not modify results; phases construct at exit. A frozen dataclass base + statically-constructed typed views; the generic `artifacts` (or `_artifacts`) lives on the base for `.run`/the runner.
- **Naming pass**: streamlining across all `*PhaseResult` classes (`chunks:` vs `outputs:` vs `winners:` vs `merged:` vs `file:` — one convention, named per-phase but shaped uniformly).

Open questions: does the "derived, never storage" doctrine (internal ledger rows can't leak into results) survive the inversion — the split must reject unwanted rows at construction; do partial-failure paths (FAILED with some rows) fit the frozen-at-exit model; interplay with `wanted`-derivation in the template.

---

## 121. Docstring audit — bring every docstring to the universal Google-style format

Rule: ALL docstrings Google-style — one-line summary, prose, then structured `Args:`/`Returns:`/`Raises:`/`Yields:`/`Attributes:` sections (the LSP parses these for hover/parameter help). The codebase is mostly there already (the house style evolved toward it), but the sweep must find and fix:

- Missing sections (functions with args but no `Args:`; classes with fields but no `Attributes:`).
- reST-style fields (`:param x:`, `:rtype:`) or NumPy-style underlined headers that predate the convention.
- **De-archaeologize**: dated reasoning ("dropped 2026-10-09"), discovery stories ("found live when…"), and tombstones of deliberately-absent fields ("X is deliberately NOT a field…") — contract only in the docstring; the history is in commits/specs. The 2026-10-10/11 sidecar-diet cycles wrote a lot of this in.
- **De-duplicate**: field inline trailing comments repeating an `Attributes:` entry — one home; the docstring section wins when the class has a docstring.
- Stale sections (parameters renamed/removed but the docstring still lists them) — the LSP surfaces mismatches; fix as found.
- Docstrings that narrate mechanics instead of contract (summary + why/constraints, not line-by-line retelling).

Mechanics: per-file pass, no behavior changes; Pylance/Pyright hover on each symbol is the verification tool.

---

---

## 122. Names audit — public/private surface + project vocabulary

Two dimensions:

- **Public/private surface.** Module top-levels are the main concern — they form each module's external contract, and the project wants a MINIMAL contract (coding-standards: "only truly necessary functions should be public"). Many top-levels look internal/implementation-specific but carry no underscore (e.g. `LimiterSummaryRow` — a persisted-form row of one phase's summary table; who outside the module would ever import it?). Per-class members are a lesser concern — same treatment, lower priority.
- **Vocabulary.** The project has an established vocabulary (attempt/winner, probe facet, fingerprint, sidecar, measure vs evaluate→measure, presence/listing, encoding workspace vs encoded dir, …) — names should USE it. Drifted synonyms and generic names (record, item, result, info where a domain word exists) are the smell.

Process (explicit): produce a TABLE first — current name, proposed name (public→`_private` or rename or unchanged), reasoning — one row per symbol, grouped by module; the user audits and approves; only then the implementation sweep. No drive-by renames.

---

## 126. Per-selector fixed quality — multiple `--strategies`/`-q` pairs

Fixed runs with a single `-q` are an awesome tool, but quality is NOT uniform between codecs — AV1 CRF and H.265 CRF differ wildly in both value and perceptible quality for the same number. Wanting different codecs at different fixed qualities in ONE run is impossible today:

- one `--strategies` value (plain `store` — a second occurrence silently overwrites the first, `cli.py:281-293`) + one `-q` (`cli.py:294-308`), both wired into the single `resolve_encoding` call (`cli.py:406-421`);
- the shared `-q` is applied to EVERY matched strategy (`_expand_strategy_pattern(pattern, canonical_quality)`, `app_config.py:627-630`). The uniform-labels guard (`app_config.py:641-652`) only catches different label TEXT (CRF vs CQ vs Mbit/s; the label defaults to "CRF", `models.py:828`), so `av1,h265 -q 18` passes but silently shares the NUMBER across differently-scaled codecs. Escape hatches today: one run per codec family, or editing config — both ugly.

Proposal: multiple `--strategies` selectors, each optionally followed by its own `-q`, quality sticking to the last `--strategies` before it — `--strategies av1 -q 14 --strategies h26* -q 18` expands to the av1 profile(s) with quality override (14, 14) and the h26* profiles with (18, 18), each keeping default presets. Argparse side looks feasible via one custom `argparse.Action` shared by both flags, appending `{strategy, quality}` pairs in order (sketched by the user; `-q` before any `--strategies` raises `ArgumentError`); needs a bit of support from our side, including scoping the single-label requirement to WITHIN one pair (labels may differ between pairs).

Blast radius (verified):

- CLI parse layer: the custom Action pairing; `_parse_strategies` / `_parse_quality_override` stay per-value; `resolve_plan_args` re-shapes its wiring. Rules needed: `-q` twice on one selector (error vs last-wins); a selector with NO `-q` among pinned ones (the all-fixed-or-all-searched guard fires — wanted?); comma-separated patterns inside one selector receive the selector's `-q` as a group.
- `AppConfig.resolve_encoding` (`app_config.py:576-672`): `quality` becomes per-pattern (parallel sequence, or folded into the strategies argument); the uniform-labels guard re-scopes from the whole matched set to one selector's pair (one `-q` value must still mean one thing for everything it pins).
- Dedup by `(preset, profile)` first-occurrence (`app_config.py:632-639`): the same strategy matched by two selectors with different `-q` is ambiguous — explicit error needed.
- Mixed fixed/searched guard (`app_config.py:654-670`) unchanged and still binding: either every selector pins a point or none does.
- Downstream looks mostly untouched: per-strategy effective ranges already exist (`_effective_codec`), `EncodingPlan.fixed_quality` stays a plan-wide "all collapsed" bool (`models.py:685-729`), and merge's fixed provenance quality is already per-output from each strategy's codec (`merge.py:135-144, 264-273, 302-306`). Verify the optimization sidecar/replay re-derivation paths read per-strategy quality, not the CLI value.

**Questions to think about:** is the need real enough to justify the CLI surface (alternate: per-profile fixed quality in config — the profile layer already owns ranges)? Error messages must teach the pairing rule (quality binds to the preceding selector).

---

## 130. Review dependencies

There are quite heavy dependencies, a total of almost 250 MB! Need a review, if we really need them or if they can be replaced by a more lightweight alternatives:

  112.0 MiB ████      ▏/cv2
   41.0 MiB █▌        ▏/pandas
   26.4 MiB ▉         ▏/matplotlib
   22.0 MiB ▊         ▏/numpy
   20.2 MiB ▋         ▏/numpy.libs
   14.6 MiB ▌         ▏/PIL
   11.3 MiB ▍         ▏/fontTools

See also: python 3.15, major dependencies bump.

---

## 132. Merge swallows measurement failures — dataless summaries stay green

**Status:** needs thinking (reachability census 2026-10-17, surfaced by
the ruler-less table discussion)

`MergePhase` catches `(OSError, ValueError)` around `_measure_quality` (merge.py, the "Could not measure quality" warning) and keeps the merged output with `metrics_dict = {}`. If every strategy's measurement fails this way, all summary rows persist empty, `_select_anchor` returns None, and the ruler-less comparison table renders in a green run — plus the empty summaries are baked into `merge.yaml` for replays. Asymmetric with encoding/optimization, where the same measurer's exceptions fail the phase before any table (and `_finish_measurement` also builds the plot, so even a matplotlib failure after a successful parse erases the metrics at merge while crashing encoding). Direction: fail loudly, matching encoding's semantics — recovery reuses the complete merged outputs and re-measures only, so the rerun is cheap.

---

## 133. Downscaling of videos for encoding

User scenario: got 4k source, encoding produces a 50 GB output - too huge for user's archiving policy. User wants to keep archiving size under 15 GB.

Possible resolution: allow downscaling of the videos.

Scope: encoding and in-run measurements. No upscaling (nowhere to get extra info from, so no reason to increase size as part of pyqenc).

Extra considerations:

- downscaling is applied after crop (on top of it)
- aspect ratio must be preserved (and here's a question of stored vs presented resolution)
- a single scale (`--width <value>`?) or a box (`--downscale <width>[,<height>]` or `--scale`?) to fit in?
- separate from `measure` intent - where we allow scaling either way (up or down) - but only to have the same measuring scale.
- downscaling algorithm used? probably in config.
- noop if either not specified by the user, or the same as the source.
- nullable is probably ok (and omitted on none from sidecars - default behvaior already)
- a change invalidates all investment (winners, attempts, merged too) - both for algorithm used and for the scale change
- resolution granularity for the codec (different codecs have different requirements; some older ones needed like 16 granularity; newer ones are often 2 or 4 granularity).
  we define resulting colorspace in the codec, so probably resolution granularity should also be part of codec

---

## 134. Names and constants

CHUNK_NAME_PATTERN is still in use. Target - both name derivation and parsing to be owned by the relevant class.

```
CROP_SOURCE_MANUAL = "manual"
CROP_SOURCE_DETECTED = "detected"
```

looks like a candidate for either enum, or specific site ownership.


```
# Directory names for phase output
EXTRACTED_DIR          = "extracted"
ENCODING_WORKSPACE_DIR = "encoding"
ENCODED_OUTPUT_DIR     = "encoded"
AUDIO_OUTPUT_DIR       = "audio"
MERGED_OUTPUT_DIR      = "merged"
MEASURE_DIR            = "measure"
```

Candidates for phase classes ownership?


```
DEFAULT_SCREENSHOT_COUNT  = 20
```

single source of truth should be the default config?


```
TIMESTAMPS_FILENAME = "timestamps.txt"
"""Filename for the per-frame PTS timestamp index produced by ExtractionPhase."""
CHAPTERS_FILENAME = "chapters.xml"
"""Filename for the extracted chapter edition produced by ExtractionPhase."""
```

Extraction phase ownership?

```
CHAIN_FILENAME_SUFFIX = " chain="
```

audio phase ownership?

```
FFMPEG_ARG_INPUT    = "-i"
"""ffmpeg input-file flag."""
FFMPEG_ARG_MAP      = "-map"
"""ffmpeg stream-map flag."""
FFMPEG_ARG_SEEK     = "-ss"
...
```

FFMPEG runner class ownership? Also, doesn't look like worth the constants, just a command building, no reuse. Not a magic constant either - just syntax of the ffmpeg.
much more concise when cleanly built right inline.

```
FFMPEG_MUXER_
```

those are more interesting. It is part of pyqenc setup, not just syntax. Which muxers exactly we want to use. Need to think better. Probably, worth a dict const, to be used in ffmpeg runner.
a dict of type -> muxer, like image/png -> image2? need to check usage sites. Similarly with extensions.

```
SELECTOR_KEY_
```

need to check usage. not sure if we need those at all. Initial intention for selector was plain regex over a well-formatted stream's display name, so `lang=(rus|eng)` is not a
language selector, rather a plain regex matcher.

!!! Need to review constants and re-judge. Constants.py is a place, where we store things that we expect to tinker with, or where they are used for multiple modules
If constants are used for a single module - they should belong there. If they are not tunables (like, ffmpeg args - they are the external tool syntax, not pyqenc tunables)
- they shouldn't be constants probably.

---

## 138. (fast) Screenshotting uses legacy positioning

It looks like screenshotting uses legacy frame_num based positioning, and an approximate at that.

We've changed all project to use timestamp-based positioning (including for chunking).

Need to check if timestamp-based positioning is suitable for taking screenshots at exact locations, assuming the video metadata isn't distorted (including PTS and framerate).

---

## 135. Measure sidecars are outdated

Measure cli intent uses sidecars with wrong fields namings and excessive, unnecessary fields. Need to compare measure sidecars to other phase sidecars and artifact sidecars.

Also measure.py in general looks like using outdate names, before glossary, like `_run_metrics` feels completely wrong. We agreed on `measure` wording.

`measure` intent should really become a phase and reuse the same mechanics, like other phases - including job providing config. Not part of auto, a separate branch, but still
reusing the same mechanics all the other code uses.

Need an assesment on feasibility.

---

## 136. Encoding needs rework

We've introduced explicit contracts and non-nullability in most places.
We've introduced explicit phase dependencies
We've introduced unified phase.run runner.

All of those give explicit code contract.

But encoding.py ignores most of that. Doing nested ifs, using optional args it then fights.

Like:

```
    phase_recovery:   _PhaseRecovery | None                                  = None,
```

Recovery is a mandatory step. Not sure if in this format, but is a mandatory step. We don't need to fight for it, to re-check if its present.

Like:

```
_assert_one_winner_per_chunk
```

we must guarantee we do have a winner per chunk, but is it the proper way? Don't we already guarantee by construction? don't we already have chunks from chunking phase and winners from encoding phase?
don't they use the same ids (start/duration)? so why such a piece of ugly code?

also, encoding feels like wrapping the wrappers in wrappers, instead of directly calling what needs to be done. Probably a legacy of old implementation.

And I have questions to basically every step - winners recollection, stats recalcs, winners producing execution.

To put it simply - encoding.py needs a full review for simplification.

---

## 137. Logging strategy format ownership

```
                        logger.info(
                            "%s%s%s (%d of %d): starting merge — %d chunks",
                            BRACKET_LEFT, strategy_name, BRACKET_RIGHT,
                            pending_idx, pending_count, chunk_count,
                        )
```

Strategy display name, being logged in brackets - should be owned somewhere uniformely for all similar logging (probably already have a func in logging).

## 139. Round-trip property trips filter_too_much on low-CPU machines

2026-10-19, surfaced while gating the §79 refactor (unrelated to it — reproduced
identically on unmodified code): `tests/test_app_config_properties.py::TestAppConfigRoundTrip::test_scalar_fields_survive_round_trip`
fails deterministically under seed 21356150877959716566173950146632100797 on
this 4-CPU machine. The strategy draws `concurrency` uniform in 1..16 and the
test `assume`s `concurrency <= os.cpu_count()` — a 75% discard rate that trips
Hypothesis's `HealthCheck.filter_too_much`. Generation-side fix candidate: draw
`concurrency` from `1..cpu_count` directly (no assume), or suppress the health
check for this test. Same statistics-based health-check family as §75's
DeadlineExceeded flakes, but deterministic per-seed rather than load-dependent.

---

## 140. Empirical test of h265 medium preset vs slow preset

There are many tests stating:
- slow vs slower - negligible quality difference (~1%) with significant complexity increase
- slow vs medium - here we have different reports, many claim that medium gives practically the same result, but much faster. My personal experience - slow matters,
  especially for grainy content (in particular our h265-crisp and VIF metric).

Suggestion: test medium, slow and maybe slower on a few not too long videos.
Measure time it takes, resulting sizes, quality. Run in both fixed and search mode. I.e. `3*2=6` tests. For fixed quality use 17 (my nearly visually lossless).

---

## 141. No CLI way to discover available strategies (profiles/presets)

The user must name strategies as `profile[+preset]` patterns (`--strategies`, `cli.py:299-304`; format and wildcard rules documented only inside `default_config.yaml:28-31`), yet the CLI offers no way to SEE what can be named — available profiles, per-codec presets, default presets, or what the default `strategies:` selection (`default_config.yaml:30-31`) actually expands to. The only in-CLI listing is error-driven: a wrong profile name raises "Available profiles: …" (`app_config.py:764-774` — names only, no presets, no defaults). Otherwise the user has to open the config yaml — not user-friendly.

**Questions to think about:** a listing subcommand — `pyqenc config show`? (`config` today is only the copy workflow, `cli.py:675-715`.) What to show per profile: codec, presets, default preset, plus the resolved default strategy selection? Teach the pattern syntax (wildcards `h265*`, `*+*`) there too?

---

## 142. Flag-transition adaptation semantics per phase — winner-wipe on unmeasured→measured fixed rerun, and a change-adaptation matrix doc

Observed live 2026-10-21 (§83 rework e2e, `D:\_encoding\tmp\nomeasure-e2e\fixed-both-heal.log`):
rerunning an UNMEASURED fixed work dir with measurement on triggers the optimization
phase's winner-wipe — sidecar-less winners are unverifiable, so they are deleted and
rebuilt from the attempt substrate (attempt pick-up re-measures in place, never
re-encodes — 108/108 healed, zero encodes). For search runs that wipe is clearly right
(no info to validate; reproducing from attempts is cheap). For the fixed
unmeasured→measured transition (same everything else) it is plausibly fine as a
safe-guard — we have no winner-remeasure, only attempt-remeasure — but it deserves a
conscious ruling. The reverse transition (measured→unmeasured) deletes NOTHING:
attempt-aware completeness accepts the sidecar superset, all phases reused (verified
live, `fixed-both-downgrade.log`).

**Questions to think about:** is the wipe the right call for the fixed transition, or
should sidecar-less winners survive until the encoding replay replaces them pair-by-pair?
Broader: we lack a written map of how each phase adapts to every input CHANGE (plan
flag flips — measure/optimize/fixed-vs-search/quality, sampling, targets, crop,
re-chunk) — which artifacts get invalidated, wiped, or reused per phase. That matrix
should be a doc (a "phase change-adaptation matrix" — per phase × per change: reuse /
re-measure / re-encode / wipe), authored from the invalidation ladders + verified
transitions like the four in the §83 rework e2e.

---

## 143. --no-measure log/verdict cosmetics (survivors of the 0.30.3 rework)

From the §83 e2e matrices (2026-10-20/21, `D:\_encoding\tmp\nomeasure-e2e\RESULTS.md`):

- The fixed-mode banner line "merged-output measurement remains the final quality
  check" is stale under `--no-measure` (merge measurement is skipped) — the banner
  should drop or reword that line when the plan opts out.
- Search + `--no-measure` still persists merged rows with `metrics: {}`, and the
  live verdict renders a ✘ mark on them (empty metrics → False) — an opted-out row
  arguably should carry no verdict at all.
- Search + `--no-optimize` takes the all-strategies path with NO log line at all
  (fixed mode logs its skip line) — one teaching line for symmetry.

---

## 144. Crop params

Need to check effect of empty crop params (0,0,0,0) on measure / encoding.
If we should omit them both in the class' method and in outside consumers.

---

## Last known = 144

Keep this updated, so we can keep continuous numbering even on last todo item deletion.
Keep this the last entry for easy human updates.

---
