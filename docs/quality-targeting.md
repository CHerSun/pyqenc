# A note on quality targeting

<!-- markdownlint-disable MD024 -->

## VMAF problems

VMAF is a good metric and often targeted as a key metric. Yet, it has its own set of problems.

### VMAF is biased

VMAF strongly favors smoothed contents. If you want to preserve the original film noise - it is not the best metric to measure that.

RECOMMENDED: do not single-target `vmaf-*` if you care about noise preservation.

### VMAF min

VMAF looks to be sensitive on the very first frame. My best guess is it lacks the motion part there. See the first point of [plot](../samples/VMAF%20on%20the%20first%20frame.png). Everywhere VMAF takes values of 100 - a huge overshoot of quality and a waste of space. The first frame is a clear outlier, yet it defines the min, so targeting `vmaf-min` becomes unreliable.

RECOMMENDED: do not target `vmaf-min`, use `vmaf-p05` instead.

### VMAF and high median problem

Normally, VMAF median of 100 is perfectly reachable. VMAF median of 98+ gives visually nearly lossless quality. But for some types of contents, like, titles, VMAF median NEVER goes above ~97.5 for ordinary codecs. See [plot](../samples/VMAF%20median%20-%20unreachable%20high%20score.png). Other metrics could take incredibly high values there, showing basically lossless quality, but not VMAF.

It is difficult to recommend any solution here. `pyqenc` will try to choose the most suitable variant if it can't reach the requested target value. Often, those are just titles or single scenes, which shouldn't affect the overall video size. One thing you can do is limit the codec quality range in the config (e.g. `quality_range: [10, 26]` instead of the full `[0, 51]`) to get a more sane range applied to your video (this is in the default config already).

## Median values and single metric targetting

This is my opinionated belief - median values are unreliable when measuring over a long video, so do not compare videos by this alone - watch also min / p05 or stddev values. Also, always use multiple metrics, only in combination they truly allow to reach stable measuring results.

Median values are rather good targets for encoding with `pyqenc` though, as they are targeted per-chunk = short uniform scenes.

### Sample summary table by median metrics

See a sample table below. It nicely shows the problem of single-metric single-stat comparison. If we taka a look at `any_vulkan_hevc-10bit` - it has awesome stats really. VMAF med score of 98.7, PSNR med of 46.9, SSIM med of 98.8, even VIF med is at 94.9 level:

| Target                     | Size (MB) | PSNR med | SSIM med | VMAF med | VIF med |
| -------------------------- | --------- | -------- | -------- | -------- | ------- |
| any_vulkan_hevc-10bit      | 186.6     | 46.9     | 98.8     | 98.7     | 94.9    |
| p7_nvenc-h265-10bit-qp     | 174.7     | 48.1     | 99.0     | 98.4     | 94.6    |
| p7_nvenc-h265-10bit-vbr-cq | 136.3     | 47.5     | 98.8     | 98.2     | 93.8    |
| p7_nvenc-h265-10bit-vbr-mu | 169.6     | 48.0     | 98.9     | 98.4     | 94.4    |
| slow_h265-anime            | 142.2     | 47.5     | 98.8     | 98.5     | 94.3    |
| slow_h265-aq               | 149.9     | 47.6     | 98.8     | 98.6     | 94.4    |
| slow_h265                  | 146.8     | 47.5     | 98.8     | 98.6     | 94.4    |
| veryslow_h264              | 206.8     | 47.4     | 98.9     | 98.5     | 94.3    |

But if we'd take a look at its minimum scores (not in the table), its VMAF min is 84 - ugly really. Its VIF min is also 84 - ugly too. And manual visual inspection showed really bad results too.

## Recommended way

To get stable results you should target multiple metrics. Considering we use chunk-based encoding (short, controlled scenes) - a single stat per metric for several metrics should be enough, i.e. you shouldn't need to target min + med together, rather pick either min or med. And target multiple metrics, i.e. VMAF alone is not enough, also target other metrics.

> A separate note on VIF. VIF is part of VMAF metric actually. Yet, VMAF is too biased to smoothed out results. If you want to retain crisp / noisy look of the original - add VIF measure separately.

My set is something like:

```yaml
- "vif-med:92"    # vif is stable, could target min or med finely. 92 looks to be a good value to add extra control on retention of film grain.
- "vmaf-p05:95"   # see the vmaf-min notice - prefer p05 over min. VMAF is the main metric controlling perceived quality.
- "psnr-med:45"   # psnr is stable, could target min or med finely. 45+ should give quite good value.
- "ssim-med:98"   # ssim is stable, could target min or med finely. SSIM is considered to not be a good measure of perceived quality. The metric itself is very compressed around 98-100.0 range, small changes there matter. 98 feels to be ok as an extra control.
```

I'm pursuing nearly visually lossless results. These values could be too high for you, if you prefer smaller resulting size.

> This is the area of personal preference really, just measure a few results that you like to see the measured stats.
