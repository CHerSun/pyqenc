# Sample Comparison: pyqenc vs BSEncode

<!-- markdownlint-disable MD024 -->

I've given the same sample to BSEncode and pyqenc.

Here's the BSEncode measured metrics plot:

![BSEncode measured metrics plot](../samples/bsencode_encode_plot.png)

Here's the pyqenc plot:

![pyqenc measured metrics plot](../samples/pyqenc_encode_plot.png)

They don't really look different at first glance, but pyqenc plot is more uniform in terms of lower quality.

If we look at stats distribution:

| Metric | Stat    | pyqenc      | BSEncode |
| ------ | ------- | ----------- | -------- |
| CRF    | range   | 17.5...24.0 | 19.2     |
| VMAF   | median  | 97.4        | 97.8     |
| VMAF   | min     | 94.0        | 91.8     |
| SSIM   | median  | 98.5        | 98.7     |
| SSIM   | min     | 95.6        | 96.1     |
| PSNR   | median  | 45.8        | 46.5     |
| PSNR   | min     | 42.0        | 41.4     |
| Size   | total   | 56 MB       | 80 MB    |

> NOTE: I tried my best to reach as similar as possible median values — what BSEncode targets, but it uses not too precise measuring by default.

Most metrics are very close. For min values pyqenc usually outperforms a full encode with BSEncode — as per given targets for min values too — this could be easily controlled separately and made higher.

The main result is the total size (measured for video stream alone): **56 MB vs 80 MB** - 30% saved bits.

This is achieved thanks to variable quality value selection per scene — frames within a scene are rather uniform, so a single quality value there provides consistent quality.

![pyqenc quality value plot](../samples/pyqenc_encode_crf.png)
