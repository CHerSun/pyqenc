# Preset comparison (§140)

Empirical basis for the `default_preset` choice per codec, measured
2026-11-04…06. Posture: video archiving — quality first, a minor
quality-for-speed trade acceptable, bits and worst-frame quality weighted
over clock time. Each experiment lives in its own file under
[preset-comparison/](preset-comparison/); this page is the index and the
global verdicts.

## Method (shared by all experiments)

- Fixed runs: `-q 18`, `--no-optimize`, all presets as strategies of one
  run (nothing eliminates); every preset's merged output is measured
  frame-exact at `measurement.sampling: 1`. Search (opposite) runs: same
  shape plus `--targets <bars>`, no pinned knob.
- Quality = merged sidecar stats: min / p05 / p10 / median per metric
  (vmaf, psnr, ssim, vif). Median is the central measure; p05/p10 and min
  carry the worst-frame tail — min is what usually ruins perceived
  quality, so the tail columns are read alongside the medians, not as
  footnotes.
- Encode time = `metrics.yaml` dotted key `encoding.<strategy>`
  (concurrency 1). Deltas inside the §152-derived `acceptance_delta`
  (vmaf 0.15 / psnr 0.24 / ssim 0.07 / vif 0.25) are measurement noise,
  marked `≈`. The bars were derived from p10/median-class deltas; `min` is
  a single-worst-frame statistic with wider noise (and the compared worst
  frames differ per preset), so min deltas are trusted as trends or
  several-fold exceedances, never alone. p05 is the stable tail statistic.
- Table convention: rows run fastest preset → slowest, so quality reads
  downward; deltas are vs the codec's shipped default (`h265+slow`,
  `av1+3`).
- Timing context: 4-core Intel i5-4570 @ 3.2 GHz, ffmpeg 9.0.2,
  pyqenc 0.38.x. Absolute times are machine-specific; ratios transfer.
- Sources: `test.mkv` (1080p animation, 108 chunks, 9526 frames) and
  `grainy-10min.mkv` (10-min stream-copy cut from a 1966 live-action film
  BDRemux — heavy natural film grain) — the two content poles that matter
  for the preset question.

## Experiments

| # | Codec | Mode | Source | Presets | File |
|---|---|---|---|---|---|
| 1 | h265 | fixed CRF 18 | test.mkv | fast, medium, slow, slower | [exp1](preset-comparison/exp1-h265-fixed-crf18-animation.md) |
| 2 | av1 | fixed CRF 18 | test.mkv | 1–6 | [exp2](preset-comparison/exp2-av1-fixed-crf18-animation.md) |
| 3 | h265 | fixed CRF 18 | grainy-10min.mkv | fast, medium, slow, slower | [exp3](preset-comparison/exp3-h265-fixed-crf18-grainy-film.md) |
| 4 | av1 | fixed CRF 18 | grainy-10min.mkv | 1–6 | [exp4](preset-comparison/exp4-av1-fixed-crf18-grainy-film.md) |
| 5 | h265 | search → `high` | test.mkv | fast, medium, slow | [exp5](preset-comparison/exp5-h265-search-high-animation.md) |
| 6 | av1 | search → `high` | test.mkv | 1, 2, 3, 4 | [exp6](preset-comparison/exp6-av1-search-high-animation.md) |
| 7 | av1 | search → `high`, re-chunked (threshold 21) | test.mkv | 1, 2, 3, 4 | [exp7](preset-comparison/exp7-av1-search-high-rechunked-animation.md) |

Fixed experiments (1–4) measure the direct tradeoff — time and size vs
delivered quality at the same knob. Search experiments (5–6) are the
opposite direction — quality pinned by bars, presets compared on
converged size, knob, and time; they separate real rate efficiency from
knob offsets. **Experiment 6 note:** target profiles are codec-agnostic by
design — the h265-crisp calibration only chose the bar VALUES; the bars
are the same objective quality standard for every codec. An earlier av1
search run substituted custom derived bars (av1+3@CRF18 levels) for
`high`; it was retracted — not comparable to Experiment 5 — and replaced
by a clean rerun under the same `high` targets. A bar a codec cannot
reach is a measurement, not a defect of the bar (as with the h265 chunks
that bottomed at the CRF 6.0 range floor in Experiment 5, and av1's
vmaf-min ceiling in Experiment 6).

## Global verdicts

**h265: `default_preset: slow` — unchanged, now measured from both
directions.** `slow` is the knee of the time↔quality curve on both content
poles: `slower` buys nothing measurable at 3.8–3.9× the time (Exps 1, 3 —
every stable-stat delta inside noise, both sources; `veryslow`/`placebo`
ruled out with it), and `medium` pays a real quality step (−1.2…−1.6
p10/median, worse on grain — Exp 3) for its 0.36–0.37× time. Searched to
equal quality the same ordering holds in efficiency: `medium` needs +18.3%
size (Exp 5), so its fixed-CR18 size advantage was a knob offset. `medium`
remains the legitimate speed alternative at that now-explicit price.

**av1: `default_preset: 4` — confirmed by both search runs.** The earlier
`2` recommendation came from the retracted derived-bars experiment and
does not survive: under the common `high` standard `+2` is the LEAST
efficient preset (largest at equal delivered quality in both chunkings,
Exps 6–7 — its per-CRF bitrate premium is not repaid under search).
`+4` matches `+3`'s delivered quality at every measured statistic at
half the time, in fixed mode (Exps 2/4) and searched mode (Exps 6/7)
alike — in the cleaner re-chunked run it is also smaller (616.1 vs
638.8 MiB). `+1` delivers the best worst frame (+1.80 vmaf-min over
`+3`) and, once the chunking skew is removed, the smallest output
(600.0 MiB) — at 3.78× time; preset 0 closed untested (`+1` ≈ `+2` at
pinned CRF, Exps 2/4). The fixed ladders still stand: at a pinned CRF
`+2` is the best quality with the grain-tail gains (film: vmaf-min
+0.99, vif-min +0.82) — worth considering for fixed-mode runs despite
its searched-mode cost. Exp 7's wider lesson: one mixed-scene 39.5 s
chunk cost more searched size than the entire preset spread — boundary
quality is a first-order lever (feeds §162/§47).

### Cross-codec notes

- The two codecs tell DIFFERENT stories under the objective standard, and
  the difference is instructive. h265's knee (`slow`) wins both directions
  — its bars are near-reachable, so search converges on real targets and
  refinement converts to efficiency. av1's bars include a floor its worst
  frame cannot reach at any preset (~83–85 vs 92.7), so parts of the
  search run in best-effort mode — and there the refinement of `+2` does
  NOT convert to efficiency (+16.7% size at equal delivery): reachable
  bars reward slower presets, unreachable ones can invert them.
- Fixed-CRF sizes across presets are never efficiency comparisons — h265
  inverted part of its fixed-CRF story under search (medium −22.6% →
  +18.3%, Exp 5); av1's `+2` went from +21.3% at pinned CRF 18 to +16.7%
  even at equal delivered quality (Exp 6).
- Worst-frame (min) behavior tracks the preset ladder on both codecs —
  slower presets hold the worst frame firmer (h265 searched: 91.6 / 90.9 /
  90.3; av1 searched: 85.1 / 84.0 / 83.3 for +1 / +2 / +3) — and the
  cross-codec worst-frame gap (h265 ~91 vs av1 ~83–85 under the same bars,
  this source) is the number to weigh for content with pathological
  single frames. Single worst frames are what viewers notice first.
