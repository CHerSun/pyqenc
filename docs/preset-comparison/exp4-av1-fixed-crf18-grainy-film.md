# Experiment 4 — av1 presets, fixed CRF 18, grainy film (grainy-10min.mkv)

- Mode: fixed, `-q 18`, `--no-optimize`; presets as strategies, one run.
- Strategies: `av1+1` … `av1+6`, plain `av1` profile. `av1+3` is the
  shipped default and the delta baseline. (Preset 1 rows come from a
  follow-up extension run into the same work-dir, 2026-11-06.)
- Source: as Experiment 3 (1966 film BDRemux cut, heavy natural grain).
- Rows run fastest → slowest; deltas vs `av1+3`.

## Cost

| preset | encode | ×time | merged MiB | Δsize vs +3 |
|---|---|---|---|---|
| +6 | 0:13:06 | 0.36× | 449.2 | +32.1% |
| +5 | 0:15:15 | 0.42× | 351.5 | +3.4% |
| +4 | 0:17:24 | 0.48× | 347.3 | +2.2% |
| **+3** | 0:36:11 | 1.00× | 340.0 | — |
| +2 | 1:08:13 | 1.89× | 364.1 | +7.1% |
| +1 | 3:28:39 | 5.77× | 363.8 | +7.0% |

## vmaf (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 75.70 (+0.36) | 93.20 (+0.23) | 93.96 (+0.19) | 96.63 (+0.32) |
| +5 | 75.37 (+0.03 ≈) | 92.68 (−0.29) | 93.50 (−0.27) | 96.21 (−0.11 ≈) |
| +4 | 75.57 (+0.22) | 92.69 (−0.27) | 93.53 (−0.25) | 96.21 (−0.10 ≈) |
| **+3** | 75.34 | 92.97 | 93.78 | 96.31 |
| +2 | 76.34 (+0.99) | 93.43 (+0.46) | 94.22 (+0.44) | 96.69 (+0.38) |
| +1 | 76.32 (+0.97) | 93.66 (+0.70) | 94.44 (+0.66) | 96.87 (+0.56) |

## psnr (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 35.71 (+0.08 ≈) | 43.48 (+0.22 ≈) | 44.03 (+0.29) | 45.89 (+0.52) |
| +5 | 35.59 (−0.04 ≈) | 43.16 (−0.10 ≈) | 43.67 (−0.07 ≈) | 45.32 (−0.05 ≈) |
| +4 | 35.63 (0.00 ≈) | 43.17 (−0.09 ≈) | 43.66 (−0.08 ≈) | 45.31 (−0.06 ≈) |
| **+3** | 35.63 | 43.26 | 43.74 | 45.37 |
| +2 | 35.88 (+0.25) | 43.45 (+0.19 ≈) | 43.89 (+0.15 ≈) | 45.67 (+0.30) |
| +1 | 35.88 (+0.25) | 43.60 (+0.34) | 44.03 (+0.29) | 45.78 (+0.41) |

## ssim (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 88.51 (+0.18) | 97.19 (+0.18) | 97.49 (+0.16) | 98.34 (+0.14) |
| +5 | 88.22 (−0.11) | 96.99 (−0.03 ≈) | 97.31 (−0.03 ≈) | 98.18 (−0.02 ≈) |
| +4 | 88.33 (−0.00 ≈) | 96.98 (−0.03 ≈) | 97.30 (−0.03 ≈) | 98.18 (−0.02 ≈) |
| **+3** | 88.33 | 97.01 | 97.33 | 98.20 |
| +2 | 88.89 (+0.55) | 97.07 (+0.06 ≈) | 97.39 (+0.05 ≈) | 98.28 (+0.09) |
| +1 | 88.88 (+0.55) | 97.14 (+0.12) | 97.43 (+0.10) | 98.30 (+0.11) |

## vif (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| +6 | 72.05 (+0.22 ≈) | 86.91 (+0.55) | 88.09 (+0.66) | 91.35 (+0.57) |
| +5 | 71.72 (−0.11 ≈) | 86.21 (−0.16 ≈) | 87.34 (−0.09 ≈) | 90.69 (−0.10 ≈) |
| +4 | 71.85 (+0.02 ≈) | 86.22 (−0.14 ≈) | 87.34 (−0.10 ≈) | 90.68 (−0.10 ≈) |
| **+3** | 71.83 | 86.36 | 87.43 | 90.78 |
| +2 | 72.65 (+0.82) | 86.59 (+0.22 ≈) | 87.72 (+0.28) | 91.20 (+0.42) |
| +1 | 72.65 (+0.82) | 86.81 (+0.45) | 87.95 (+0.52) | 91.35 (+0.57) |

## Verdict

- **The ladder is ordered `+2 > +3 > +4 ≈ +5`, and `+2` is again the last
  real step.** Vs `+3`: vmaf-p10 +0.44, vmaf-med +0.38, vif-med +0.42,
  psnr-med +0.30 — every one outside noise — for 1.89× time and +7.1% size
  at the fixed CRF.
- **`+1` ≈ `+2` here as on animation**: over `+2` it adds +0.22 vmaf-p10
  (the single bar-crossing delta) and +0.24 vif-p10 (inside noise), with
  everything else flat, at 3.05× `+2`'s time. The curve is flat at and
  below 2 on both poles — preset 0 was closed without testing on this
  evidence.
- **`+4`/`+5` sit on `+3`'s plateau** (all p05/p10/median deltas inside
  noise, including the mins on this source) at 0.48×/0.42× time — within
  the plateau, speed comes nearly free.
- `+6` repeats the fast-end inefficiency: +0.1…+0.6 quality movements at
  +32.1% size.

### Grain and worst-frame notes

`+2`'s gains over `+3` concentrate in the worst-frame tails — vmaf-min
+0.99, vif-min +0.82, ssim-min +0.55 — several-fold beyond any min-stat
noise, the strongest quality signal in the av1 campaign and exactly the
"use 2 or lower for grain retention" guidance. The fixed-CRF +7.1% size
penalty is partly this quality; Experiment 6 separates it into efficiency.
