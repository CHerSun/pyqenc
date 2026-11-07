# Experiment 6 — av1 presets, SEARCH to `high`, animation (test.mkv)

- Mode: searched, `--targets high` (the shipped profile, verbatim — same
  standard as Experiment 5), `--no-optimize`; strategies `av1+{1,2,3,4}`.
- Supersedes a retracted derived-bars run (custom av1+3@CRF18 targets) that
  was not comparable to Experiment 5; target profiles are codec-agnostic by
  design — an unreachable bar is a measurement of the codec's shortfall,
  not a defect of the bar.
- Run note: interrupted once (machine needed) and resumed from stored
  attempts — no re-encodes, encode times are true totals.
- Source: as Experiments 1/2. Rows run fastest → slowest; deltas vs `+3`.

## Cost and convergence

| preset | encode (search total) | ×time | merged MiB | Δsize vs +3 | winning CRF min / p10 / med / p90 / max |
|---|---|---|---|---|---|
| +4 | 1:14:50 | 0.55× | 664.2 | +3.1% | 4.0 / 6.0 / 11.0 / 17.1 / 30.0 |
| **+3** | 2:16:58 | 1.00× | 644.1 | — | 4.0 / 6.0 / 11.0 / 18.0 / 30.0 |
| +2 | 3:56:08 | 1.72× | 751.6 | +16.7% | 4.0 / 6.0 / 13.0 / 23.0 / 30.0 |
| +1 | 8:37:53 | 3.78× | 679.1 | +5.4% | 4.0 / 6.9 / 13.5 / 22.1 / 30.0 |

## Delivered quality (merged, frame-exact @ sampling 1)

Bars (`high`): vmaf-p10 95.3 / vmaf-min 92.7 / psnr-med 46.4 / psnr-min
42.8 / ssim-med 98.5 / ssim-min 97.4 / vif-med 93.5 / vif-min 88.5 (p10
guards omitted for brevity — all met).

### vmaf

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +4 | 83.27 (+0.01 ≈) | 95.55 (+0.02 ≈) | 95.86 (+0.02 ≈) | 97.26 (+0.02 ≈) |
| **+3** | 83.26 | 95.52 | 95.84 | 97.24 |
| +2 | 83.99 (+0.73) | 95.51 (−0.02 ≈) | 95.80 (−0.04 ≈) | 97.26 (+0.02 ≈) |
| +1 | 85.06 (+1.80) | 95.51 (−0.02 ≈) | 95.83 (−0.01 ≈) | 97.25 (+0.01 ≈) |

### psnr

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +4 | 42.85 (+0.01 ≈) | 46.10 (0.00 ≈) | 46.52 (+0.01 ≈) | 50.02 (−0.01 ≈) |
| **+3** | 42.84 | 46.10 | 46.51 | 50.03 |
| +2 | 42.88 (+0.04 ≈) | 46.05 (−0.05 ≈) | 46.48 (−0.03 ≈) | 49.86 (−0.17 ≈) |
| +1 | 42.82 (−0.02 ≈) | 46.05 (−0.05 ≈) | 46.48 (−0.02 ≈) | 49.64 (−0.39) |

### ssim

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +4 | 97.42 (−0.00 ≈) | 98.51 (+0.00 ≈) | 98.63 (+0.00 ≈) | 99.20 (−0.00 ≈) |
| **+3** | 97.42 | 98.51 | 98.63 | 99.20 |
| +2 | 97.45 (+0.03 ≈) | 98.48 (−0.03 ≈) | 98.61 (−0.02 ≈) | 99.18 (−0.01 ≈) |
| +1 | 97.42 (+0.00 ≈) | 98.47 (−0.04 ≈) | 98.60 (−0.03 ≈) | 99.14 (−0.05 ≈) |

### vif

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +4 | 88.54 (+0.01 ≈) | 92.79 (+0.03 ≈) | 93.31 (+0.02 ≈) | 95.64 (+0.08 ≈) |
| **+3** | 88.53 | 92.77 | 93.30 | 95.56 |
| +2 | 88.50 (−0.02 ≈) | 92.61 (−0.16 ≈) | 93.16 (−0.13 ≈) | 95.49 (−0.07 ≈) |
| +1 | 88.52 (−0.01 ≈) | 92.58 (−0.18 ≈) | 93.12 (−0.18 ≈) | 95.33 (−0.23 ≈) |

Readings from the delivered numbers:

- Every preset meets every stable bar (vmaf-p10, medians, p10 guards, and
  the psnr/ssim/vif min floors all pass with surplus) — the search
  equalizes them to noise on everything except vmaf-min.
- **vmaf-min (92.7) is unreachable for av1 on this source at every preset**:
  delivered 83.26 / 83.27 / 83.99 / 85.06 for +3 / +4 / +2 / +1 — an
  av1 worst-frame ceiling of ~83–85, where h265 reached 90.3–91.7 under the
  same standard (Experiment 5). The floor is met per-chunk only by the
  chunks that can; the rest converge in best-effort mode.
- The worst-frame ordering survives equalization (+1 best at +1.80 over
  +3), but the stable ladder is fully equalized — the size differences in
  the cost table buy nothing measurable beyond the bars and the vmaf-min
  residue.

## Verdict

- **Under the common objective standard, `+3` is the most efficient av1
  preset and `+2` the least**: 644.1 vs 751.6 MiB (+16.7%) at
  equal-quality-everywhere-except-the-unreachable-floor. The retraction
  note: the earlier derived-bars run had shown the opposite (−8.0% for +2)
  — under its tighter, av1-tailored bars every preset converged in a
  regime where +2's per-CRF refinement paid; under `high`'s best-effort
  regime, +2's much higher per-CRF bitrate (fixed runs: +21% at CRF 18)
  is not repaid, and the net flips.
- **`+4` again matches `+3` at half the time** (+3.1% size, every
  delivered stat ≈ noise incl. the vmaf-min miss) — the 3/4 equivalence
  now holds in fixed AND searched mode on both sources.
- **`+1`** buys the best worst-frame delivery (vmaf-min +1.80 over +3) for
  +5.4% size and 3.78× time — the only thing it ever bought, here too
  bought more cheaply by nothing else, and too expensive as a default.
- The fixed-ladder picture (Exps 2/4) still stands: at a PINNED CRF, +2
  delivers the best quality. But the product's primary mode is search
  toward objective targets, and under that standard +2 costs +16.7% size
  for +0.73 vmaf-min — the default choice belongs to the search-standard
  winners: +3 (smallest) or +4 (equal quality, half time).

### Grain and worst-frame notes

The cross-codec worst-frame gap is the headline tail finding: av1's
vmaf-min ceiling (~83–85) sits ~7 points below h265's (~91) under the same
bars on this source. The +2 grain-tail gains of the fixed ladders
(film: vmaf-min +0.99, vif-min +0.82) remain real at pinned CRF, but under
search they surface only as the +0.73 vmaf-min residue above.

**The vmaf-min floor is one chunk, not a general av1 tail decay.** Winner
sidecars: every av1 preset's worst chunk is the same 4.8 s segment
(00:04:04–00:04:09), capped at vmaf-min 84.0–86.6 even at CRF 4 — the
range floor — while its OTHER worst-frame stats at that CRF say
near-transparent (vif-min 96.4–96.8, psnr-min 50.9–51.7). Pixels nearly
identical, one metric floor-bound: a content×VMAF interaction on that
segment's frames, not a coding failure the knob can buy back. h265's own
worst chunk is the same segment (also range-bottoming there, delivering
91.6–93.7); the next av1 chunks sit at 92.2–93.0, straddling the bar. The
size consequence is large: the unreachable floor keeps chunks digging
(winning-CRF p10 6.0 vs h265's 10.4) and overdelivering every other stat
(psnr-med 50.0 vs h265's 48.4) — a meaningful share of av1's +52% size vs
h265 under `high` is this dynamic, not clean rate inefficiency.
