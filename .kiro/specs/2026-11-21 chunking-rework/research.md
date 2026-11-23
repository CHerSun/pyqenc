# Scene detection research — summary

Research period: 2026-11-13 → 2026-11-21. Sources: a 6.6-min animation short
("test1", the long-standing reference sample) and a 92-min live-action movie
("test2"), plus offline census traces for three more movies (91–128 min).
Full working record: the research workspace `PLAN.md` (not in the repo).

Status (2026-11-23): this is the record of the research phase, kept as
written. Three things in it no longer describe the product:

- "The final algorithm" below is the window-mean splitter, which is not
  implemented. Its fast-mode variant failed its gate run on 2026-11-22
  and was replaced by the causal-Hampel descent (`algorithm.md`); the
  window-mean reference is kept in `algorithm-window-mean.md`.
- The results below (−4.5%, −2.21%) were measured for that splitter with
  the content detector's cut list as its base layer; they do not transfer
  to the implemented one.
- The companion fix this record points to — skipping boundary-frame
  readings at merge — was retracted on 2026-11-22 (measurement sanctity
  ruling). The merged-vs-winner consistency invariant in `design.md`
  replaced it.

## Motivation

The stock chunking (PySceneDetect `ContentDetector` + linear first-crossing
threshold) had two measured problems, inherited from the §140 campaign:

1. **Instability** — retuning the threshold moves/removes existing boundaries
   non-locally (threshold 24→21 measured live: cuts added AND removed AND
   shifted; the §47 finding). Boundary churn orphans encoding investments.
2. **Misplacement** — the strongest content score near a real difficulty
   transition can sit seconds away from it (exp7: split ~7 s early), and
   gradual difficulty shifts produce no content cut at all (a 39.5 s mixed
   chunk cost 20% of an entire short's output).

Goal: encoding scenes (segments homogeneous in encoding difficulty), stable
across re-tuning (nested cut family), with config-controlled lengths, and no
opaque per-source tuning values.

## What was checked

- **Score sources**: PySceneDetect content scores; ffmpeg `scdet`; per-frame
  PSNR/SSIM vs an ultrafast probe encode; per-packet probe bitrate; a
  z-score composite of the three probe metrics.
- **Selection policies**: linear threshold (stock), sorted-scores sweep,
  recursive bisection, recursive descent, level-spread pocket isolation,
  fragility (quality-level) gates, and finally the measure-once statistical
  splitter.
- **Presets/profiles**: the whole ultrafast ladder plus a slow-preset matrix
  (plain h265, h265-crisp, h265-aq) to separate chunking effects from codec
  effects.
- **Whole-movie A/B**: reference chunking vs the final design on the 92-min
  movie, equal targets, sampling 1.

## What each scoring source gives

| Source | Measures | Strengths | Blind spots | Role |
|---|---|---|---|---|
| content scores (scenedetect) | per-frame HSV change — visual discontinuity | real cuts, precise, one cheap decode | gradual shifts; near difficulty transitions its peak sits seconds away; thresholds on it unstable + opaque | base layer — cuts are its monopoly |
| ffmpeg `scdet` | single-frame scene-change score | same class as content scores | far sparser (8 boundaries where content gave 119; nothing inside the known offender) | none — dominated everywhere we looked |
| probe PSNR (per frame, fixed CRF) | fidelity level — fragility axis | the 39.5 s offender's 45→40.5 dB cliff; fragility steps | head readings dominated its raw rankings; weak ranker alone | slow-mode feature |
| probe SSIM (per frame) | structural fidelity level | pure-fragility cases (movement vs static-grain window: the only signal, peak 11.345) | cost-blind (grain shifts bits, not SSIM) | the fragility detector |
| probe bitrate (per packet) | cost at fixed CRF — difficulty axis | strongest single signal (the ~8× cliff; every difficulty pocket) | confounded by grain (expensive-but-stable) and dark-detail omission (cheap-but-fragile) | the difficulty detector |
| composite (z-score mean) | all three collapsed | convenient | dilution — hid the movement-vs-grain case; retired | rejected |

One line: **content sees events; the probe trio sees regimes** — difficulty
(bits) and fragility (psnr/ssim) are independent (correlations 0.14–0.51),
so the slow mode needs all three features and collapsing them loses exactly
the rare cases that motivated the work.

## How the final algorithm works

1. **Base**: the content detector's cuts (visual discontinuities are its
   monopoly — codec-only placement measurably loses elsewhere).
2. **Scoring** (mode-dependent): fast = content-score trace (one decode);
   slow = probe encode (h265 ultrafast, fixed CRF, full res) + per-frame
   PSNR/SSIM + packet sizes → feature traces [psnr, ssim, log-bits].
3. **Measure once, sliding**: per feature, a Welch t at every frame — the
   level shift between min-scene-length windows on each side, in units of
   the source's own window-mean noise (σ from adjacent disjoint windows;
   self-scaling: noisy sources auto-desensitize). Split positioned at the
   firing feature's peak. (An early "head guard" excluding the opening
   seconds was dropped on review: the elevated head readings are real
   black-intro regimes, not artifacts, and cutting there matches the stock
   detector's own first boundary.)
4. **Sorted-application tree**: candidates strongest-first with min-scene
   legality; the tree IS the sort order → nested in k by construction
   (deepening never moves upper boundaries — the stability goal met
   structurally).
5. **Length policy**: hard min (config) always; target duration (config)
   via best-candidate
   descent gated by the order-statistics floor √(2·ln m) — a segment is
   "uniform" when its best internal candidate doesn't beat the expected
   noise-max of m candidates, and uniform segments stay whole regardless of
   length (credits/long takes).
6. **PTS anchoring**: every boundary equals the frame's real PTS from the
   extraction index — grid timestamps leak frames across windows on
   ms-quantized sources (three failed runs proved the exact-PTS rule).

Constants: duration bounds (user-facing, encoding mechanics) and ONE
dimensionless k (global significance bar ≈ multiple-comparisons scale);
everything else derives from the data.

## Results

| Comparison | Result |
|---|---|
| test1 ladder (ultrafast, equal delivery) | 531.8 → 507.8 MiB = **−4.5%** (recursive splits −1.9%, difficulty pockets −1.8%, fragility −0.7%) |
| test1 slow-preset matrix | chunking win holds (−1.3%); aq profile closes the dark-scene vmaf gap (all targets, +13.7% cost); crisp does not |
| Census (4 movies, no encoding) | 29–92 over-long segments per movie (max 9.2 min) vs test1's 2 — the win ceiling scales with movie length |
| test2 whole-movie A/B | **−2.21%** (6,793.6 → 6,643.3 MiB) at equal per-chunk delivery; wins concentrated in split mixed segments |
| Delivered-quality stability (test1, SSIM over time) | deep-dip seconds: stock 50 → ladder 19–20 (2.5× flatter); worst splitter 62 (worse than stock). Chunking shapes the quality-vs-time curve itself, not just size; PSNR shows nothing (saturated ~48 dB) — the effect lives in the sensitive metrics |
| Visual + statistical cross-check | a 92 s mid-movie segment confirmed "one scene" by eye; the floor's below-noise-max verdict agreed |

## Principles established

- Content cuts are well-placed for ordinary scenes; codec traces earn their
  keep only at difficulty cliffs and inside detector-gap segments.
- A single bisection of a heterogeneous segment REPLICATES the drag (the
  remaining half still parks at its worst sub-part's CRF) — splits must
  recurse toward homogeneity.
- The probe carries two independent heterogeneity axes: difficulty (bitrate)
  and fragility (quality-at-fixed-CRF); each needed its own detector.
- Every extra window pays acceptance margins — statistical justification
  must precede every split (wholesale splitting lost +4.4–4.7%).
- Per-chunk first-frame readings are skipped in per-chunk stats but counted
  in merged stats → merged minima are chunk-count-biased. (The companion
  fix this pointed to was retracted on 2026-11-22; the merged-vs-winner
  consistency invariant in `design.md` replaced it.)

## Production bycatches (independent of chunking)

1. Language-suffixed Matroska duration tags (`DURATION-eng`) invisible to
   the stream inventory → probe reports unknown duration → chunking fatal.
2. Merged-output min stats count chunk-boundary frames that per-chunk stats
   skip — cross-chunking comparisons of merged minima are invalid until the
   boundary skip is applied at merge. Corrected 2026-11-22: that skip was
   retracted — readings are never skipped to make numbers agree; merged
   minima are held to the merged-vs-winner consistency invariant in
   `design.md` instead.
