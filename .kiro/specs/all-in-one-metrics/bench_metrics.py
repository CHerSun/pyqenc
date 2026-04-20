"""Benchmark PSNR, SSIM, VMAF (with VIF) at subsample factors 1, 3, 10.

Calls run_metric() directly so the exact same ffmpeg commands as production are used.
"""
import asyncio
import time
from pathlib import Path

from pyqenc.models import CropParams
from pyqenc.quality import MetricType, run_metric

REF  = Path(r"D:\_current\pyqenc1\measure\(1).mkv")
DIST = Path(r"D:\_current\pyqenc1\measure\(1.1) any_vulkan_hevc-10bit.mkv")
CWD  = Path(r"D:\_current\pyqenc1\measure")

CROP_REF  = CropParams(top=22, bottom=22, left=0, right=0)
CROP_DIST = CropParams(top=0,  bottom=0,  left=0, right=0)
FACTORS   = [1, 3, 10]
METRICS = [MetricType.PSNR, MetricType.SSIM, MetricType.VMAF]


async def bench_one(metric: MetricType, factor: int) -> dict:
    prefix = f"bench_{metric.value}_f{factor}_"
    print(f"  running {metric.value} factor={factor} ...", flush=True)
    t0 = time.perf_counter()
    result = await run_metric(
        metric         = metric,
        distorted      = DIST,
        reference      = REF,
        crop_distorted = CROP_DIST,
        crop_reference = CROP_REF,
        duration       = 0,
        width          = 0,
        use_gpu        = False,
        subsample      = factor,
        output_prefix  = prefix,
        cwd            = CWD,
        output_extension = ".tmp",
    )
    elapsed = time.perf_counter() - t0
    frames  = result.frame_count or 0
    fps     = frames / elapsed if frames else None
    status  = "OK" if result.success else f"ERR(rc={result.returncode})"
    print(f"  {status}  elapsed={elapsed:.1f}s  frames={frames}  fps={f'{fps:.2f}' if fps else 'N/A'}")
    if not result.success:
        for ln in (result.stderr_lines or [])[-5:]:
            print(f"    {ln}")
    return {
        "metric":    metric.value,
        "factor":    factor,
        "elapsed_s": round(elapsed, 1),
        "frames":    frames,
        "fps":       round(fps, 2) if fps else None,
        "ok":        result.success,
    }


async def main() -> None:
    results = []
    for factor in FACTORS:
        print(f"\n=== factor={factor} ===")
        for metric in METRICS:
            results.append(await bench_one(metric, factor))

    print("\n\n" + "=" * 64)
    print(f"{'Metric':<12} {'Factor':>6} {'Elapsed(s)':>10} {'Frames':>8} {'FPS':>8}")
    print("-" * 64)
    for r in results:
        fps_s = f"{r['fps']:.2f}" if r["fps"] is not None else "?"
        print(f"{r['metric']:<12} {r['factor']:>6} {r['elapsed_s']:>10.1f} {r['frames']:>8} {fps_s:>8}")


asyncio.run(main())
