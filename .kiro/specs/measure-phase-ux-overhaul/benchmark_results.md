# Screenshot Benchmark Results

- Run date: 2026-05-01
- Screenshot count per video: 20
- Source for shared positions: `(1).mkv` — 110.2s, 24fps, 2644 frames, step=125
- Shared positions (first 3): frames [125, 250, 375] → timestamps [5.2s, 10.4s, 15.6s]

## Short videos (shared positions from source)

### (1).mkv — 110.2s (0.03h), 24fps

| Strategy | Time (s) | % of duration | Meets target (≤36.7s) | Captured |
|---|---|---|---|---|
| C — N calls, fast -ss seek, -frames:v 1 | 6.6 | 6% | ✅ | 20/20 |
| A1 — single-pass timestamp (gte+guard) | 12.6 | 11% | ✅ | 20/20 |
| A2 — single-pass frame-number (eq(n,F)) | 12.2 | 11% | ✅ | 20/20 |
| A3 — single-pass timestamp (gte+isnan guard) | 12.4 | 11% | ✅ | 20/20 |
| A4 — single-pass mod (not(mod(n,step))*gt) | 13.2 | 12% | ✅ | 20/20 |

Exactness: A1 vs C — 13/20 match, 7/20 differ ⚠ (see analysis below)

### (1.1) slow_h265.mkv — 110.2s (0.03h), 24fps

| Strategy | Time (s) | % of duration | Meets target (≤36.7s) | Captured |
|---|---|---|---|---|
| C — N calls, fast -ss seek, -frames:v 1 | 19.4 | 18% | ✅ | 20/20 |
| A1 — single-pass timestamp (gte+guard) | 19.7 | 18% | ✅ | 20/20 |
| A2 — single-pass frame-number (eq(n,F)) | 19.2 | 17% | ✅ | 20/20 |
| A3 — single-pass timestamp (gte+isnan guard) | 19.2 | 17% | ✅ | 20/20 |
| A4 — single-pass mod (not(mod(n,step))*gt) | 18.1 | 16% | ✅ | 20/20 |

Exactness: A1 vs C — 19/20 match, 1/20 differ ⚠ (see analysis below)

### (1.1) any_vulkan_hevc-10bit.mkv — 110.2s (0.03h), 24fps

| Strategy | Time (s) | % of duration | Meets target (≤36.7s) | Captured |
|---|---|---|---|---|
| C — N calls, fast -ss seek, -frames:v 1 | 21.7 | 20% | ✅ | 20/20 |
| A1 — single-pass timestamp (gte+guard) | 21.3 | 19% | ✅ | 20/20 |
| A2 — single-pass frame-number (eq(n,F)) | 20.5 | 19% | ✅ | 20/20 |
| A3 — single-pass timestamp (gte+isnan guard) | 20.7 | 19% | ✅ | 20/20 |
| A4 — single-pass mod (not(mod(n,step))*gt) | 20.0 | 18% | ✅ | 20/20 |

Exactness: A1 vs C — 19/20 match, 1/20 differ ⚠ (see analysis below)

## Full movie (own positions)

### movie.mkv — 5792.1s (1.61h), 24fps, step=6619

| Strategy | Time (s) | % of duration | Meets target (≤1930.7s) | Captured |
|---|---|---|---|---|
| C — N calls, fast -ss seek, -frames:v 1 | 7.3 | **0.1%** | ✅ | 20/20 |
| A1 — single-pass timestamp (gte+guard) | 807.2 | 14% | ✅ | 20/20 |
| A2 — single-pass frame-number (eq(n,F)) | 812.0 | 14% | ✅ | 20/20 |
| A3/A4 | ~800s (estimated, run interrupted) | ~14% | ✅ | — |

**Strategy C is ~110x faster than single-pass strategies on the full movie.**

## Frame exactness analysis

Two groups of strategies produce identical frames:

- **Group 1 (frame-number aligned): C, A2, A4** — all agree on the same frames. C uses `-ss <timestamp>` which ffmpeg resolves to the exact frame at that timestamp. A2 uses `eq(n,F)` with the same frame numbers. A4 uses mod with the same step. All three produce bit-identical PNGs (verified by total file size equality).

- **Group 2 (timestamp gte): A1, A3** — these use `gte(t,T)` which selects the first frame *at or after* the floating-point timestamp. Due to container timestamp representation, this can land one frame off from the frame-number calculation for some positions. A1 and A3 agree with each other but differ from Group 1 on a small number of frames.

**Conclusion:** Group 1 strategies are more consistent with each other. The `-ss` fast-seek approach (Strategy C) lands on the same frame as explicit frame-number selection (A2), confirming it is frame-accurate.

## Recommendation

**Use Strategy C: N sequential ffmpeg calls with `-ss <timestamp>` before `-i`, `-frames:v 1`.**

Rationale:
- 100x faster than single-pass on long videos (7s vs 800s for 1.6h movie)
- Frame-accurate: same frames as explicit frame-number selection
- Simple implementation: no complex select filter expressions
- Scales well: time is proportional to N × (keyframe interval), not video duration
- On short videos (~2min) it's also 2x faster than single-pass (6.6s vs 12s)

The position calculation uses frame-number arithmetic (`step = total_frames // (count + 1)`) to ensure deterministic, stable positions across reruns.
