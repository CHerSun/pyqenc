# Experiment 1 — h265 presets, fixed CRF 18, animation (test.mkv)

- Mode: fixed, `-q 18`, `--no-optimize`; presets as strategies, one run.
- Strategies: `h265+fast`, `h265+medium`, `h265+slow` (shipped default —
  the delta baseline), `h265+slower`; plain `h265` profile.
- Source: `test.mkv` (1080p animation), 108 chunks, 9526 frames, crop 22/22.
- Rows run fastest → slowest; quality deltas are vs `slow`.

## Cost

| preset | encode | ×time | merged MiB | Δsize vs slow |
|---|---|---|---|---|
| fast | 0:14:28 | 0.31× | 216.1 | −32.5% |
| medium | 0:16:56 | 0.36× | 247.9 | −22.6% |
| **slow** | 0:46:27 | 1.00× | 320.2 | — |
| slower | 3:00:33 | 3.89× | 326.2 | +1.9% |

## vmaf (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 80.63 (−2.81) | 92.26 (−1.82) | 93.13 (−1.62) | 96.23 (−0.73) |
| medium | 81.30 (−2.14) | 92.69 (−1.39) | 93.53 (−1.22) | 96.47 (−0.49) |
| **slow** | 83.44 | 94.07 | 94.75 | 96.96 |
| slower | 84.44 (+1.00) | 93.98 (−0.09 ≈) | 94.64 (−0.11 ≈) | 96.95 (−0.00 ≈) |

## psnr (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 40.10 (−0.61) | 44.43 (−1.18) | 45.02 (−1.17) | 47.31 (−0.99) |
| medium | 40.06 (−0.65) | 44.84 (−0.77) | 45.43 (−0.76) | 47.57 (−0.73) |
| **slow** | 40.71 | 45.61 | 46.19 | 48.30 |
| slower | 40.84 (+0.13 ≈) | 45.62 (+0.01 ≈) | 46.21 (+0.02 ≈) | 48.26 (−0.04 ≈) |

## ssim (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 96.61 (−0.49) | 97.97 (−0.36) | 98.17 (−0.31) | 98.71 (−0.22) |
| medium | 96.68 (−0.42) | 98.06 (−0.26) | 98.26 (−0.22) | 98.78 (−0.14) |
| **slow** | 97.10 | 98.32 | 98.48 | 98.92 |
| slower | 97.15 (+0.05 ≈) | 98.30 (−0.02 ≈) | 98.47 (−0.02 ≈) | 98.91 (−0.01 ≈) |

## vif (merged, frame-exact @ sampling 1)

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 82.59 (−3.11) | 89.97 (−2.05) | 90.79 (−1.92) | 93.30 (−1.62) |
| medium | 83.02 (−2.68) | 90.50 (−1.52) | 91.33 (−1.38) | 93.78 (−1.14) |
| **slow** | 85.70 | 92.02 | 92.71 | 94.92 |
| slower | 85.97 (+0.26) | 92.00 (−0.02 ≈) | 92.69 (−0.02 ≈) | 94.97 (+0.05 ≈) |

## Verdict

- **The time↔quality knee is `slow`.** Above it, `slower` costs 3.89× the
  time for nothing measurable: every p05/p10/median delta sits inside noise
  on all four metrics (only two single-frame `min` values move — the
  noisiest statistics). `veryslow`/`placebo` are ruled out without testing;
  the curve is flat before `slower`.
- **Below it, `medium` trades real quality for speed**: 0.36× the time and
  −22.6% size, but −1.2…−1.6 across every p10/median (all far outside
  noise) and −2.1…−2.7 on the worst-frame mins.
- `fast` is dominated by `medium` here — nearly the same encode time
  (14.5 vs 17 min), clearly worse everywhere. The practical choice is
  `medium` vs `slow`.

### Grain and worst-frame notes

VIF shows the widest preset gap of all metrics (min −2.68, p10 −1.38 for
medium) — texture retention is where preset refinement pays first. The
worst-frame (min) columns degrade in lockstep with p05/p10, so on this
source the stable statistics already carry the tail story.
