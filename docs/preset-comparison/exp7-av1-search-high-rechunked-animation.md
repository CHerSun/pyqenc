# Experiment 7 — av1 presets, SEARCH to `high`, re-chunked, animation (test.mkv)

- Mode: searched, `--targets high`, `--no-optimize`; strategies
  `av1+{1,2,3,4}`; same work-dir as Experiment 6, resumed.
- Motivation: exp6's largest single size driver was one 39.5 s chunk
  mixing a ~3 s dark opening with bright content — every preset's search
  dragged the WHOLE chunk to the CRF floor chasing `vif_min` on the dark
  part (~300 MB at near-lossless rate; the +1/+2 winners there: CRF 4).
- Re-chunking: `--scene-threshold 21` (from 24). The dark→bright transition
  peaks at content_val 21.41 (t=309.06 s). Boundary churn vs 24: 5 cuts
  added (incl. the target), 4 removed, 2 shifted — 94% of boundaries
  stable, net 108 → 109 chunks; the churn itself is evidence for §47's
  stability problem (path-dependent linear scan).
- Timing note: incremental rerun into a shared work-dir — `metrics.yaml`
  accumulates, so per-strategy encode times are not attributable; the
  fresh encode+measure span was ~10.25 h (four presets' re-derived chunks
  only; all other chunks replayed stored attempts).

## Cost and convergence (sizes; timing not attributable — see note)

| preset | exp6 MiB (108 chunks) | exp7 MiB (109 chunks) | Δ | winning CRF min / p10 / med / p90 / max |
|---|---|---|---|---|
| +4 | 664.2 | 616.1 | −48.1 | 4.0 / 6.0 / 11.0 / 18.0 / 30.0 |
| **+3** | 644.1 | 638.8 | −5.3 | 4.0 / 6.0 / 11.0 / 18.0 / 30.0 |
| +2 | 751.6 | 662.0 | −89.6 | 4.0 / 6.0 / 13.0 / 23.0 / 30.0 |
| +1 | 679.1 | 600.0 | −79.1 | 4.0 / 7.0 / 13.0 / 23.0 / 30.0 |

## Delivered quality (merged, frame-exact @ sampling 1)

Targets as Experiment 6 (`high`). All stable bars met by every preset;
`vmaf-min` (92.7) still unreachable — identical picture to exp6 (mins
83.26 / 83.27 / 83.99 / 85.06 for +3 / +4 / +2 / +1), confirming the
04:04-chunk VMAF quirk is orthogonal to this re-chunk. Delivered tables
vs `+3` (full values in the run artifacts):

- vmaf / psnr / ssim / vif: every p05/p10/median delta inside noise for
  all four presets; the stable ladder stays equalized (psnr-median rides
  ~3.5 above the bar, ssim ~0.7, vif ~2.0 — same overdelivery as exp6,
  marginally compressed by the cleaner chunking).

## Verdict

- **The mixed-scene chunk was a first-order size driver.** Removing it
  shaves 5–90 MiB per preset (largest exactly where the skew bit deepest:
  +2 −89.6, +1 −79.1), halves the inter-preset spread (107.5 → 62.0 MiB),
  and reorders the ranking: `+1` becomes the smallest (600.0), `+4` second
  (616.1) and now BELOW `+3` (638.8); `+2` stays the largest (662.0).
  Exp6's "+3 most efficient" crown was partly a skew artifact — with
  clean boundaries the four presets sit in a ±5% band.
- **The robust findings survive both chunkings**: the `+2` searched-size
  anomaly (largest at equal delivery — its per-CRF bitrate premium is not
  repaid), `+4` = `+3`'s delivered quality at half the time (now also
  smaller), `+1`'s worst-frame lead (+1.80 vmaf-min) at multi-× time, and
  the vmaf-min unreachability itself.
- **The shipped default `4` is confirmed by the cleaner run**: equal
  delivered quality, −3.6% size vs `+3`, half the encode time.
- Chunking moral: boundary quality rivals preset choice as a size lever
  in searched runs — one bad 39.5 s chunk cost more than the entire
  preset spread. Feeds §162 (visibility) and §47 (stable splitting).

### Post-run follow-up (visual check)

The split did not land at the visual transition: the content-score peak
(t=309.06 s) sits ~7 s BEFORE the actual dark→bright change, so the
remaining 36.6 s chunk still opens with ~7 s of dark content — it still
converges to CRF 4–6 and still carries roughly 200 MB of overhead (down
from ~300 MB, still ugly). Beyond instability, `scene_threshold`
chunking MISPLACES boundaries: the content detector's peak is not the
encoding transition. See the campaign's chunking verdict and TODO
§47/§162.

### Grain and worst-frame notes

Unchanged from exp6 — the worst-frame ceiling (~83–85) lives in the
04:04 chunk's VMAF interaction, untouched by this re-chunk; +1 holds the
best worst-frame (+1.80) at 3.78× time.
