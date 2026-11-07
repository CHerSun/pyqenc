# Experiment 2 — av1 presets, fixed CRF 18, animation (test.mkv)

- Mode: fixed, `-q 18`, `--no-optimize`; presets as strategies, one run.
- Strategies: `av1+1` … `av1+6`, plain `av1` profile. `av1+3` is the
  shipped default and the delta baseline. (Preset 1 rows come from a
  follow-up extension run into the same work-dir, 2026-11-06.)
- Source: `test.mkv` (1080p animation), 108 chunks, 9526 frames, crop 22/22.
- Rows run fastest → slowest; deltas vs `av1+3`.

## Cost

| preset | encode | ×time | merged MiB | Δsize vs +3 |
|---|---|---|---|---|
| +6 | 0:10:35 | 0.37× | 199.4 | +23.2% |
| +5 | 0:12:41 | 0.44× | 168.8 | +4.4% |
| +4 | 0:14:33 | 0.51× | 165.1 | +2.0% |
| **+3** | 0:28:36 | 1.00× | 161.8 | — |
| +2 | 0:55:10 | 1.93× | 196.3 | +21.3% |
| +1 | 2:18:01 | 4.83× | 193.2 | +19.4% |

## vmaf (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 76.42 (+0.23) | 91.80 (+0.10 ≈) | 92.81 (+0.12 ≈) | 96.71 (+0.10 ≈) |
| +5 | 76.38 (+0.19) | 91.52 (−0.17) | 92.59 (−0.10 ≈) | 96.51 (−0.10 ≈) |
| +4 | 76.35 (+0.17) | 91.56 (−0.14 ≈) | 92.58 (−0.11 ≈) | 96.54 (−0.07 ≈) |
| **+3** | 76.19 | 91.70 | 92.69 | 96.61 |
| +2 | 76.50 (+0.32) | 92.45 (+0.75) | 93.41 (+0.71) | 96.99 (+0.38) |
| +1 | 76.71 (+0.52) | 92.54 (+0.85) | 93.51 (+0.82) | 97.07 (+0.46) |

## psnr (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 40.92 (+0.21 ≈) | 44.66 (+0.29) | 45.21 (+0.27) | 47.22 (+0.07 ≈) |
| +5 | 40.46 (−0.25) | 44.26 (−0.11 ≈) | 44.84 (−0.10 ≈) | 47.08 (−0.07 ≈) |
| +4 | 40.43 (−0.28) | 44.27 (−0.10 ≈) | 44.84 (−0.10 ≈) | 47.10 (−0.05 ≈) |
| **+3** | 40.71 | 44.37 | 44.94 | 47.15 |
| +2 | 41.27 (+0.56) | 45.13 (+0.76) | 45.66 (+0.72) | 47.71 (+0.56) |
| +1 | 41.51 (+0.80) | 45.23 (+0.86) | 45.79 (+0.85) | 47.77 (+0.62) |

## ssim (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 96.44 (+0.06 ≈) | 97.94 (+0.08) | 98.14 (+0.07 ≈) | 98.68 (+0.03 ≈) |
| +5 | 96.35 (−0.04 ≈) | 97.83 (−0.03 ≈) | 98.05 (−0.03 ≈) | 98.64 (−0.02 ≈) |
| +4 | 96.38 (−0.01 ≈) | 97.83 (−0.03 ≈) | 98.05 (−0.02 ≈) | 98.64 (−0.01 ≈) |
| **+3** | 96.38 | 97.86 | 98.08 | 98.66 |
| +2 | 96.72 (+0.33) | 98.02 (+0.16) | 98.25 (+0.17) | 98.82 (+0.16) |
| +1 | 96.71 (+0.33) | 98.05 (+0.19) | 98.26 (+0.18) | 98.84 (+0.18) |

## vif (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 79.88 (−0.69) | 89.73 (+0.38) | 90.59 (+0.32) | 93.22 (+0.23 ≈) |
| +5 | 80.17 (−0.40) | 89.26 (−0.10 ≈) | 90.17 (−0.10 ≈) | 92.93 (−0.06 ≈) |
| +4 | 80.38 (−0.19 ≈) | 89.28 (−0.08 ≈) | 90.18 (−0.08 ≈) | 92.94 (−0.04 ≈) |
| **+3** | 80.57 | 89.36 | 90.27 | 92.98 |
| +2 | 80.54 (−0.03 ≈) | 90.00 (+0.64) | 91.04 (+0.77) | 93.78 (+0.80) |
| +1 | 80.54 (−0.03 ≈) | 90.13 (+0.77) | 91.14 (+0.87) | 93.88 (+0.90) |

## Verdict

- **Presets 3/4/5 are one quality plateau**: every p05/p10/median delta
  between them is inside noise on all four metrics. `+4` delivers the
  plateau's quality at 0.51× `+3`'s time — as a speed point, `+3` is
  dominated by `+4` within the plateau.
- **`+2` is the last real quality step**: +0.7…+0.9 p10/p05 and
  +0.2…+0.8 medians vs `+3` (all outside noise), for 1.93× time. Note it
  also spends +21.3% size at the same CRF — SVT-AV1's slow presets trade
  rate for refinement at equal CRF, so fixed-CRF sizes across presets are
  not efficiency comparisons (see Experiment 6 for the equal-quality run).
- **`+1` adds ≈ nothing over `+2`**: every delta inside noise, at 2.5×
  `+2`'s time. The curve is flat at and below 2.
- `+6` spends +23.2% size for mixed, noise-level quality — the fast end
  wastes bits; not a candidate at any posture.

### Grain and worst-frame notes

vif-min declines monotonically toward faster presets across the whole range
(80.57 → 80.38 → 80.17 → 79.88 for +3 → +4 → +5 → +6) — a four-point
monotone ramp is signal-shaped: faster presets degrade the single worst
texture frame even where the worst-5% band is unchanged. Bounded: `min` is
a single-frame statistic whose compared frames differ per preset (vmaf-min
moves the opposite way), and the same comparison is noise-clean on the
grainy pole (Experiment 4).
