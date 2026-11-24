# Chunking rework — change spec (draft for review)

<!-- markdownlint-disable MD024 -->

- Created: 2026-11-21
- Completed:

Replaces the threshold-based scene detection with a two-mode statistical
chunking. Research basis: `research.md` (same folder). Algorithm
reference, as implemented: `algorithm.md` (same folder). The
research-phase splitter it replaced, not implemented:
`algorithm-window-mean.md` (same folder).

REVISED 2026-11-23 to the implemented design. The 2026-11-21 draft
specified a different splitter and duration-based length keys; what the
draft said and why each piece was replaced is recorded under
[Design history](#design-history).

## Goals

- Encoding scenes: segments homogeneous in encoding difficulty.
- No detector threshold and no opaque per-source value: the end-user
  tunes one key — the minimum scene length, in frames.
- Both modes share ONE splitter; only the scoring differs.
- Stability — carried over from the draft; delivered in practice, not by
  construction. The draft asked for a nested cut family: a re-tune only
  subdivides, boundaries never move or vanish. The descent offers
  candidates by raw content score, so a looser setting can in principle
  let a stronger-raw candidate take the place of the frame that split a
  chunk before. Measured on the reference source it does not happen:
  tightening either setting only removes boundaries, loosening adds
  none, and nothing relocates (significance bar 2 → 8: 115 → 113;
  minimum length 12 → 24 frames: 115 → 110).

## Non-goals

- No change to `build_chunks`, recovery, or downstream phases (the boundary
  list remains the only contract; scenario injection proved the existing
  sidecar shape carries new boundaries unchanged).
- The probe phase stays metadata-only — the slow-mode probe encode is
  chunking's own work, not probe's.
- No per-frame trace persistence in sidecars: traces are workspace
  intermediates (cleanup-level governed); sidecars store mode + results.

## Architecture — separation of concerns

Three seams inside ChunkingPhase:

1. **Scoring provider** (selected by `chunking.mode`):
   - `fast` (default): one decode pass producing the per-frame
     content-score trace. PySceneDetect is used purely as a scorer (its
     stats manager); its cut-decision logic is not consumed anywhere and
     no detector threshold exists in the pipeline.
   - `slow`: the content trace plus a probe encode (h265 ultrafast,
     fixed CRF, full res) measured against the source, giving three
     extra per-frame feature traces: PSNR, SSIM and
     `log1p(packet size in bytes)`. Cost ≈ real-time on the source
     (measured: a couple of hours for a 92-min 1080p movie at 2 cores).
2. **Splitter** (mode-independent, pure function —
   `pyqenc.chunking_split.split_boundaries(content, pts, min_scene_length,
   extra_traces)`): the causal-Hampel descent.
   - **Significance**: per-frame causal Hampel z — each frame against
     the 12 frames strictly before it (local median and 1.4826·MAD
     scale). Direction-biased like the content scorer itself, so a ramp
     with history self-cancels.
   - **Content guards** (raw score units — guards, not rulers): a floor
     (content score ≥ 15) and a jump from the centered 7-frame average
     (≥ 10). The jump is the anti-panning guard: a ramp onset from a
     quiet baseline collapses the local MAD and reads as a huge z, but
     lacks the absolute departure a cut has.
   - **Descent**: top-down. Each chunk is offered its frames in
     descending content score; the first candidate at least
     `min_scene_length` frames from both chunk edges whose significance
     (max over features) clears the bar (k = 3) splits it; both children
     repeat. A chunk with no qualifying candidate is statistically
     uniform and stays whole at any length — there is no target-length
     pass and no forced split.
   - **Extra features** (slow mode): PSNR/SSIM/log-bits enter the same
     descent as ungated, scale-free Hampel z, OR-maxed with the guarded
     content z.
   - **Anchoring**: every boundary is the exact frame that fired and
     carries its PTS from the extraction timestamps index.
   - **Frame alignment**: the frame axis is the pipeline's. Its length
     is the frame count of the extraction timestamps index — the same
     frame lines `count_frames` counts for the probe — and the content
     scorer is given that count and must deliver a score for every one of
     those frames. The decoder's own frame count is never consulted: the
     OpenCV backend estimates it from the container duration (the longest
     stream's, which can outlast the video: 9528 against 9526 on the
     reference source). A decoder that falls short of the index's frames
     or scores past them fails the phase, and so does any provider trace
     of another length; nothing is trimmed or padded. The index's
     end-of-stream entry is told from a frame line in one place
     (`utils/timestamps.py`). Probe packet sizes are ordered by
     presentation timestamp for the same reason.
   - The significance bar and the guards are algorithm constants
     (`pyqenc/chunking_split.py`), not config. The lookback lives in
     `constants.py` because it is also the config minimum's bound.
3. **Phase wiring**: ChunkingPhase runs provider → splitter, prepends
   the stream-start boundary, and persists mode + minimum length +
   scenes to `chunking.yaml`. No interior boundaries → the whole video
   is one chunk.

A simpler neighbor-gap guard (`score − max(prev, next) > 10`) produced
an identical boundary set on the reference source. Not adopted: a
2-neighbor gate can swallow fast (~3-frame) blends that the 7-frame
average still sees. It is documented at `centered_jump` in the module.

## Config

`chunking:` section:

| key | status | default | meaning |
|---|---|---|---|
| `mode` | new | `fast` | `fast` \| `slow` (extensible for future modes) |
| `min_scene_length` | kept — the pre-rework key and unit (frames); default 24 → 15; lower bound: the splitter's lookback (`HAMPEL_LOOKBACK`, 12) | `15` | hard minimum segment length in FRAMES — the only length ruler |
| `scene_threshold` | REMOVED | — | replaced by the statistical significance bar (an algorithm constant) |

The draft's duration-based keys (`min_scene_duration`, 1.0 s, and
`target_scene_duration`, 15.0 s) were never shipped. Frames were kept
because the splitter's legality check is a frame count and the value is
consumed downstream by per-chunk measurement. The target was dropped
because the descent has no target-length pass in either mode. The
minimum's lower bound is the splitter's lookback, read from the same
constant (`HAMPEL_LOOKBACK`): a shorter minimum would let a chunk's
significance reach into the previous chunk.

The default 15 is the value the fast mode was validated with (boundary
detection against the full visual inspection). Whether 15-frame chunks
hold up for per-chunk statistics is part of the validation run.

Probe operating point (slow mode) stays internal constants for now — it is
a scoring calibration, not a user surface; revisit if a need appears.

CLI: `--scene-threshold` / `--scene-min-length` are replaced by
`--chunking-mode {fast,slow}` and `--min-scene-length FRAMES` (same
one-flag-one-meaning rule as the rest of the CLI).

## Sidecar (`chunking.yaml`)

Persists: source fingerprint (unchanged), `mode`, `min_scene_length`,
`scenes` (PTS-anchored, frames + timestamps as today). Invalidation: a
change of either parameter key re-detects (same semantics as the old
threshold key); a sidecar written before the rework fails validation
and re-detects. The significance bar and the guards are algorithm
constants, not config — they are not invalidation keys.

## Where the change belongs

- `pyqenc/chunking_split.py` (new): the splitter as a pure function.
- `pyqenc/phases/chunking.py`: scoring providers + splitter wiring + the
  sidecar keys; `detect_scenes()` retired.
- `pyqenc/app_config.py` + `default_config.yaml`: the config keys above.
- `pyqenc/cli.py`: flag changes.
- Tests: property suite for the splitter (minimum-length gaps in frames,
  Σ counts telescope, PTS anchoring, termination, ramp and ramp-onset
  rejection, extra-feature firing) + provider smoke tests.
- Docs: `docs/glossary.md`, `docs/architecture.md`, `docs/cli-reference.md`.

## Companion fixes (land independently, regardless)

1. `DURATION*` language-suffixed tag parsing in the stream inventory
   (already local, gates green; COMMITTED 382dcf4).
2. ~~Merged-output measurement applies the per-chunk first-reading skip at
   every chunk boundary~~ RETRACTED (user ruling 2026-11-22): the ONLY
   allowed skip is VMAF's first reading of a measurement session — one per
   session, merged or chunk. Measurements are never skipped to make
   numbers agree; boundary frames measuring badly is an encoding-side
   problem to investigate, not hide.

## Validation plan (implementation gate)

1. **Fast mode, boundary detection** — the reference source against the
   user's full visual inspection (111 must-break / 5 optional /
   8 must-not-break groups). PASSED offline 2026-11-23: 115 boundaries =
   all 111 must-break groups + 4 optional, no must-not-break violation,
   no unaccounted boundary; the prod splitter reproduces the lab set
   frame for frame.
2. **Fast mode, pipeline** — one search run on the reference source with
   the shipped chunking, compared with the stock-chunking reference run
   at equal settings (size, per-chunk delivery). RESULT 2026-11-23:
   neutral. Merged 531.53 MiB against 531.77 MiB (−0.05%), 9526 frames
   preserved. All targets met on the merged output except vmaf-min, in
   both runs (88.4 / 88.0 against 92.7 — chunks exhausted at the CRF
   floor: 6 here, 7 with stock chunking, the same pockets). p10 and min
   stats within ±0.07 of stock; the medians moved most — psnr −0.41 dB,
   vif −0.20, ssim −0.04, vmaf 0.00 — all inside their bars. 91 windows
   are shared with identical CRF; the difference sits in 10 regions:
   −2.88 MiB in the ending (eight cuts stock missed) and −1.80 MiB at
   29–35 s, against +4.31 MiB at 5.8–15.2 s, where a stock cut the visual
   inspection rejects (no break, but an encoding-difficulty boundary) is
   gone — content-only scoring cannot see it. Boundaries: 100 shared;
   15 only in the new set, all in must-break groups; 7 only in the stock
   set — 4 in must-not-break groups, 1 optional, and 2 the same blend
   events placed one frame later (peak frame instead of first frame over
   the threshold: 1506/1505 and 8288/8287).
3. **Merged-vs-winner consistency invariant** (user directive 2026-11-22,
   born from the merged-min investigation): in every e2e run, per min stat,
   the merged-output measurement must sit at or above the winner
   population's floor — no discrepancies. psnr/ssim/vif have no skips, so
   equality is expected there (same frames, same encodes; large gaps =
   investigate the specific frames, as the t2 psnr-min 37.2 glitch and
   the t2-stat vmaf-min 84.6 dip both demanded). vmaf carries the known
   session-first-frame allowance (per-chunk stats never judge chunk-first
   frames; the merged session does) — a dip there flags a chunk-start
   frame worth looking at, not a measurement error. The e2e harness checks
   all four and FAILS the validation on any unexplained discrepancy.
   PASSED on the run above: the merged minimum equals the winner floor
   on all four min stats (psnr 43.93, ssim 97.645, vif 88.605, vmaf
   88.371).
4. **Static + suite gates.** PASSED 2026-11-23 for ruff, ty and the
   non-slow suite. The e2e pytest suite still hangs in ffmpeg
   (TODO §114) and was not run; the pipeline run above is the e2e
   evidence.
5. **Fast mode on the movie source** — one search run with the shipped
   chunking against the stock-chunking reference at equal settings,
   sharing its attempts store. RESULT 2026-11-24, at the bar of 4: merged
   6738.2 against 6795.1 MiB (−0.84%), 132,336 frames preserved; target
   statistics within 0.03 of stock on the merged output; psnr/ssim/vif
   minima equal their winner floors exactly. 702 of 839 windows identical
   to stock's with the same CRF. Two findings: the merged VMAF minimum
   sits on a chunk's first frame (TODO §171), and the bar of 4 refused
   stock's cut at 806.875 s (content 31.35, z 3.37), leaving a 20 s
   window below bar. The user confirmed that cut by eye; the bar was
   lowered to 3.
6. **Slow mode — not validated.** The draft's replay gate (reproduce the
   −4.5% ladder set) and the movie-scale A/B (−2.21%) measured the
   retired Welch splitter with the detector cut list as its base; they
   do not transfer to the descent with ungated codec-trace features.
   Slow mode needs its own ground truth and gate.
7. **Rerun with the bar of 3** on the movie source, same settings and
   the same shared attempts store. RESULT 2026-11-24: merged 6735.0 MiB
   (−0.88% against stock, −0.05% against the bar of 4), 132,336 frames
   preserved; 10 regions changed, 98 s of video, 3.1 MiB less. The
   window that missed vmaf-min and vif-min at the bar of 4 (13:25–13:45)
   is split where stock splits it, and both parts pass. The one that
   missed vmaf-p10 (13:08–13:25) shrinks to a 3.3 s part that still
   misses it. Merged vif-min is back at stock's value; the other merged
   statistics are within 0.02 of the bar-4 run. psnr, ssim and vif
   minima equal their winner floors; the merged VMAF minimum is the
   same chunk-first frame as above (TODO §171).

## Slow mode — state of the work (2026-11-24)

Coded, not validated, and its method not yet settled.

**As coded** (`slow_scoring` in `phases/chunking.py`): a probe encode
(h265 ultrafast, fixed CRF, full res, no crop) is measured against the
source for per-frame PSNR and SSIM; per-packet byte sizes, ordered by
PTS, give the third trace. All four traces — content first — go through
the same descent as fast mode: PSNR, SSIM and log-bits enter as ungated
causal Hampel z, OR-maxed with the guarded content z. The probe encode
and its traces are workspace intermediates under `chunking/`. It runs
end to end on the reference sample; nothing more is known about it.

**What the research established for codec traces** (`research.md`,
`algorithm-window-mean.md`): the probe trio sees what the content score
cannot — on the short source the movement-to-grain transition near
11.7 s and the difficulty cliff near 05:15 produce no content event.
Difficulty (bits) and fragility (PSNR/SSIM) are independent, so all
three features are needed. The two-window splitter on these traces, on
the content detector's cut list as its base, measured −4.5% on the
short source and −2.21% on the movie.

**To settle first — which method:**

- The coded shape: the fast descent with the codec traces as extra
  features.
- The two-window splitter on the codec traces, the research-validated
  reference, on top of the fast-mode boundary set.

The research's slow design used the content detector's cut list as its
base. That list is used nowhere now; the base is the fast-mode result.

**Known hazards of the coded shape:**

- The extra features have no raw guard: a nearly flat stretch of a codec
  trace gives a tiny deviation a large z.
- The probe's frame rate is derived from the PTS axis; the rest of the
  pipeline passes the stream's declared fraction.
- The probe costs about real time on the source.

**Ground truth:** the short source's visual marks cover content-visible
breaks; its codec-only transitions are not yet marked. The validated
prototype (`stat_split.py`) and the research traces are in the research
workspace, not in the repo.

## Design history

- **2026-11-21 — draft.** Splitter: measure-once sliding Welch t over
  min-scene-length windows, strongest-first sorted-application tree
  nested in k = 6, floor-gated target-duration pass (√(2·ln m)), PTS
  anchoring. Base layer: the detector's cut list in slow mode only; in
  fast mode the tree over the content trace was the whole boundary set.
  Config: duration-based `min_scene_duration` (1.0 s) and
  `target_scene_duration` (15.0 s). The retired splitter's full text is
  kept in `algorithm-window-mean.md`.
- **2026-11-22 — fast-mode gate run failed.** 28 interior boundaries
  against the stock chunking's 107, misplaced. Cause: the content score
  is an impulse/event signal and the Welch t is a level-shift statistic
  — a cut spike dilutes 24× into a window mean and never clears the bar.
  An interim compromise (the detector's cut list as a base layer in both
  modes, at an internal threshold) was rejected by the user — a hidden
  opaque threshold with the same instability — and removed.
- **2026-11-22/23 — user lab.** The user's top-down descent
  (`tree_descent_cher`) with swappable local-significance scorers, plus
  a full visual inspection of the reference source as ground truth.
  Converged on causal Hampel z (lookback 12), raw floor 15, jump guard,
  k = 4, 15-frame minimum. Tuning findings: the causal window beats the
  centered one; lookback 12 beats 5 and 24; the raw floor is needed
  (without it: 274 boundaries); a centered 7-frame jump replaced a
  trailing 5-frame one and removed the last must-not-break violation.
- **2026-11-23 — implemented** as specified above. A working-tree
  interim variant (global-z Hampel sorted tree, event-start placement)
  was replaced by it.
- **2026-11-24 — bar lowered from 4 to 3** after the movie-scale run. In
  a busy neighborhood the Hampel z of a real cut drops below 4 (stock's
  cut at 806.875 s: content 31.35, jump 20.4, z 3.37). At 3 the movie
  source gains 10 boundaries — all content 20–44 and jumps 13–28, four
  of them stock's own — and the short source is unchanged.
