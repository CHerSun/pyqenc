# Experiment 5 — h265 presets, SEARCH to `high`, animation (test.mkv)

- Mode: searched, `--targets high` (the shipped target profile),
  `--no-optimize`; strategies `h265+{fast,medium,slow}`.
- The opposite test: fix the measured quality, compare what each preset
  converges to. Answers whether `medium`'s fixed-CR18 size advantage
  (−22.6%, Experiment 1) is real efficiency or a knob offset.
- Source: as Experiment 1. Rows run fastest → slowest; deltas vs `slow`.

## Cost and convergence

| preset | encode (search total) | ×time | merged MiB | Δsize vs slow | winning CRF min / p10 / med / p90 / max |
|---|---|---|---|---|---|
| fast | 1:34:37 | 0.38× | 523.9 | +23.8% | 6.0 / 10.4 / 14.5 / 17.5 / 30.0 |
| medium | 1:51:33 | 0.44× | 500.7 | +18.3% | 6.0 / 10.9 / 15.5 / 18.5 / 28.5 |
| **slow** | 4:11:17 | 1.00× | 423.4 | — | 6.0 / 13.5 / 17.5 / 20.5 / 29.5 |

## Delivered quality (merged, frame-exact @ sampling 1)

Bars: vmaf-p10 95.3 / vmaf-min 92.7 / psnr-med 46.4 / psnr-min 42.8 /
ssim-med 98.5 / ssim-min 97.4 / vif-med 93.5 / vif-min 88.5 (the shipped
`high` profile; p10 guards omitted for brevity — all met).

### vmaf

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 90.31 (−1.38) | 95.35 (−0.01 ≈) | 95.46 (−0.09 ≈) | 96.70 (−0.22) |
| medium | 90.97 (−0.72) | 95.38 (+0.02 ≈) | 95.52 (−0.03 ≈) | 96.74 (−0.18) |
| **slow** | 91.69 | 95.36 | 95.54 | 96.92 |

### psnr

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 43.52 (+0.54) | 45.95 (+0.23 ≈) | 46.33 (+0.18 ≈) | 48.35 (+0.40) |
| medium | 42.80 (−0.18 ≈) | 45.93 (+0.21 ≈) | 46.34 (+0.19 ≈) | 48.37 (+0.42) |
| **slow** | 42.98 | 45.72 | 46.15 | 47.95 |

### ssim

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 97.51 (+0.10) | 98.54 (+0.12) | 98.63 (+0.11) | 99.05 (+0.08) |
| medium | 97.47 (+0.07) | 98.49 (+0.07 ≈) | 98.59 (+0.07 ≈) | 99.06 (+0.09) |
| **slow** | 97.40 | 98.42 | 98.53 | 98.97 |

### vif

| preset | min | p05 | p10 | median |
|---|---|---|---|---|
| fast | 89.09 (+0.56) | 92.65 (+0.01 ≈) | 93.11 (−0.01 ≈) | 95.04 (+0.08 ≈) |
| medium | 88.57 (+0.05 ≈) | 92.60 (−0.05 ≈) | 93.10 (−0.02 ≈) | 95.04 (+0.08 ≈) |
| **slow** | 88.52 | 92.65 | 93.12 | 94.96 |

Readings from the delivered numbers:

- The stable ladder is equalized exactly as intended — every p05/p10/median
  difference between presets is inside noise on all four metrics. The size
  differences in the cost table buy nothing measurable beyond the bars.
- The one bar nothing fully holds is `vmaf-min` (92.7): delivered 91.69 /
  90.97 / 90.31 for slow / medium / fast — a single-frame floor the plain
  profile cannot fully hold (the floor value derives from the h265-crisp
  calibration measurement, §152); some chunks
  bottom out at the CRF 6.0 range floor chasing it.
- The cheaper presets converge at deeper CRFs, so their NON-binding stats
  ride above slow's: psnr-medians +0.40/+0.42, ssim +0.08…+0.12 — visible
  headroom that partly explains their +18–24% size.
- Self-consistency: `slow` converged at median CRF 17.5 — the same number
  the §152 calibration opposite runs produced for plain h265 under crisp
  bars.

## Verdict

**At equal measured quality, `slow` is the most rate-efficient of the three
— the fixed-CRF size story fully inverts.** At CRF 18 `medium` looked
22.6% smaller than `slow` (Experiment 1); searched to the same bars it is
**18.3% larger**: it must dig ~2 CRF steps deeper (winning median 15.5 vs
17.5) to buy back the quality it loses per bit. Its fixed-CR18 "bigness"
was quality, not waste.

The trade in one line each: `medium` saves 56% of the search encode time
and pays +18.3% size at identical delivered quality; `fast` saves 62% and
pays +23.8%. For the archiving posture (bits matter more than clock),
`slow` wins; for time-constrained runs the price list is now explicit.

### Grain and worst-frame notes

The only bar any preset misses is the single-worst-frame vmaf floor, and
the miss grows toward faster presets (91.6 → 90.9 → 90.3) — the worst-frame
quality ordering survives equalization: slower presets hold the worst
frame firmer even while the stable statistics are equalized by the search.
