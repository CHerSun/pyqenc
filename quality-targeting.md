# A note on quality targeting

## VMAF and min

VMAF specifically looks to be sensitive to the very first frame on each chunk. My best guess is it lacks the motion part there.

Due to this ==vmaf-min could be unreliable==. Prefer vmaf-p05 over vmaf-min.

## Median values

Most other tools target only the median metric values. Those are not too reliable, especially for longer videos.

## Single metric targetting

Some tools target one metric, like VMAF median. VMAF is quite a biased metric. My belief is that it's wrong to target single metric, it won't give you proper quality control.

## Summary table by median metrics

See the table below. `any_vulkan_hevc-10bit` has awesome VMAF med score of 98.7:

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

But if you'd take a look at its VMAF min - it's 84 (ugly really). VIF min is also 84 (ugly too). It is one of the worse results in visual examination too.

## Recommended way

To get stable results you should target multiple metrics. Considering we use chunk-based encoding (short, controlled scenes) - a single stat for several metrics should be enough, i.e. you shouldn't target min + med together, rather pick either min or med. But for multiple metrics, i.e. VMAF alone is not enough.

A separate note on VIF. VIF is part of VMAF metric actually. Yet, VMAF is too biased to smoothed out results. If you want to retain crisp / noisy look of the original - I strongly advise to add VIF measure separately.

Ny set is something like:

- vmaf-p05=96.5
- vif-min=92
- psnr-med=46
- ssim-med=99

I'm pursuing nearly visually lossless results. Those could be too high for you, if you want smaller result size.

> This is the area of personal preference really, just measure a few results that you like to see the measured stats.
