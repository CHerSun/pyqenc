"""Test hypothesis: combined PSNR+SSIM in a single ffmpeg pass vs two separate passes.

Uses split[] to fan out decoded frames to both filters simultaneously.
"""
import asyncio
import time
from pathlib import Path

from pyqenc.models import CropParams
from pyqenc.quality import MetricType, run_metric
from pyqenc.utils.ffmpeg_runner import run_ffmpeg_async

REF       = Path(r"D:\_current\pyqenc1\measure\(1).mkv")
DIST      = Path(r"D:\_current\pyqenc1\measure\(1.1) any_vulkan_hevc-10bit.mkv")
CWD       = Path(r"D:\_current\pyqenc1\measure")
CROP_REF  = CropParams(top=22, bottom=22, left=0, right=0)
CROP_DIST = CropParams(top=0,  bottom=0,  left=0, right=0)
FACTORS   = [1, 10]


async def bench_separate(factor: int) -> float:
    """Two separate ffmpeg runs: PSNR then SSIM."""
    t0 = time.perf_counter()
    for metric in [MetricType.PSNR, MetricType.SSIM]:
        await run_metric(
            metric           = metric,
            distorted        = DIST,
            reference        = REF,
            crop_distorted   = CROP_DIST,
            crop_reference   = CROP_REF,
            duration         = 0,
            width            = 0,
            use_gpu          = False,
            subsample        = factor,
            output_prefix    = f"bench_sep_f{factor}_",
            cwd              = CWD,
            output_extension = ".tmp",
        )
    return time.perf_counter() - t0


async def bench_combined(factor: int) -> float:
    """Single ffmpeg run computing PSNR and SSIM simultaneously via split[]."""
    crop_r = CROP_REF.to_ffmpeg_filter()

    if factor > 1:
        sel = f"select='not(mod(n,{factor}))',setpts=PTS-STARTPTS"
        fd = f"[0:v]{sel},split=2[main1][main2]"
        fr = f"[1:v]{crop_r},{sel},split=2[ref1][ref2]"
    else:
        fd = f"[0:v]setpts=PTS-STARTPTS,split=2[main1][main2]"
        fr = f"[1:v]{crop_r},setpts=PTS-STARTPTS,split=2[ref1][ref2]"

    psnr = f"[main1][ref1]psnr=stats_file=bench_comb_f{factor}_psnr.tmp"
    ssim = f"[main2][ref2]ssim=stats_file=bench_comb_f{factor}_ssim.tmp"
    fc   = f"{fd};{fr};{psnr};{ssim}"

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-progress", "pipe:1",
        "-i", str(DIST.resolve()),
        "-i", str(REF.resolve()),
        "-filter_complex", fc,
        "-f", "null", "-",
    ]

    t0     = time.perf_counter()
    result = await run_ffmpeg_async(cmd, output_file=None, cwd=CWD)
    elapsed = time.perf_counter() - t0

    if not result.success:
        print(f"  COMBINED f={factor} FAILED (rc={result.returncode})")
        for ln in (result.stderr_lines or [])[-5:]:
            print(f"    {ln}")
    return elapsed


async def main() -> None:
    print(f"{'Run':<30} {'Factor':>6} {'Elapsed(s)':>10}")
    print("-" * 50)

    for factor in FACTORS:
        print(f"  separate PSNR+SSIM f={factor} ...", flush=True)
        t_sep = await bench_separate(factor)
        print(f"{'separate PSNR+SSIM':<30} {factor:>6} {t_sep:>10.1f}")

        print(f"  combined PSNR+SSIM f={factor} ...", flush=True)
        t_comb = await bench_combined(factor)
        print(f"{'combined PSNR+SSIM':<30} {factor:>6} {t_comb:>10.1f}")

        saving = (t_sep - t_comb) / t_sep * 100
        print(f"  → saving: {t_sep - t_comb:.1f}s ({saving:.0f}%)\n")


asyncio.run(main())
