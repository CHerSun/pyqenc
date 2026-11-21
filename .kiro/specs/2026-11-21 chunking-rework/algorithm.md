# The splitter — algorithm reference

Implementable specification of the validated boundary-selection algorithm.
Everything here is exactly what ran in the research runs (workspace
`stat_split.py`), stated without reference to that code. Inputs, outputs,
constants, formulas, edge cases.

## Inputs

- **Feature traces** (per frame, aligned by frame index over the whole
  source; from the scoring provider):
  - fast mode: one trace — the content score per frame;
  - slow mode: three traces — per-frame PSNR, per-frame SSIM, and
    `log1p(packet size in bytes)` from the probe encode.
- **Frame period** `frame_period` = last frame time / (frame count − 1),
  derived from the traces' own timestamps (NOT a hardcoded fps — sources
  differ: 23.976, 24.000, ...).
- **Params**: `min_scene_duration` (hard min, seconds; default 1.0),
  `target_scene_duration` (seconds; default 15.0).
- **Seeds** (slow mode only): the base detector's boundary list. Fast mode
  has no seeds — the tree alone produces every boundary.
- **PTS index** (`timestamps.txt`, per-frame PTS in ms) for anchoring.

Derived: `W = max(2, round(min_scene_duration / frame_period))` frames per
window side. Constant `k = 6.0` (global significance bar; see Rationale).

## Step 1 — noise scaling (per feature)

Sigma of a W-frame MEAN, per feature, from the source itself:

1. Split the trace into consecutive disjoint W-frame windows; take each
   window's mean.
2. `sigma = MAD(window_mean_diffs) / 0.6745 / sqrt(2)`, where the diffs are
   between ADJACENT disjoint windows and MAD is the median absolute
   deviation (median of |diff| — used directly as the robust scale).
3. Clamp below at a tiny epsilon (1e-9).

CRITICAL: sigma must be estimated at the window-mean scale, NOT from
frame-to-frame diffs. Frame traces are temporally correlated (slow drift);
frame-diff MAD understates window-mean noise by orders of magnitude and the
tree then carves noise everywhere (measured: 254 cuts on a 6.6-min source
vs the correct ~117). This estimator is the entire "self-scaling" property:
grainy/noisy sources inflate sigma (auto-desensitize), clean sources deflate
it (auto-sensitize). No per-source tuning exists.

Standard error for a left-vs-right mean comparison:
`se_f = sigma_f * sqrt(2)`.

## Step 2 — measure once (sliding, every frame)

For every frame index `i` in `[0, n)` (compute via per-feature prefix sums,
O(n)):

```
ml = mean(feature[i-W : i])     # W frames ending just before i
mr = mean(feature[i : i+W])     # W frames starting at i
t_f(i) = |ml - mr| / se_f       # per feature f
```

Frames nearer than W to either end get t = 0. Combined score
`score(i) = max_f t_f(i)`; `feat_at(i)` = the argmax feature. Edge cases:
W wider than the trace → no candidates; a trace shorter than 2W produces no
boundaries from that feature.

## Step 3 — sorted application (the tree)

1. Order all frames by descending `score`.
2. Walk the order; stop at the first `score < k`.
3. For each candidate frame `i`: position `t = placed(i)` (Step 4); accept
   iff every existing boundary is ≥ `min_scene_duration` away from `t`
   (checked against current accepted set + seeds); append.

This is the entire tree: the sort order IS the tree (strongest split first,
min-scene legality). Properties (both verified live):
- **Nested in k**: the per-candidate score is k-independent; lowering k
  only admits more cuts inside existing segments — boundaries never move
  or vanish (the stability requirement).
- **Readings never go stale**: windows span only ±W = ±min_scene, and
  legality forbids cuts closer than min_scene, so any surviving candidate's
  windows never contain a neighbor's boundary. No re-measurement is ever
  needed when the tree deepens.

## Step 4 — placement (firing feature)

`placed(i)`: within `[i-W, i+W]`, take the frame maximizing
`t_{feat_at(i)}` (the feature that FIRED at i), return its timestamp.
Positioning follows the deciding feature, not whichever feature spikes
highest at the chosen frame.

## Step 5 — target-duration pass (floor-gated descent)

Repeat until no segment exceeds `target_scene_duration`:

1. For each over-long segment `[a, b)` (bounded by current boundaries):
   candidates = frames with `score > 0` inside
   `[a + min_scene, b - min_scene]`.
2. `best` = the candidate with max score; `m` = candidate count.
3. **Floor**: `sqrt(2 * ln(m))` for m > 1 else 0. If `score(best) < floor`,
   the segment is STATISTICALLY UNIFORM — leave it whole (memoize the
   rejection by its `(a, b)` pair; a rejected segment's candidate set never
   changes).
4. Else cut at `t = min(max(placed(best), a + min_scene), b - min_scene)`
   — CLAMP into the segment's min-scene margins. Without the clamp,
   placement can refine up to W toward an edge, the "split" produces a
   zero-length child, the over-long key survives, and the loop never
   terminates (this exact infinite loop happened).

The floor is an order-statistics bar: the expected maximum of m pure-noise
t-values is ~√(2 ln m). Beating it means the best point is structure, not
noise; not beating it means uniform (credits, long takes) — and uniform
segments gain nothing from splitting (verified: a 92 s visually-confirmed
single scene had best internal t = 3.34 vs floor 3.92 and was correctly
left whole; the hard-cap alternative would have diced it into ~7 pieces).

## Step 6 — PTS anchoring (mandatory)

Map every boundary frame to its PTS from the source's timestamps index and
store `timestamp_seconds = pts_ms[frame] / 1000` EXACTLY. Never the
idealized grid time `frame * frame_period`:

- Grid time > real PTS (ms rounding down): the boundary frame slips into
  the PREDECESSOR's `-t` window (encode count +1 → frame-preservation
  invariant fails).
- PTS − 1 ms: the seek floors into the PREVIOUS frame's slot → successor
  starts one early (same invariant failure, opposite direction).
- Exact PTS: the seek's skip test (`pts < ss`) keeps the boundary frame and
  the predecessor's limit (`pts < limit`) excludes it. Both sides correct.

Window names render at 3-decimal ms, so differently-anchored timestamps can
share a name — attempt stores must never be reused across re-anchored
chunkings (delete by name when re-anchoring).

## Rationale corners

- **k = 6**: a multiple-comparisons bar. Per movie we test ~n_frames
  candidates; at k=4 expect ~10 noise cuts per 150k tests, at k=3 hundreds.
  k=6 ≈ zero expected false cuts at movie scale. It may later be DERIVED
  from the effective candidate count (√(2 ln n_eff) ≈ 4.3–5.5) — treat 6 as
  the conservative validated point; the test2 [6,8) band is where a
  validation run arbitrates.
- **Weak-but-real shifts**: never lower k; widen W instead (t grows ~√W for
  persistent shifts, stays flat for noise).
- **Known validated results**: short source −4.5% (ultrafast ladder),
  movie −2.21% at equal per-chunk delivery, floor judgment confirmed by
  eye on the 92 s case, quality-vs-time curve 2.5× flatter (SSIM deep-dip
  seconds 50 → 19).

## Fast-mode differences

The identical machinery over ONE trace (content score) with no seeds. The
content-trace variant was NOT itself a validated scenario (validated trees
ran on codec traces / hybrid bases) — the implementation gate includes a
fast-mode validation run on the short source vs stock output.
