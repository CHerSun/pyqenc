"""Test: all 4 metrics (PSNR, SSIM, VMAF+VIF) in a single ffmpeg pass.

PSNR and SSIM always get every frame (no select).
VMAF uses n_subsample=factor internally.
Compare against 3 separate passes at the same factor.
"""
import asyncio
import time
from pathlib import Path

from pyqenc.models import CropParams
from pyqenc.quality import MetricType, run_metric
from pyqenc.utils.ffmpeg_runner import run_ffmpeg_async

REF       = Path(r"D:\_encoding\pyqenc\measure\(1).mkv")
DIST      = Path(r"D:\_encoding\pyqenc\measure\(1.1) any_vulkan_hevc-10bit.mkv")
CWD       = Path(r"D:\_encoding\pyqenc\measure")
CROP_REF  = CropParams(top=22, bottom=22, left=0, right=0)
CROP_DIST = CropParams(top=0,  bottom=0,  left=0, right=0)
FACTORS   = [1, 3, 10]


async def bench_separate(factor: int) -> float:
    """Three separate ffmpeg runs: PSNR, SSIM, VMAF (each with factor)."""
    t0 = time.perf_counter()
    for metric in [MetricType.PSNR, MetricType.SSIM, MetricType.VMAF]:
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
            output_prefix    = f"bench_aio_sep_f{factor}_",
            cwd              = CWD,
            output_extension = ".tmp",
        )
    return time.perf_counter() - t0


async def bench_all_in_one(factor: int) -> float:
    """Single ffmpeg pass: PSNR+SSIM on every frame, VMAF with n_subsample=factor."""
    crop_r   = CROP_REF.to_ffmpeg_filter()
    vmaf_sub = f"n_subsample={factor}:" if factor > 1 else ""

    # Split distorted into 3, reference into 3
    fd = f"[0:v]setpts=PTS-STARTPTS,split=3[main1][main2][main3]"
    fr = f"[1:v]{crop_r},setpts=PTS-STARTPTS,split=3[ref1][ref2][ref3]"

    psnr = f"[main1][ref1]psnr=stats_file=bench_aio_f{factor}_psnr.tmp"
    ssim = f"[main2][ref2]ssim=stats_file=bench_aio_f{factor}_ssim.tmp"
    vmaf = (
        f"[main3][ref3]libvmaf=n_threads=4:{vmaf_sub}"
        f"log_path=bench_aio_f{factor}_vmaf.tmp:log_fmt=json:feature=name=vif"
    )
    fc = f"{fd};{fr};{psnr};{ssim};{vmaf}"

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
        print(f"  ALL-IN-ONE f={factor} FAILED (rc={result.returncode})")
        for ln in (result.stderr_lines or [])[-8:]:
            print(f"    {ln}")
    return elapsed


async def bench_all_in_one_with_select(factor: int) -> float:
    """Single ffmpeg pass: PSNR+SSIM with select filter (factor), VMAF with n_subsample=factor."""
    crop_r   = CROP_REF.to_ffmpeg_filter()
    vmaf_sub = f"n_subsample={factor}:" if factor > 1 else ""

    if factor > 1:
        sel = f"select='not(mod(n,{factor}))',setpts=PTS-STARTPTS"
        fd = (
            f"[0:v]split=3[d1][d2][d3];"
            f"[d1]{sel}[main1];"
            f"[d2]{sel}[main2];"
            f"[d3]setpts=PTS-STARTPTS[main3]"
        )
        fr = (
            f"[1:v]{crop_r},split=3[r1][r2][r3];"
            f"[r1]{sel}[ref1];"
            f"[r2]{sel}[ref2];"
            f"[r3]setpts=PTS-STARTPTS[ref3]"
        )
    else:
        fd = f"[0:v]setpts=PTS-STARTPTS,split=3[main1][main2][main3]"
        fr = f"[1:v]{crop_r},setpts=PTS-STARTPTS,split=3[ref1][ref2][ref3]"

    psnr = f"[main1][ref1]psnr=stats_file=bench_aio_sel_f{factor}_psnr.tmp"
    ssim = f"[main2][ref2]ssim=stats_file=bench_aio_sel_f{factor}_ssim.tmp"
    vmaf = (
        f"[main3][ref3]libvmaf=n_threads=4:{vmaf_sub}"
        f"log_path=bench_aio_sel_f{factor}_vmaf.tmp:log_fmt=json:feature=name=vif"
    )
    fc = f"{fd};{fr};{psnr};{ssim};{vmaf}"

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
        print(f"  ALL-IN-ONE+SELECT f={factor} FAILED (rc={result.returncode})")
        for ln in (result.stderr_lines or [])[-8:]:
            print(f"    {ln}")
    return elapsed


async def main() -> None:
    print(f"{'Run':<26} {'Factor':>6} {'Elapsed(s)':>10}")
    print("-" * 46)

    for factor in FACTORS:
        print(f"\n  [separate 3×] f={factor} ...", flush=True)
        t_sep = await bench_separate(factor)
        print(f"{'separate PSNR+SSIM+VMAF':<26} {factor:>6} {t_sep:>10.1f}")

        print(f"  [all-in-one]  f={factor} ...", flush=True)
        t_aio = await bench_all_in_one(factor)
        print(f"{'all-in-one (VMAF×factor)':<26} {factor:>6} {t_aio:>10.1f}")

        saving = (t_sep - t_aio) / t_sep * 100
        print(f"  → saving vs separate: {t_sep - t_aio:.1f}s ({saving:.0f}%)")

        print(f"  [all-in-one+select] f={factor} ...", flush=True)
        t_sel = await bench_all_in_one_with_select(factor)
        print(f"{'all-in-one+select':<26} {factor:>6} {t_sel:>10.1f}")

        saving2 = (t_sep - t_sel) / t_sep * 100
        print(f"  → saving vs separate: {t_sep - t_sel:.1f}s ({saving2:.0f}%)")


asyncio.run(main())
