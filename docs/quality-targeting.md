# Quality Targeting Guide

<!-- markdownlint-disable MD024 -->

`pyqenc` encodes each video chunk to meet a set of quality targets rather than a fixed bitrate or CRF. This guide explains what metrics are available, how to choose good target values, and how to tune them.

---

## Available metrics

Each metric measures similarity between the source and encoded frame on a 0–100 scale (100 = identical). They capture different aspects of visual quality, and none is sufficient on its own.

### VMAF

VMAF (Video Multi-Method Assessment Fusion) is a Netflix-developed perceptual metric that models the human visual system. It correlates well with perceived quality on typical filmed content.

**Strengths:** Good general-purpose perceived quality indicator. Well-calibrated for broadcast and streaming content. A score of 95+ is generally considered high quality; 98+ is visually near-lossless for smooth content.

**Limitations:**
- Strongly biased toward smooth, clean content. It rewards blurring fine texture (film grain, noise) because a denoised frame looks "cleaner" to its model. If preserving original grain matters, VMAF alone will push toward over-smoothing.
- The first frame of a chunk gets an artificially high score because VMAF lacks motion context at frame 0. This inflates the `min` statistic, making it an unreliable target. Use `vmaf-p05` instead of `vmaf-min`.
- Some content types — title cards, solid-color transitions — hit a VMAF ceiling around 97–97.5 even at very high bitrate. This is a known model limitation, not an encoding problem. `pyqenc` handles this gracefully by accepting the best achievable result.

### VIF

VIF (Visual Information Fidelity) is actually a component inside the VMAF computation, but `pyqenc` exposes it as a standalone metric. It measures how much visual information is preserved from source to encode.

**Strengths:** Less biased than VMAF toward smooth content. More sensitive to loss of fine texture and grain. A good companion to VMAF when grain retention matters. A score of 90–94 typically gives good retention of original noise structure.

**Limitations:** Less well-known reference points than VMAF. Not a replacement for VMAF — use both together.

### PSNR

PSNR (Peak Signal-to-Noise Ratio) is a classical engineering metric based on mean squared error between frames.

**Strengths:** Stable, reliable, and predictable. Well-understood. Resistant to the content-type biases of VMAF. A median around 44–48 dB typically corresponds to good perceptual quality.

**Limitations:** Does not model human perception — a numerically identical PSNR can look quite different depending on where errors occur. Use as a secondary control alongside perceptual metrics.

### SSIM

SSIM (Structural Similarity Index) measures luminance, contrast, and structure similarity between frames, normalised to a 0–100 scale here.

**Strengths:** Captures structural degradation well. Less pixel-level than PSNR.

**Limitations:** Highly non-linear near 100 — the difference between 98 and 99 is much larger than between 90 and 91 in visual terms. A median of 98+ is generally good; 99+ is near-lossless for typical content. Do not use as a primary metric; it badly correlates with perceived quality on its own.

---

## Why target multiple metrics

A single metric can be fooled. The table below shows a real example where `any_vulkan_hevc-10bit` looks excellent by median scores across all four metrics — yet its minimum scores tell a different story:

| Strategy                   | Size (MB) | PSNR med | SSIM med | VMAF med | VIF med |
| -------------------------- | --------- | -------- | -------- | -------- | ------- |
| any_vulkan_hevc-10bit      | 186.6     | 46.9     | 98.8     | 98.7     | 94.9    |
| p7_nvenc-h265-10bit-qp     | 174.7     | 48.1     | 99.0     | 98.4     | 94.6    |
| p7_nvenc-h265-10bit-vbr-cq | 136.3     | 47.5     | 98.8     | 98.2     | 93.8    |
| slow_h265-anime            | 142.2     | 47.5     | 98.8     | 98.5     | 94.3    |
| slow_h265-aq               | 149.9     | 47.6     | 98.8     | 98.6     | 94.4    |
| slow_h265                  | 146.8     | 47.5     | 98.8     | 98.6     | 94.4    |
| veryslow_h264              | 206.8     | 47.4     | 98.9     | 98.5     | 94.3    |

The Vulkan strategy has a VMAF min of 84 and a VIF min of 84. Manual inspection confirmed visible quality problems. Median-only comparison missed them entirely.

**The key insight:** metrics are targeted per-chunk in `pyqenc` (each chunk is a short, relatively uniform scene), so median values are meaningful and stable at that granularity. But still use at least one safeguard statistic (min or p05) alongside the median for each metric to catch outlier chunks.

---

## Which statistics to target

`pyqenc` supports several statistics over the per-frame metric scores within a chunk:

| Statistic | Description | Notes |
|-----------|-------------|-------|
| `min` | Worst single frame | Fragile — a single bad frame (e.g. VMAF frame 0 bias) distorts it. Avoid as a primary target. |
| `p05` | 5th percentile | Robust worst-case. Recommended over `min` for VMAF. |
| `p25` | 25th percentile | Useful for tighter floor control. |
| `med` | Median (50th percentile) | Good general target. Stable per-chunk. Recommended. |
| `p75`, `p95` | Upper percentiles | Rarely needed for targeting; useful for analysis. |
| `max` | Best single frame | Not useful for targeting; useful for analysis. |

Recommended approach: **target `med` as the primary statistic and add a `p05` or `min` as a floor safeguard.** For VMAF specifically, prefer `p05` over `min` due to the first-frame bias.

You do not need to target both `med` and `p05` for the same metric — pick one primary and one safeguard and move on.

---

## Recommended target set

The defaults shipped with `pyqenc` are a starting point, not a universal prescription. The right values depend on your content and how much quality vs size trade-off you want.

```yaml
encoding:
  targets:
    - "vif-med:92.0"    # grain retention; 92–94 is good; higher = crisper, larger file
    - "vif-min:88.0"    # floor safeguard
    - "vmaf-p05:95.0"   # p05 avoids VMAF first-frame bias; 95–97 = high quality
    - "vmaf-min:92.0"   # floor safeguard
    - "psnr-med:45.0"   # 44–46 = good retention; 50+ = near-lossless
    - "psnr-min:42.0"   # floor safeguard
    - "ssim-med:98.0"   # 98+ = good; 99+ = near-lossless
    - "ssim-min:95.0"   # floor safeguard
```

To increase quality (larger files): raise the most constraining passing target (marked `•` in logs) by 0.5–1.0.

To decrease quality (smaller files): lower the most constraining failing target (marked `✘` in logs) by 0.5–1.0.

Tune on a representative sample clip first. Metrics do not linearly map to perceived quality, so small numeric changes can have larger visual effects than expected. As long as intermediate results have not been cleaned up, re-running with adjusted targets only re-encodes the affected chunks.

---

## Content-specific considerations

**Film grain / live action:** VIF is particularly useful here. VMAF will reward over-smoothing of grain; VIF catches it. Keep both.

**Anime / flat areas:** VMAF and SSIM tend to work well. VIF targets can be relaxed (anime has less natural texture). PSNR is a reliable secondary.

**Titles and solid transitions:** VMAF hits its ceiling (~97.5) on these scenes at any bitrate. This is a model limitation — `pyqenc` accepts the best achievable result and moves on. It does not cause problems in practice as these scenes are usually short.

**HDR content:** All metrics run in the encoded colour space. Results are comparable to SDR in practice, but absolute target values may need slight adjustment — HDR encodes can score slightly lower on VMAF at equivalent visual quality.

---

## Narrowing the quality search range

By default the quality search explores the full range declared on the codec — e.g. CRF 6–30 for h.265. For most content that's fine. But you may want tighter control:

- **Avoid extreme values.** CRF 6 on h.265 produces enormous files. Constraining to CRF 12–24 keeps the search focused.
- **Pin to a single CRF.** Setting `min == max` skips quality search entirely — the encoder runs once at that value.
- **Per-content tuning.** Film grain, anime, and clean CGI converge at different CRF bands. A narrowed profile encodes more predictably for a specific type of content.

### Adding a local profile with a range override

Create or edit a `pyqenc.yaml` in your project directory (`pyqenc config . -y` copies the active config as a starting point). Add your profile under `profiles` — only the fields you want to change are needed:

```yaml
profiles:
  h265-tight:                # new profile — needs full profile fields
    codec: h265-10bit
    description: "h.265 with a tighter CRF band"
    extra_args: []
    quality_range: [12.0, 24.0]   # narrows the codec's default [6.0, 30.0]

  h265-anime:                # overriding an existing profile — only add what changes
    quality_range: [14.0, 22.0]
```

Then use it:

```sh
pyqenc auto movie.mkv --strategies h265-tight -y
pyqenc auto movie.mkv --strategies "h265*" -y   # picks up any overridden h265 profiles too
```

### Constraints

- The profile range must be a **subset** of the codec's declared range — only narrowing is allowed. A range that extends beyond the codec's bounds raises a `ValidationError` at startup.
- Direction is preserved: for CRF/CQ/QP codecs `[better, worse]` means `[low_crf, high_crf]`; for VBR codecs it means `[high_bitrate, low_bitrate]`.
- A pinned value (`min == max`) skips quality search but not the pipeline — chunking, resumption, and merge still work normally.
- To affect all profiles that share a codec, override `quality_range` under `codecs:` instead of under `profiles:`. The profile-level override is preferable when you want per-profile control without changing the shared codec definition.
