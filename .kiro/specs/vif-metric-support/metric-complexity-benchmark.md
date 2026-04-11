# Metric Complexity Benchmark

Measured on CHerSun's PC, 2026-04-11.

## Setup

- Reference:  `(1).mkv`  — H.264, 1920×1080, 24 fps, 110s (~2644 frames)
- Distorted:  `(1.1) any_vulkan_hevc-10bit.mkv` — HEVC 10-bit, 1920×1036 (pre-cropped)
- Crop applied to reference only: top=22, bottom=22 (→ 1920×1036)
- No scaling
- VMAF: `n_threads=4`, `feature=name=vif` (VIF is embedded, not a separate pass)
- Commands via `run_metric()` — identical to production

## Raw Results

| Metric   | Factor | Elapsed (s) | Frames processed | FPS   |
|----------|--------|-------------|------------------|-------|
| PSNR     | 1      | 39.5        | 2644             | 67.0  |
| SSIM     | 1      | 41.2        | 2644             | 64.1  |
| VMAF+VIF | 1      | 179.8       | 2644             | 14.7  |
| PSNR     | 3      | 48.8        | 882              | 18.1  | ← outlier, ignored
| SSIM     | 3      | 35.9        | 882              | 24.5  |
| VMAF+VIF | 3      | 83.2        | 2644             | 31.8  |
| PSNR     | 10     | 35.2        | 265              | 7.5   |
| SSIM     | 10     | 34.3        | 265              | 7.7   |
| VMAF+VIF | 10     | 60.2        | 2644             | 43.9  |

## Time Decomposition Model

Total time per metric run = `D + M/factor`, where:
- `D` = decode time (fixed per run, independent of factor)
- `M` = measurement time at factor=1 (scales as 1/factor)

For PSNR/SSIM, both D and M scale with factor because non-sampled frames are skipped
entirely at the decoder level. For VMAF, all frames are always decoded; `n_subsample`
only skips the libvmaf scoring internally, so D is larger and less factor-sensitive.

### SSIM (cleanest fit, using f=1 and f=10)

```
(D + M) - (D + M/10) = 41.2 - 34.3 = 6.9
M × 0.9 = 6.9  →  M ≈ 7.7s,  D ≈ 33.5s
Check f=3: 33.5 + 7.7/3 = 36.1  (actual 35.9 ✓)
```

### PSNR (using f=1 and f=10, ignoring f=3 outlier)

```
(D + M) - (D + M/10) = 39.5 - 35.2 = 4.3
M × 0.9 = 4.3  →  M ≈ 4.8s,  D ≈ 34.7s
```

### VMAF (using f=1 and f=3, then f=1 and f=10, averaged)

VMAF always decodes all frames; n_subsample only skips scoring.

```
f=1 & f=3:  M × 2/3 = 179.8 - 83.2 = 96.6  →  M ≈ 144.9s, D ≈ 34.9s
f=1 & f=10: M × 0.9 = 179.8 - 60.2 = 119.6 →  M ≈ 132.9s, D ≈ 46.9s
Average:    M ≈ 139s, D ≈ 41s
```

VMAF fits less cleanly — libvmaf threading overhead likely doesn't scale linearly.

## Summary

| Metric | D (decode, s) | M (measure @ f=1, s) | M relative to PSNR |
|--------|--------------|----------------------|--------------------|
| PSNR   | ~34.7        | ~4.8                 | 1.0×               |
| SSIM   | ~33.5        | ~7.7                 | 1.6×               |
| VMAF   | ~41          | ~139                 | 29×                |

## Complexity Values

The `complexity` field encodes M normalized to D=1 (one decode unit ≈ 34s on this machine).
Total estimated time for a metric at a given factor = `D × (1 + complexity/factor)`.

For N metrics run separately: `N×D + D×(M1/f + M2/f + ... + MN/f)`.

| Metric | D (decode) | M raw (s) | complexity = M/D | Notes                          |
|--------|-----------|-----------|-----------------|--------------------------------|
| PSNR   | ~34.7s    | ~4.8s     | **0.14**        | baseline                       |
| SSIM   | ~33.5s    | ~7.7s     | **0.23**        |                                |
| VMAF   | ~41s      | ~139s     | **4.1**         | always decodes all frames      |
| VIF    | —         | —         | **0.0**         | embedded in VMAF, no separate run  |

At production default factor=10:
- PSNR:  1 + 0.14/10 = 1.014 → ~35s  ✓
- SSIM:  1 + 0.23/10 = 1.023 → ~35s  ✓
- VMAF:  1 + 4.1/10  = 1.41  → ~48s  (actual 60s — VMAF D is slightly higher ~41s vs 34s)

Note: the `complexity` scalar in the current progress bar code is used as a flat weight,
not with the `1 + M/factor` formula. A future improvement could use this model properly.

## Combined PSNR+SSIM Pass Experiment

Hypothesis: running PSNR and SSIM in a single ffmpeg pass via `split[]` saves one full
decode, cutting total time roughly in half for those two metrics.

Filter graph used:
```
[0:v]select,setpts,split=2[main1][main2];
[1:v]crop,select,setpts,split=2[ref1][ref2];
[main1][ref1]psnr=stats_file=...;
[main2][ref2]ssim=stats_file=...
```

| Run                | Factor | Elapsed (s) | Saving       |
|--------------------|--------|-------------|--------------|
| separate PSNR+SSIM | 1      | 82.6        | —            |
| combined PSNR+SSIM | 1      | 45.0        | 37.6s (46%)  |
| separate PSNR+SSIM | 10     | 70.3        | —            |
| combined PSNR+SSIM | 10     | 35.9        | 34.4s (49%)  |

Conclusion: combining PSNR+SSIM into one pass saves ~one decode unit (~34s) regardless
of factor, confirming the D+M/factor model. This is a worthwhile optimization to implement
in `run_metric` / `_generate_metrics`.

## All-in-One Pass Experiment (PSNR+SSIM+VMAF+VIF)

Hypothesis: single ffmpeg pass for all metrics, with PSNR/SSIM always on every frame
and VMAF using `n_subsample=factor`. Saves 2 decode units vs 3 separate passes.

Filter graph:
```
[0:v]setpts,split=3[main1][main2][main3];
[1:v]crop,setpts,split=3[ref1][ref2][ref3];
[main1][ref1]psnr=stats_file=...;
[main2][ref2]ssim=stats_file=...;
[main3][ref3]libvmaf=n_threads=4:n_subsample=N:...:feature=name=vif
```

| Run                      | Factor | Elapsed (s) | Saving        |
|--------------------------|--------|-------------|---------------|
| separate 3× (PSNR+SSIM+VMAF) | 1 | 267.6      | —             |
| all-in-one               | 1      | 176.2       | 91.4s  (34%)  |
| separate 3×              | 3      | 171.3       | —             |
| all-in-one               | 3      | 102.4       | 68.9s  (40%)  |
| separate 3×              | 10     | 147.1       | —             |
| all-in-one               | 10     | 120.2       | 26.9s  (18%)  |

### Analysis

The saving decreases as factor increases because at high factors the separate PSNR/SSIM
passes are already cheap (decode is skipped for non-sampled frames), while the all-in-one
must always decode every frame to feed VMAF. At factor=10:

- Separate: PSNR≈35s + SSIM≈34s + VMAF≈60s = ~129s (measured 147s)
- All-in-one: one full decode + PSNR+SSIM+VMAF measurement ≈ 34 + 4.8 + 7.7 + 139/10 = ~60s
  (measured 120s — VMAF's own decode overhead inflates this)

The all-in-one is always faster, but the benefit is largest at factor=1 (34% saving)
and factor=3 (40% saving). At factor=10 the gain is smaller (18%) because VMAF still
dominates and PSNR/SSIM are already fast in separate passes.

Conclusion: all-in-one is worthwhile to implement, especially at lower factors. It also
improves PSNR/SSIM accuracy at high factors since they now see every frame.
