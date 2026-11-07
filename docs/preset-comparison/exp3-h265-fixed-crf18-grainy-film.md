# Experiment 3 — h265 presets, fixed CRF 18, grainy film (grainy-10min.mkv)

- Mode: fixed, `-q 18`, `--no-optimize`; presets as strategies, one run.
- Strategies: `h265+{fast,medium,slow,slower}`; plain `h265` profile;
  `slow` = shipped default and delta baseline.
- Source: `grainy-10min.mkv` — a 10-minute stream-copy cut from a 1966
  live-action film BDRemux (1080p, ~20 Mbit/s, heavy natural film grain;
  14,390 frames).
- Rows run fastest → slowest; deltas vs `slow`.

## Cost

| preset | encode | ×time | merged MiB | Δsize vs slow |
|---|---|---|---|---|
| fast | 0:17:54 | 0.23× | 437.4 | −34.3% |
| medium | 0:29:22 | 0.37× | 500.2 | −24.9% |
| **slow** | 1:18:45 | 1.00× | 665.8 | — |
| slower | 4:55:23 | 3.75× | 670.5 | +0.7% |

## vmaf (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 82.60 (−4.15) | 92.71 (−1.68) | 93.41 (−1.64) | 95.99 (−1.52) |
| medium | 83.38 (−3.37) | 93.04 (−1.34) | 93.74 (−1.32) | 96.38 (−1.14) |
| **slow** | 86.75 | 94.39 | 95.05 | 97.52 |
| slower | 87.43 (+0.68) | 94.42 (+0.04 ≈) | 95.13 (+0.08 ≈) | 97.54 (+0.02 ≈) |

## psnr (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 40.42 (−1.70) | 42.92 (−1.08) | 43.35 (−1.06) | 45.47 (−0.99) |
| medium | 40.63 (−1.49) | 43.11 (−0.89) | 43.54 (−0.87) | 45.64 (−0.82) |
| **slow** | 42.12 | 44.00 | 44.41 | 46.46 |
| slower | 42.40 (+0.28) | 44.06 (+0.06 ≈) | 44.45 (+0.04 ≈) | 46.55 (+0.09 ≈) |

## ssim (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 95.07 (−1.60) | 96.97 (−0.65) | 97.26 (−0.55) | 98.21 (−0.31) |
| medium | 95.18 (−1.50) | 97.08 (−0.54) | 97.35 (−0.46) | 98.27 (−0.26) |
| **slow** | 96.68 | 97.62 | 97.81 | 98.52 |
| slower | 96.76 (+0.08) | 97.64 (+0.02 ≈) | 97.83 (+0.02 ≈) | 98.53 (+0.01 ≈) |

## vif (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 80.34 (−4.54) | 86.42 (−2.62) | 87.41 (−2.36) | 90.65 (−1.83) |
| medium | 81.06 (−3.82) | 86.95 (−2.09) | 87.86 (−1.91) | 91.04 (−1.45) |
| **slow** | 84.88 | 89.04 | 89.77 | 92.49 |
| slower | 85.13 (+0.24 ≈) | 89.16 (+0.12 ≈) | 89.94 (+0.17 ≈) | 92.58 (+0.09 ≈) |

## Verdict

Same shape as Experiment 1, with wider margins:

- **`slow` is the knee here too.** `slower` costs 3.75× the time for noise:
  every p05/p10/median delta inside noise on all four metrics (single-frame
  mins move ≤ +0.68). Combined with Experiment 1, `slower` is ruled out on
  both content poles, and `veryslow`/`placebo` with it.
- **`medium`'s quality cost is larger on this source than on animation**
  (vmaf-p10 −1.32 vs −1.22, ssim-p10 −0.46 vs −0.22, vif-p10 −1.91 vs
  −1.38; worst-frame mins −1.5…−3.8). Its time saving shrinks the same way
  (0.37× vs 0.36× — unchanged), so the speed↔quality trade is strictly
  worse here: the same time discount buys more quality loss.
- `fast` is again dominated by `medium` (0.23× vs 0.37× time, clearly worse
  on every stat).

### Grain and worst-frame notes

This is the grain pole, and it widens exactly the metrics that track
texture: VIF (p10 −1.91 for medium) and the worst-frame mins (vmaf-min
−3.37, vif-min −3.82). If grain retention matters for a library, the
`medium`↔`slow` gap on this source is the number to weigh against the
2.7× time difference.
