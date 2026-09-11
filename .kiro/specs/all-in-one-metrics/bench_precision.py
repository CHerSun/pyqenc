"""Precision analysis: how well do subsampled stats represent the full-frame stats.

The per-frame values at factor=3/10 are identical to the corresponding frames
in factor=1 (select just picks the same frames). So per-frame error is always 0.

The meaningful question is: how well does the subsampled *distribution* represent
the full distribution? We compare the key statistics (min, p05, p25, med, p75,
p95, max, std) computed from subsampled frames against those from all frames.

Uses bench_aio_sel_f1 as ground truth (all frames, with select filter).
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from pyqenc.utils.visualization import (
    parse_psnr_file,
    parse_ssim_file,
    parse_vmaf_file,
    parse_vif_file,
)
from pyqenc.quality import MetricType

CWD        = Path(r"D:\_encoding\pyqenc\measure")
VIF_SOURCE = MetricType.VMAF
FACTORS    = [1, 3, 10]
STAT_KEYS  = ["min", "p05", "p25", "med", "p75", "p95", "max", "std"]


def load(metric: MetricType, factor: int) -> pd.Series:
    if metric == MetricType.VIF:
        path = CWD / f"bench_aio_sel_f{factor}_{VIF_SOURCE.value}.tmp"
        df   = parse_vif_file(path, factor=factor)
    else:
        parsers = {
            MetricType.PSNR: parse_psnr_file,
            MetricType.SSIM: parse_ssim_file,
            MetricType.VMAF: parse_vmaf_file,
        }
        path = CWD / f"bench_aio_sel_f{factor}_{metric.value}.tmp"
        df   = parsers[metric](path, factor=factor)
    return df[metric.value]


def compute_stats(s: pd.Series) -> dict[str, float]:
    q = s.quantile([0.05, 0.25, 0.50, 0.75, 0.95])
    return {
        "min": float(s.min()),
        "p05": float(q[0.05]),
        "p25": float(q[0.25]),
        "med": float(q[0.50]),
        "p75": float(q[0.75]),
        "p95": float(q[0.95]),
        "max": float(s.max()),
        "std": float(s.std()),
    }


# Load baselines (factor=1 = all frames)
print("Loading factor=1 baselines...")
baseline_series: dict[MetricType, pd.Series] = {}
baseline_stats:  dict[MetricType, dict[str, float]] = {}
for metric in MetricType:
    try:
        s = load(metric, 1)
        baseline_series[metric] = s
        baseline_stats[metric]  = compute_stats(s)
        print(f"  {metric.value}: {len(s)} frames")
    except Exception as e:
        print(f"  {metric.value}: FAILED — {e}")
print()

# Print absolute stats for all 3 factors first
for factor in FACTORS:
    print(f"{'='*80}")
    print(f"Factor = {factor}  —  absolute stats (normalized 0-100 scale)")
    print(f"{'='*80}")
    hdr = f"{'Metric':<8} {'Frames':>7}  " + "  ".join(f"{k:>7}" for k in STAT_KEYS)
    print(hdr)
    print("-" * 80)
    for metric in MetricType:
        if metric not in baseline_stats:
            continue
        try:
            s  = load(metric, factor)
            sn = metric.info.normalize(s)
            st = compute_stats(sn)
            row = f"{metric.value:<8} {len(s):>7}  " + "  ".join(f"{st[k]:>7.2f}" for k in STAT_KEYS)
            print(row)
        except Exception as e:
            print(f"{metric.value:<8} FAILED: {e}")
    print()

# Then diffs vs factor=1
for factor in [3, 10]:
    print(f"{'='*80}")
    print(f"Factor = {factor}  —  stat deviation from factor=1 (absolute difference, normalized scale)")
    print(f"{'='*80}")
    hdr = f"{'Metric':<8} {'Frames':>7}  " + "  ".join(f"{k:>7}" for k in STAT_KEYS)
    print(hdr)
    print("-" * 80)

    for metric in MetricType:
        if metric not in baseline_stats:
            continue
        try:
            s      = load(metric, factor)
            sn     = metric.info.normalize(s)
            s_stat = compute_stats(sn)
            b_stat = compute_stats(metric.info.normalize(baseline_series[metric]))
            diffs  = {k: abs(s_stat[k] - b_stat[k]) for k in STAT_KEYS}
            row    = f"{metric.value:<8} {len(s):>7}  " + "  ".join(f"{diffs[k]:>7.4f}" for k in STAT_KEYS)
            print(row)
        except Exception as e:
            print(f"{metric.value:<8} FAILED: {e}")
    print()
