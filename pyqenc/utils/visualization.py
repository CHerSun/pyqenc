"""
Unified visualization and quality metrics analysis for video encoding pipeline.

Consolidates metric parsing, statistics computation, and plot generation
from the legacy metrics_visualization module.
"""
# CHerSun 2026

import asyncio
import json
import logging
import os
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import matplotlib

from pyqenc.constants import TIME_SEPARATOR_MS, TIME_SEPARATOR_SAFE
from pyqenc.models import CropParams, QualityTarget
from pyqenc.quality import (
    ChunkQualityStats,
    MetricData,
    MetricStats,
    MetricType,
    QualityArtifacts,
    QualityEvaluation,
    _MetricStatistics,
    normalize_metric,
    run_metric,
)
from pyqenc.utils.alive import ProgressBar

matplotlib.use("Agg")  # non-interactive backend — safe to call from any thread
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

# Suppress noisy matplotlib debug/info chatter
logging.getLogger("matplotlib").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KEY_FRAME_NUM: str = "frameNum"

# Font sizes
_FONT_TITLE:         int = 14
_FONT_AXIS_LABEL:    int = 12
_FONT_AXIS_TICKS:    int = 10
_FONT_AXIS_TICKS_X:  int = 8
_FONT_LEGEND:        int = 10
_FONT_SUMMARY_BOX:   int = 9
_FONT_BAR_LABEL:     int = 8
_FONT_SUBPLOT_TITLE: int = 10
_FONT_SUBPLOT_XLABEL: int = 10

# Figure layout
_FIG_WIDTH:               int   = 14
_FIG_HEIGHT:              int   = 10
_MAIN_PLOT_HEIGHT_RATIO:  int   = 3
_STATS_PLOT_HEIGHT_RATIO: int   = 1
_GRID_HSPACE:             float = 0.3
_GRID_WSPACE:             float = 0.3

# Y-axis ranges
_PSNR_Y_MIN:        int = 0
_PSNR_Y_MAX:        int = 103
_PSNR_Y_MAJOR_TICK: int = 10
_PSNR_Y_MINOR_TICK: int = 2
_PCT_Y_MIN:         int = 0
_PCT_Y_MAX:         int = 103
_PCT_Y_MAJOR_TICK:  int = 10
_PCT_Y_MINOR_TICK:  int = 2

# Line / smoothing
_LINE_WIDTH_DEFAULT:  float = 0.8
_LINE_ALPHA:          float = 0.7
_TARGET_PLOT_POINTS:  int   = 200   # desired number of display points on the main line
_MIN_POINTS_PER_BIN:  int   = 3     # minimum raw points per bin to enable aggregation
_RANGE_ALPHA:         float = 0.15

# X-axis ticks
_X_MAJOR_TICKS:   int   = 20
_X_MINOR_TICKS:   int   = 100
_X_PADDING_RATIO: float = 0.003

# Summary boxes
_SUMMARY_BOX_Y_POS:       float = 0.02
_SUMMARY_BOX_WIDTH:       float = 0.18
_SUMMARY_BOX_SPACING:     float = 0.02
_SUMMARY_BOX_START_X:     float = 0.05
_SUMMARY_BOX_ALPHA:       float = 0.8
_SUMMARY_BOX_METRIC_ALPHA: float = 0.3
_SUMMARY_BOX_ZORDER:      int   = 10

# Bar chart
_BAR_HEIGHT: float = 0.7
_BAR_ALPHA:  float = 0.8

# Misc
_PLOT_DPI:        int   = 200
_GRID_ALPHA_MAJOR: float = 0.3
_GRID_ALPHA_MINOR: float = 0.1
_LEGEND_ALPHA:    float = 0.9
_MARKER_SIZE:     int   = 10
_MARKER_ALPHA:    float = 0.9


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_psnr_line(line: str) -> dict[str, float] | None:
    """Parse one line from a PSNR log file."""
    try:
        parts  = line.split()
        parsed: dict[str, int | float] = {}
        for part in parts:
            if ":" in part:
                key, value = part.split(":", 1)
                parsed[key] = int(value) if key == "n" else float(value)
        if "n" in parsed and "psnr_avg" in parsed:
            return {"n": parsed["n"], MetricType.PSNR.value: parsed["psnr_avg"]}
        return None
    except (ValueError, IndexError):
        return None


def _parse_ssim_line(line: str) -> dict[str, float] | None:
    """Parse one line from an SSIM log file."""
    try:
        parts  = line.split()
        parsed: dict[str, int | float] = {}
        for part in parts:
            if ":" in part:
                key, value = part.split(":", 1)
                parsed[key] = int(value) if key == "n" else float(value)
        if "n" in parsed and "All" in parsed:
            return {"n": parsed["n"], MetricType.SSIM.value: parsed["All"]}
        return None
    except (ValueError, IndexError):
        return None


def parse_psnr_file(file_path: Path, factor: int = 1) -> pd.DataFrame:
    """Parse a PSNR log file into a frame-indexed DataFrame.

    PSNR logs count from n=1, so ``frameNum = (n - 1) * factor``.

    Args:
        file_path: Path to the PSNR log file.
        factor:    Frame sampling factor used during metric generation.

    Returns:
        DataFrame indexed by ``frameNum`` with a single ``psnr`` column.

    Raises:
        ValueError: If the file is not a valid PSNR log.
    """
    data: list[dict[str, float]] = []
    is_psnr = False
    with file_path.open("r") as fh:
        for line in fh:
            if parsed := _parse_psnr_line(line):
                data.append({
                    _KEY_FRAME_NUM:      (parsed["n"] - 1) * factor,
                    MetricType.PSNR.value: parsed[MetricType.PSNR.value],
                })
                is_psnr = True
            elif not is_psnr:
                break
    if not data:
        raise ValueError(f"Not a PSNR log file: {file_path}")
    df = pd.DataFrame(data)
    df.set_index(_KEY_FRAME_NUM, inplace=True)
    return df


def parse_ssim_file(file_path: Path, factor: int = 1) -> pd.DataFrame:
    """Parse an SSIM log file into a frame-indexed DataFrame.

    SSIM logs count from n=1, so ``frameNum = (n - 1) * factor``.

    Args:
        file_path: Path to the SSIM log file.
        factor:    Frame sampling factor used during metric generation.

    Returns:
        DataFrame indexed by ``frameNum`` with a single ``ssim`` column.

    Raises:
        ValueError: If the file is not a valid SSIM log.
    """
    data: list[dict[str, float]] = []
    is_ssim = False
    with file_path.open("r") as fh:
        for line in fh:
            if parsed := _parse_ssim_line(line):
                data.append({
                    _KEY_FRAME_NUM:      (parsed["n"] - 1) * factor,
                    MetricType.SSIM.value: parsed[MetricType.SSIM.value],
                })
                is_ssim = True
            elif not is_ssim:
                break
    if not data:
        raise ValueError(f"Not a SSIM log file: {file_path}")
    df = pd.DataFrame(data)
    df.set_index(_KEY_FRAME_NUM, inplace=True)
    return df


def parse_vmaf_file(file_path: Path, factor: int = 1) -> pd.DataFrame:
    """Parse a VMAF JSON file into a frame-indexed DataFrame.

    VMAF reports actual frame numbers starting from 0; no alignment adjustment
    is needed.  The ``factor`` parameter is accepted for API consistency.

    Args:
        file_path: Path to the VMAF JSON file.
        factor:    Frame sampling factor (accepted for consistency, unused).

    Returns:
        DataFrame indexed by ``frameNum`` with a single ``vmaf`` column.

    Raises:
        ValueError: If the file is not a valid VMAF JSON.
    """
    try:
        with file_path.open("r") as fh:
            vmaf_data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in VMAF file {file_path}: {exc}") from exc

    frames = vmaf_data.get("frames")
    if not isinstance(frames, list):
        raise ValueError(f"VMAF 'frames' is not an array: {file_path}")
    if not frames:
        raise ValueError(f"VMAF 'frames' array is empty: {file_path}")

    data: list[dict[str, float | int]] = []
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError(f"VMAF frame is not a dictionary: {file_path}")
        n    = frame.get("frameNum")
        vmaf = frame.get("metrics", {}).get("vmaf")
        if n is not None and vmaf is not None:
            data.append({_KEY_FRAME_NUM: n, MetricType.VMAF.value: vmaf})
        else:
            raise ValueError(f"NOT a VMAF file: {file_path}")

    df = pd.DataFrame(data)
    df.set_index(_KEY_FRAME_NUM, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_statistics(
    values:          pd.Series,
    std_cutoff_max:  float | None = None,
    std_cutoff_min:  float | None = None,
) -> _MetricStatistics:
    """Compute quantile-based statistics for any numeric metric series.

    Args:
        values:         Series of metric values.
        std_cutoff_max: Exclude values above this threshold from std calculation.
        std_cutoff_min: Exclude values below this threshold from std calculation.

    Returns:
        Dictionary with ``min``, ``p5``–``p95``, ``max``, and ``std``.
    """
    levels = [0.00, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
    keys   = ["min", "p5", "p10", "p25", "p50", "p75", "p90", "p95", "max", "std"]

    clipped = values.clip(upper=100.0)
    stats: list[float] = list(clipped.quantile(levels))
    stats.append(clipped.max())

    std_values = clipped
    if std_cutoff_max is not None:
        std_values = std_values[std_values <= std_cutoff_max]
    if std_cutoff_min is not None:
        std_values = std_values[std_values >= std_cutoff_min]
    stats.append(std_values.std())

    return dict(zip(keys, stats))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Visual style
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricVisualStyle:
    """Visual styling configuration for a single metric type."""

    label:              str
    color:              str
    unit:               str
    y_axis:             Literal["left", "right"]
    linestyle:          str
    linewidth:          float
    lossless_threshold: float | None
    lossless_label:     str


DEFAULT_METRIC_STYLES: dict[MetricType, MetricVisualStyle] = {
    MetricType.PSNR: MetricVisualStyle(
        label="PSNR",
        color="blue",
        unit=" dB",
        y_axis="left",
        linestyle="-",
        linewidth=_LINE_WIDTH_DEFAULT,
        lossless_threshold=None,
        lossless_label="∞ dB",
    ),
    MetricType.SSIM: MetricVisualStyle(
        label="SSIM",
        color="green",
        unit="%",
        y_axis="right",
        linestyle="-",
        linewidth=_LINE_WIDTH_DEFAULT,
        lossless_threshold=100.0,
        lossless_label="100.0",
    ),
    MetricType.VMAF: MetricVisualStyle(
        label="VMAF",
        color="#CC6600",
        unit="%",
        y_axis="right",
        linestyle="-",
        linewidth=_LINE_WIDTH_DEFAULT,
        lossless_threshold=100.0,
        lossless_label="100.0",
    ),
}


# ---------------------------------------------------------------------------
# Plot creation
# ---------------------------------------------------------------------------

def create_unified_plot(
    metrics:             dict[MetricType, MetricData],
    factor:              int,
    output_path:         Path,
    title:               str                                  = "Video Quality Metrics Analysis",
    styles:              dict[MetricType, MetricVisualStyle] | None = None,
    fps:                 float | None                         = None,
    chunk_start_seconds: float                               = 0.0,
) -> dict[MetricType, _MetricStatistics]:
    """Create a unified quality-metrics plot and save it to disk.

    Generates a figure with:
    - Main plot area with dual Y-axes (PSNR left, SSIM/VMAF right).
    - Horizontal summary boxes at the bottom of the main plot.
    - Per-metric statistics bar subplots below the main plot.

    Args:
        metrics:             Mapping of ``MetricType`` to ``MetricData``.
        factor:              Frame sampling factor used during metric generation.
        output_path:         Destination path for the saved PNG.
        title:               Plot title.
        styles:              Optional custom visual styles; falls back to
                             ``DEFAULT_METRIC_STYLES`` for any missing key.
        fps:                 Frames per second of the encoded video.  When provided,
                             x-axis tick labels show ``HH:MM:SS`` on the top line and
                             the adjusted frame number on the bottom line.  When
                             ``None``, raw frame numbers are shown as before.
        chunk_start_seconds: Start timestamp of the chunk in seconds.  When non-zero,
                             the x-axis frame numbers are offset so they reflect the
                             actual position in the source video rather than starting
                             from 0.

    Returns:
        Mapping of ``MetricType`` to full ``_MetricStatistics``.

    Raises:
        ValueError: If ``metrics`` is empty.
    """
    if not metrics:
        raise ValueError("No valid metrics provided for visualization")

    # Merge with defaults
    effective_styles: dict[MetricType, MetricVisualStyle] = dict(DEFAULT_METRIC_STYLES)
    if styles:
        effective_styles.update(styles)

    plt.style.use("seaborn-v0_8")
    plt.rcParams["axes.grid"] = False

    n_metrics = len(metrics)
    fig = plt.figure(figsize=(_FIG_WIDTH, _FIG_HEIGHT))
    gs  = fig.add_gridspec(
        2, n_metrics,
        height_ratios=[_MAIN_PLOT_HEIGHT_RATIO, _STATS_PLOT_HEIGHT_RATIO],
        hspace=_GRID_HSPACE,
        wspace=_GRID_WSPACE,
    )

    ax_main = fig.add_subplot(gs[0, :])
    ax_main.set_axisbelow(True)

    has_psnr:               bool = MetricType.PSNR in metrics
    has_ssim:               bool = MetricType.SSIM in metrics
    has_vmaf:               bool = MetricType.VMAF in metrics
    has_percentage_metrics: bool = has_ssim or has_vmaf

    ax_left:  plt.Axes | None = None
    ax_right: plt.Axes | None = None

    def _configure_psnr_axis(ax: plt.Axes) -> None:
        style = effective_styles[MetricType.PSNR]
        ax.set_ylabel("PSNR (dB)", color=style.color, fontsize=_FONT_AXIS_LABEL, fontweight="bold")
        ax.set_ylim(_PSNR_Y_MIN, _PSNR_Y_MAX)
        ax.tick_params(axis="y", labelcolor=style.color, labelsize=_FONT_AXIS_TICKS)
        ax.yaxis.set_major_locator(plt.MultipleLocator(_PSNR_Y_MAJOR_TICK))
        ax.yaxis.set_minor_locator(plt.MultipleLocator(_PSNR_Y_MINOR_TICK))
        ax.set_axisbelow(True)
        ax.grid(True, which="major", alpha=_GRID_ALPHA_MAJOR, zorder=0)
        ax.grid(True, which="minor", alpha=_GRID_ALPHA_MINOR, zorder=0)

    def _configure_pct_axis(ax: plt.Axes, color: str) -> None:
        ax.set_ylabel("SSIM / VMAF (%)", color=color, fontsize=_FONT_AXIS_LABEL, fontweight="bold")
        ax.set_ylim(_PCT_Y_MIN, _PCT_Y_MAX)
        ax.tick_params(axis="y", labelcolor=color, labelsize=_FONT_AXIS_TICKS)
        ax.yaxis.set_major_locator(plt.MultipleLocator(_PCT_Y_MAJOR_TICK))
        ax.yaxis.set_minor_locator(plt.MultipleLocator(_PCT_Y_MINOR_TICK))
        ax.set_axisbelow(True)

    if has_psnr and has_percentage_metrics:
        ax_left  = ax_main
        ax_right = ax_main.twinx()
        _configure_psnr_axis(ax_left)
        pct_color: str = effective_styles[MetricType.SSIM if has_ssim else MetricType.VMAF].color
        _configure_pct_axis(ax_right, pct_color)
        ax_right.set_axisbelow(True)
        ax_right.grid(True, which="major", alpha=_GRID_ALPHA_MAJOR, zorder=0)
        ax_right.grid(True, which="minor", alpha=_GRID_ALPHA_MINOR, zorder=0)
    elif has_psnr:
        ax_left = ax_main
        _configure_psnr_axis(ax_left)
    else:
        ax_right = ax_main
        pct_color = effective_styles[MetricType.SSIM if has_ssim else MetricType.VMAF].color
        _configure_pct_axis(ax_right, pct_color)
        ax_right.set_axisbelow(True)
        ax_right.grid(True, which="major", alpha=_GRID_ALPHA_MAJOR, zorder=0)
        ax_right.grid(True, which="minor", alpha=_GRID_ALPHA_MINOR, zorder=0)

    ax_main.set_title(title, fontsize=_FONT_TITLE, fontweight="bold", pad=20)
    ax_main.tick_params(axis="x", labelsize=_FONT_AXIS_TICKS_X)

    # Compute frame offset from chunk start so x-axis reflects source timestamps
    frame_offset: float = chunk_start_seconds * fps if (fps is not None and fps > 0) else 0.0

    # Scale metrics to display range once:
    #   SSIM: raw 0–1 → multiply by 100
    #   PSNR: clip inf → 100.0
    #   VMAF: unchanged (already 0–100)
    # All subsequent code uses scaled_values exclusively — no raw metrics access after this point.
    scaled_values: dict[MetricType, pd.Series] = {}
    frame_index:   dict[MetricType, pd.Index]  = {}
    for metric_type, metric_data in metrics.items():
        vals = metric_data.df[metric_data.column].copy()
        if metric_type == MetricType.SSIM:
            vals = vals * 100.0
        elif metric_type == MetricType.PSNR:
            vals = vals.clip(upper=100.0)
        scaled_values[metric_type] = vals
        frame_index[metric_type]   = metric_data.df.index

    max_frame = max(idx.max() for idx in frame_index.values()) + frame_offset
    min_frame = min(idx.min() for idx in frame_index.values()) + frame_offset
    x_pad     = (max_frame - min_frame) * _X_PADDING_RATIO
    ax_main.set_xlim(min_frame - x_pad, max_frame + x_pad)
    ax_main.xaxis.set_major_locator(ticker.MaxNLocator(nbins=_X_MAJOR_TICKS, integer=True))
    ax_main.xaxis.set_minor_locator(ticker.MaxNLocator(nbins=_X_MINOR_TICKS, integer=True))

    if fps is not None and fps > 0:
        ax_main.set_xlabel("Timestamp / Frame Number", fontsize=_FONT_AXIS_LABEL, fontweight="bold")

        def _dual_label_formatter(frame_num: float, _pos: int | None = None) -> str:
            """Format x-axis tick as 'HH:MM:SS\nframe N'."""
            total_seconds = int(frame_num / fps)
            hours         = total_seconds // 3600
            minutes       = (total_seconds % 3600) // 60
            seconds       = total_seconds % 60
            timestamp     = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            return f"{timestamp}\n{int(frame_num)}"

        ax_main.xaxis.set_major_formatter(ticker.FuncFormatter(_dual_label_formatter))
        # Increase bottom margin so two-line labels don't get clipped
        ax_main.tick_params(axis="x", labelsize=_FONT_AXIS_TICKS_X, pad=4)
    else:
        ax_main.set_xlabel("Frame Number", fontsize=_FONT_AXIS_LABEL, fontweight="bold")

    lines:  list[plt.Line2D] = []
    labels: list[str]        = []

    for metric_type in scaled_values:
        style  = effective_styles[metric_type]

        if style.y_axis == "left" and ax_left is not None:
            ax = ax_left
        elif style.y_axis == "right" and ax_right is not None:
            ax = ax_right
        else:
            ax = ax_main

        plot_values = scaled_values[metric_type]
        plot_index  = frame_index[metric_type] + frame_offset

        n_points = len(plot_values)
        min_raw_for_agg = _TARGET_PLOT_POINTS * _MIN_POINTS_PER_BIN
        if n_points >= min_raw_for_agg:
            window = max(1, n_points // _TARGET_PLOT_POINTS)
            smoothed    = plot_values.rolling(window=window, center=True, min_periods=1).mean()
            rolling_min = plot_values.rolling(window=window * 2, center=True, min_periods=1).min()
            rolling_max = plot_values.rolling(window=window * 2, center=True, min_periods=1).max()
            ax.fill_between(plot_index, rolling_min, rolling_max,
                            color=style.color, alpha=_RANGE_ALPHA, zorder=2,
                            label=f"{style.label} range")
            line, = ax.plot(plot_index, smoothed,
                            color=style.color, linestyle=style.linestyle,
                            linewidth=style.linewidth, label=style.label,
                            alpha=_LINE_ALPHA, zorder=3)
        else:
            line, = ax.plot(plot_index, plot_values,
                            color=style.color, linestyle=style.linestyle,
                            linewidth=style.linewidth, label=style.label,
                            alpha=_LINE_ALPHA, zorder=3)

        lines.append(line)
        labels.append(style.label)

    # Compute full statistics from scaled values
    stats: dict[MetricType, _MetricStatistics] = {
        mt: compute_statistics(scaled_values[mt])
        for mt in scaled_values
    }

    # Collect summary box data — rendered after tight_layout so axes position is final
    _summary_boxes: list[tuple[float, float, str, dict]] = []

    total_frames   = max(idx.max() for idx in frame_index.values()) + 1
    frames_checked = max(len(v) for v in scaled_values.values())
    current_x      = _SUMMARY_BOX_START_X

    _summary_boxes.append((
        current_x, _SUMMARY_BOX_Y_POS,
        f"Frames:\n  Total: {total_frames}\n  Checked: {frames_checked}\n  Factor: 1:{factor}",
        dict(facecolor="wheat", alpha=_SUMMARY_BOX_ALPHA),
    ))
    current_x += _SUMMARY_BOX_WIDTH + _SUMMARY_BOX_SPACING

    for metric_type in [MetricType.PSNR, MetricType.SSIM, MetricType.VMAF]:
        if metric_type not in scaled_values:
            continue
        metric_stats = stats[metric_type]
        style        = effective_styles[metric_type]
        vals         = scaled_values[metric_type]

        lossless_count = int((vals >= 100.0).sum())

        display_stats: dict[str, float] = dict(metric_stats)

        max_label: str   = "Max"
        max_value: float = display_stats["max"]
        if metric_type == MetricType.PSNR and np.isinf(max_value):
            for pkey, plabel in [("p95", "95%"), ("p90", "90%"), ("p75", "75%"), ("p50", "50%")]:
                if not np.isinf(metric_stats[pkey]):
                    max_label = plabel
                    max_value = metric_stats[pkey]
                    break

        summary_text = (
            f"{metric_type.value.upper()}:\n"
            f"  Min: {display_stats['min']:>5.1f}{style.unit}\n"
            f"  Med: {display_stats['p50']:>5.1f}{style.unit}\n"
            f"  {max_label}: {max_value:>5.1f}{style.unit}\n"
            f"  Std: {display_stats['std']:>5.1f}{style.unit}\n"
            f"  Lossless: {lossless_count} ({style.lossless_label})"
        )
        _summary_boxes.append((
            current_x, _SUMMARY_BOX_Y_POS, summary_text,
            dict(facecolor=style.color, alpha=_SUMMARY_BOX_METRIC_ALPHA),
        ))
        current_x += _SUMMARY_BOX_WIDTH + _SUMMARY_BOX_SPACING

    ax_main.legend(lines, labels, loc="lower right", fontsize=_FONT_LEGEND, framealpha=_LEGEND_ALPHA)

    # Statistics bar subplots
    bar_labels = ["Min", "5%", "25%", "50%", "75%", "95%", "Max"]
    stat_keys  = ["min", "p5", "p25", "p50", "p75", "p95", "max"]
    subplot_idx = 0

    for metric_type in [MetricType.PSNR, MetricType.SSIM, MetricType.VMAF]:
        if metric_type not in scaled_values:
            continue
        metric_stats = stats[metric_type]
        style        = effective_styles[metric_type]
        ax_stats     = fig.add_subplot(gs[1, subplot_idx])
        subplot_idx += 1

        stat_values = [metric_stats[k] for k in stat_keys]

        y_positions: np.ndarray                  = np.arange(len(bar_labels))
        base_rgb:    tuple[float, float, float]  = mcolors.to_rgb(style.color)
        colors:      list[tuple[float, ...]]     = [
            tuple(c * (1 - mix) + mix for c in base_rgb)
            for mix in [0.15 + 0.40 * i / (len(bar_labels) - 1) for i in range(len(bar_labels))]
        ]

        for i, (pos, val, label) in enumerate(zip(y_positions, stat_values, bar_labels)):
            if not (np.isnan(val) or np.isinf(val)):
                ax_stats.barh(pos, val, color=colors[i], alpha=_BAR_ALPHA, height=_BAR_HEIGHT)
                ax_stats.text(val, pos, f" {val:.1f}{style.unit}",
                              va="center", ha="left", fontsize=_FONT_BAR_LABEL, color=style.color)

        ax_stats.set_yticks(y_positions)
        ax_stats.set_yticklabels(bar_labels, fontsize=_FONT_AXIS_TICKS)
        ax_stats.set_xlabel(f"{metric_type.value.upper()} ({style.unit})",
                            fontsize=_FONT_SUBPLOT_XLABEL, fontweight="bold", color=style.color)
        ax_stats.set_title(f"{metric_type.value.upper()} Distribution",
                           fontsize=_FONT_SUBPLOT_TITLE, fontweight="bold", color=style.color)
        ax_stats.tick_params(axis="x", labelcolor=style.color, labelsize=_FONT_AXIS_TICKS_X)
        ax_stats.tick_params(axis="y", labelsize=_FONT_AXIS_TICKS)
        ax_stats.set_xlim(_PSNR_Y_MIN, _PSNR_Y_MAX) if metric_type == MetricType.PSNR \
            else ax_stats.set_xlim(_PCT_Y_MIN, _PCT_Y_MAX)
        ax_stats.grid(True, axis="x", alpha=_GRID_ALPHA_MAJOR, zorder=0)
        ax_stats.set_axisbelow(True)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore",
                                message="This figure includes Axes that are not compatible with tight_layout")
        plt.tight_layout()

    # -----------------------------------------------------------------------
    # Summary boxes — why fig.text() and NOT ax.text()
    # -----------------------------------------------------------------------
    # When a twinx() axes is present it sits in a separate axes layer that is
    # rendered on top of ax_main in the figure's stacking order.  Any text
    # added via ax_main.text(..., transform=ax_main.transAxes) lives in
    # ax_main's layer and is therefore drawn *under* the twinx axes — meaning
    # the twinx grid lines and background patch overdraw it regardless of the
    # zorder value set on the Text artist (zorder only sorts within one axes).
    #
    # The fix: use fig.text() with figure-level coordinates so the text is
    # composited in the figure layer, which is always above all axes layers.
    #
    # The coordinate conversion (axes → figure) must happen AFTER tight_layout()
    # because tight_layout repositions the axes; computing the transform before
    # that call would produce stale coordinates and misplace the boxes.
    #
    # What NOT to do:
    #   - ax.text(..., zorder=<large number>) — zorder is intra-axes only.
    #   - iterating gridlines and setting their zorder — seaborn styles render
    #     the grid as part of the axes background, not as Line2D artists, so
    #     get_xgridlines() / get_ygridlines() may return nothing useful.
    #   - switching to a "darkgrid" or "whitegrid" seaborn style and hoping
    #     rcParams["axes.grid"] = False suppresses it — it does not fully
    #     suppress the background patch that carries the grid texture.
    # -----------------------------------------------------------------------
    axes_to_fig = ax_main.transAxes + fig.transFigure.inverted()
    for ax_x, ax_y, text, bbox_kw in _summary_boxes:
        fx, fy = axes_to_fig.transform((ax_x, ax_y))
        fig.text(
            fx, fy, text,
            transform=fig.transFigure, fontsize=_FONT_SUMMARY_BOX,
            verticalalignment="bottom",
            bbox=dict(boxstyle="round", **bbox_kw),
            family="monospace",
        )

    fig.savefig(output_path, dpi=_PLOT_DPI, bbox_inches="tight")
    plt.close(fig)

    return stats


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def _extract_key_stats(full_stats: _MetricStatistics, metric_type: MetricType) -> MetricStats:
    """Extract the four key statistics from a full statistics dict.

    For PSNR, substitutes the highest non-inf percentile for ``max`` when the
    true maximum is infinite.

    Args:
        full_stats:  Full statistics dictionary.
        metric_type: Metric type (affects PSNR max handling).

    Returns:
        ``MetricStats`` with ``min``, ``median``, ``max``, and ``std``.
    """
    max_value = full_stats["max"]
    if metric_type == MetricType.PSNR and np.isinf(max_value):
        for pkey in ("p95", "p90", "p75", "p50"):
            candidate = full_stats[pkey]
            if not np.isinf(candidate):
                max_value = candidate
                break
    return {
        "min":    full_stats["min"],
        "median": full_stats["p50"],
        "max":    max_value,
        "std":    full_stats["std"],
    }


def _auto_output_path(
    psnr_log:  Path | None,
    ssim_log:  Path | None,
    vmaf_json: Path | None,
) -> Path:
    """Derive an output plot path from the first available metric file."""
    first = psnr_log or ssim_log or vmaf_json
    if not first:
        raise ValueError("At least one metric file must be provided")
    stem   = first.stem
    prefix = stem.split("_")[0] if "_" in stem else stem
    return first.parent / f"{prefix}_metrics.png"


def _save_stats_file(
    metric_type: MetricType,
    stats:       _MetricStatistics,
    metric_file: Path,
) -> None:
    """Persist statistics as a human-readable ``.stats`` text file.

    Args:
        metric_type: Metric type (determines unit and display scaling).
        stats:       Full statistics dictionary.
        metric_file: Original metric log file; ``.stats`` is written alongside it.
    """
    stats_path:    Path                  = metric_file.with_suffix(".stats")
    display_stats: dict[str, float]      = dict(stats)

    lines = [
        f"{metric_type.value.upper()} statistics",
        "=" * 40,
        *[f"{key:<3}:{display_stats[key]:>8.2f}" for key in display_stats],
    ]
    stats_path.write_text(os.linesep.join(lines), encoding="utf-8")
    logger.debug("Saved %s statistics to %s", metric_type.value.upper(), stats_path)


def analyze_chunk_quality(
    psnr_log:            Path | None = None,
    ssim_log:            Path | None = None,
    vmaf_json:           Path | None = None,
    factor:              int         = 1,
    output_path:         Path | None = None,
    title:               str | None  = None,
    generate_plot:       bool        = True,
    fps:                 float | None = None,
    chunk_start_seconds: float       = 0.0,
) -> ChunkQualityStats:
    """Analyze video chunk quality from metric log files.

    Parses the provided metric files, computes statistics, optionally generates
    a unified visualization plot, and saves per-metric ``.stats`` text files.

    Args:
        psnr_log:            Path to PSNR log file (optional).
        ssim_log:            Path to SSIM log file (optional).
        vmaf_json:           Path to VMAF JSON file (optional).
        factor:              Frame sampling factor used during metric generation.
        output_path:         Destination for the plot PNG.  Auto-derived when ``None``.
        title:               Plot title.  Auto-generated when ``None``.
        generate_plot:       Whether to create and save the visualization.
        fps:                 Frames per second of the encoded video.  When provided,
                             x-axis tick labels show ``HH:MM:SS`` on the top line and
                             the adjusted frame number on the bottom line.
        chunk_start_seconds: Start timestamp of the chunk in seconds.  When non-zero,
                             the x-axis is offset so frame numbers reflect the actual
                             position in the source video.

    Returns:
        ``ChunkQualityStats`` with ``min``, ``median``, ``max``, and ``std``
        for each available metric; unavailable metrics are ``None``.

    Raises:
        ValueError: If no valid metric file could be parsed.

    Side Effects:
        - Saves plot PNG to ``output_path`` (when ``generate_plot`` is ``True``).
        - Saves ``.stats`` text files alongside each metric log file.
    """
    result = ChunkQualityStats()

    parsed_metrics: dict[MetricType, MetricData]          = {}
    full_stats:     dict[MetricType, _MetricStatistics]   = {}
    metric_files:   dict[MetricType, Path]                = {}

    # --- PSNR ---
    if psnr_log is not None:
        try:
            logger.debug("Parsing PSNR log: %s", psnr_log)
            df = parse_psnr_file(psnr_log, factor)
            parsed_metrics[MetricType.PSNR] = MetricData(df=df, column=MetricType.PSNR.value)
            metric_files[MetricType.PSNR]   = psnr_log
            fs = compute_statistics(df[MetricType.PSNR.value], std_cutoff_max=100.0)
            full_stats[MetricType.PSNR]     = fs
            result[MetricType.PSNR]         = _extract_key_stats(fs, MetricType.PSNR)
            logger.debug("Parsed PSNR: %d frames", len(df))
        except Exception as exc:
            logger.warning("Failed to parse PSNR from %s: %s", psnr_log, exc)

    # --- SSIM ---
    if ssim_log is not None:
        try:
            logger.debug("Parsing SSIM log: %s", ssim_log)
            df = parse_ssim_file(ssim_log, factor)
            parsed_metrics[MetricType.SSIM] = MetricData(df=df, column=MetricType.SSIM.value)
            metric_files[MetricType.SSIM]   = ssim_log
            fs = compute_statistics(df[MetricType.SSIM.value])
            full_stats[MetricType.SSIM]     = fs
            result[MetricType.SSIM]         = _extract_key_stats(fs, MetricType.SSIM)
            logger.debug("Parsed SSIM: %d frames", len(df))
        except Exception as exc:
            logger.warning("Failed to parse SSIM from %s: %s", ssim_log, exc)

    # --- VMAF ---
    if vmaf_json is not None:
        try:
            logger.debug("Parsing VMAF JSON: %s", vmaf_json)
            df = parse_vmaf_file(vmaf_json, factor)
            parsed_metrics[MetricType.VMAF] = MetricData(df=df, column=MetricType.VMAF.value)
            metric_files[MetricType.VMAF]   = vmaf_json
            fs = compute_statistics(df[MetricType.VMAF.value])
            full_stats[MetricType.VMAF]     = fs
            result[MetricType.VMAF]         = _extract_key_stats(fs, MetricType.VMAF)
            logger.debug("Parsed VMAF: %d frames", len(df))
        except Exception as exc:
            logger.warning("Failed to parse VMAF from %s: %s", vmaf_json, exc)

    if not parsed_metrics:
        raise ValueError(
            "No valid metrics could be parsed. "
            "At least one valid metric file (PSNR, SSIM, or VMAF) is required."
        )

    # Normalize all stat values to 0–100 scale immediately after extraction (Req 5.1)
    for metric_type, stats in result.items():
        if stats is not None:
            for stat_key in ("min", "median", "max", "std"):
                raw = stats.get(stat_key)
                if raw is not None:
                    stats[stat_key] = normalize_metric(metric_type, raw)  # type: ignore[literal-required]

    # Single concise info summary
    parts = []
    for mt in [MetricType.VMAF, MetricType.PSNR, MetricType.SSIM]:
        if mt in result and result[mt] is not None:
            s = result[mt]
            parts.append(f"{mt.value.upper()} min={s['min']:.1f} med={s['median']:.1f}")
    if parts:
        logger.debug("Metrics (normalized): %s", " | ".join(parts))

    if generate_plot:
        if output_path is None:
            output_path = _auto_output_path(psnr_log, ssim_log, vmaf_json)
        if title is None:
            names = [mt.value.upper() for mt in parsed_metrics]
            title = f"Video Quality Metrics Analysis ({', '.join(names)})"
        logger.debug("Generating unified plot: %s", output_path)
        create_unified_plot(
            metrics=parsed_metrics,
            factor=factor,
            output_path=output_path,
            title=title,
            fps=fps,
            chunk_start_seconds=chunk_start_seconds,
        )
        logger.debug("Plot saved to %s", output_path)

    for metric_type, metric_file in metric_files.items():
        if metric_type in full_stats:
            _save_stats_file(metric_type, full_stats[metric_type], metric_file)

    return result


class QualityEvaluator:
    """Evaluates encoded chunks against quality targets.

    This class integrates with the metric runner for metric generation and
    metrics_visualization for parsing and plotting.
    """

    def __init__(self, work_dir: Path) -> None:
        """Initialize quality evaluator.

        Args:
            work_dir: Working directory for metric artifacts
        """
        self.work_dir: Path = work_dir

    async def _generate_metrics(
        self,
        encoded: Path,
        reference: Path,
        ref_crop: CropParams,
        output_prefix: str,
        metrics_sampling: int = 10,
        bar_advance: Callable[[float], None] | None = None,
        duration_seconds: float = 0.0,
    ) -> tuple[Path, Path, Path]:
        """Generate metric log files for quality comparison.

        ffmpeg is run in the encoded file's parent directory using a UUID-based
        temporary filename prefix so that no special characters appear in the
        filter-graph string.  Each metric is written to a ``.tmp``-suffixed file
        while ffmpeg is running; on success the file is atomically renamed to its
        final canonical path derived from ``output_prefix``.

        This ensures the standard ``.tmp`` cleanup routine removes any leftover
        files from interrupted runs.

        When ``bar_advance`` is provided, each ffmpeg process reports progress
        via ``ProgressCallback`` which advances the bar based on ``out_time_seconds``.

        Args:
            encoded:          Path to encoded video file
            reference:        Path to reference video file
            ref_crop:         Crop parameters for the reference input
            output_prefix:    Full path prefix for the final metric files
                              (e.g. ``/work/encoded/slow_h265/chunk.000000-000195.``)
            metrics_sampling: Frame subsampling factor
            bar_advance:      Optional callable that advances the progress bar
                              by the given number of seconds.
            duration_seconds: Duration of the encoded clip in seconds; used to
                              cap per-process bar advances.

        Returns:
            Tuple of (psnr_log, ssim_log, vmaf_json) final paths
        """
        # UUID prefix keeps special characters out of the ffmpeg filter graph;
        # the .tmp extension ensures cleanup on interrupted runs.
        uuid_hex = uuid.uuid4().hex
        cwd = encoded.parent

        logger.debug(
            "Generating metrics for %s vs %s (tmp prefix: %s)",
            encoded.name, reference.name, uuid_hex,
        )

        def _make_progress_callback(last_time: list[float]) -> Callable[[int, float], None] | None:
            """Build a ProgressCallback that converts absolute out_time_s to bar deltas."""
            if bar_advance is None or duration_seconds <= 0:
                return None

            def _callback(frame: int, out_time_s: float) -> None:
                delta = max(0.0, out_time_s - last_time[0])
                delta = min(delta, duration_seconds - last_time[0])
                if delta > 0:
                    bar_advance(delta)
                    last_time[0] = out_time_s

            return _callback

        # tmp_prefix used as output_prefix in run_metric; extension overridden to .tmp
        # so ffmpeg writes e.g. <uuid>.psnr.tmp, <uuid>.ssim.tmp, <uuid>.vmaf.tmp
        tmp_prefix = f"{uuid_hex}."

        async def _run_one(metric: MetricType) -> None:
            result = await run_metric(
                metric=metric,
                distorted=encoded,
                reference=reference,
                crop_distorted=CropParams(),
                crop_reference=ref_crop,
                duration=0,
                width=0,
                use_gpu=False,
                subsample=metrics_sampling,
                output_prefix=tmp_prefix,
                cwd=cwd,
                progress_callback=_make_progress_callback([0.0]),
                output_extension=".tmp",
            )
            if not result.success:
                logger.warning(
                    "Metric %s calculation had non-zero exit code: %d",
                    metric.value, result.returncode,
                )

        await asyncio.gather(*[_run_one(metric) for metric in MetricType])

        # Rename <uuid>.<metric>.tmp → final canonical path (single atomic rename per file)
        final_psnr = Path(f"{output_prefix}{MetricType.PSNR.value}.log")
        final_ssim = Path(f"{output_prefix}{MetricType.SSIM.value}.log")
        final_vmaf = Path(f"{output_prefix}{MetricType.VMAF.value}.json")

        for metric, final_path in [
            (MetricType.PSNR, final_psnr),
            (MetricType.SSIM, final_ssim),
            (MetricType.VMAF, final_vmaf),
        ]:
            tmp_path = cwd / f"{tmp_prefix}{metric.value}.tmp"
            if tmp_path.exists():
                final_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    tmp_path.replace(final_path)
                except OSError as exc:
                    logger.warning(
                        "Failed to rename metric tmp file %s → %s: %s",
                        tmp_path, final_path, exc,
                    )
            else:
                logger.warning("Expected metric tmp file not found: %s", tmp_path)

        return final_psnr, final_ssim, final_vmaf

    def evaluate_chunk(
        self,
        encoded: Path,
        reference: Path,
        ref_crop: CropParams,
        targets: list[QualityTarget],
        output_dir: Path,
        subsample_factor: int = 10,
        show_progress: bool = False,
        plot_path: Path | None = None,
        chunk_start_seconds: float = 0.0,
    ) -> QualityEvaluation:
        """Evaluate encoded chunk against reference and quality targets.

        Args:
            encoded:             Path to encoded video file
            reference:           Path to reference video file
            ref_crop:            Crop parameters for the reference input
            targets:             List of quality targets to evaluate against
            output_dir:          Directory for raw metric log files and stats
            subsample_factor:    Frame subsampling factor for metrics
            show_progress:       If True, display a live progress bar (use only
                                 when not nested inside another alive_bar context,
                                 e.g. for the final full-video metrics run after merge).
            plot_path:           Explicit path for the PNG plot.  When ``None``,
                                 the plot is written as ``<encoded.stem>.png``
                                 inside ``output_dir``.
            chunk_start_seconds: Start timestamp of the chunk in seconds.  Used to
                                 offset the x-axis so the graph reflects the actual
                                 position in the source video.

        Returns:
            QualityEvaluation with metrics and target evaluation results
        """
        # Ensure output directory exists
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate output prefix for metric files
        output_prefix = str(output_dir / f"{encoded.stem}.")

        # Probe duration for progress bar total (3 metric passes)
        _NUM_METRIC_PASSES = 3
        duration_seconds: float | None = None
        fps_value:        float | None = None
        try:
            from pyqenc.models import VideoMetadata
            vm = VideoMetadata(path=encoded)
            duration_seconds = vm.duration_seconds
            fps_value        = vm.fps
        except Exception:
            pass

        bar_title = encoded.stem.replace(TIME_SEPARATOR_MS, ".").replace(TIME_SEPARATOR_SAFE, ":")

        if show_progress:
            with ProgressBar(_NUM_METRIC_PASSES * (duration_seconds or 0.0), title=f"Metrics: {bar_title}", show_counters=False) as advance:
                psnr_log, ssim_log, vmaf_json = asyncio.run(
                    self._generate_metrics(
                        encoded,
                        reference,
                        ref_crop,
                        output_prefix,
                        subsample_factor,
                        bar_advance=advance,
                        duration_seconds=duration_seconds or 0.0,
                    )
                )
        else:
            psnr_log, ssim_log, vmaf_json = asyncio.run(
                self._generate_metrics(
                    encoded,
                    reference,
                    ref_crop,
                    output_prefix,
                    subsample_factor,
                    bar_advance=None,
                    duration_seconds=duration_seconds or 0.0,
                )
            )

        # Parse metrics and generate plots using metrics_visualization
        logger.debug("Parsing metrics and generating plots")
        resolved_plot_path = plot_path if plot_path is not None else output_dir / f"{encoded.stem}.png"
        metrics = analyze_chunk_quality(
            psnr_log=psnr_log if psnr_log.exists() else None,
            ssim_log=ssim_log if ssim_log.exists() else None,
            vmaf_json=vmaf_json if vmaf_json.exists() else None,
            factor=subsample_factor,
            output_path=resolved_plot_path,
            title=f"Quality metrics for {encoded.stem.replace(TIME_SEPARATOR_MS, ".").replace(TIME_SEPARATOR_SAFE, ":")}",
            generate_plot=True,
            fps=fps_value,
            chunk_start_seconds=chunk_start_seconds,
        )

        # Collect artifacts
        artifacts = QualityArtifacts(
            psnr_log=psnr_log if psnr_log.exists() else None,
            ssim_log=ssim_log if ssim_log.exists() else None,
            vmaf_json=vmaf_json if vmaf_json.exists() else None,
            plot=resolved_plot_path,
            stats_files=[]
        )

        # Collect stats files
        for metric_file in [psnr_log, ssim_log, vmaf_json]:
            if metric_file and metric_file.exists():
                stats_file = metric_file.with_suffix('.stats')
                if stats_file.exists():
                    artifacts.stats_files.append(stats_file)

        # Evaluate against targets
        failed_targets: list[QualityTarget] = []
        for target in targets:

            metric_stats = metrics.get(MetricType(target.metric))
            if metric_stats is None:
                logger.warning("Target metric '%s' not available in results", target.metric)
                failed_targets.append(target)
                continue

            # Get the statistic value
            actual_value = metric_stats.get(target.statistic)
            if actual_value is None:
                logger.warning(
                    "Target statistic '%s' not available for metric '%s'",
                    target.statistic,
                    target.metric,
                )
                failed_targets.append(target)
                continue

            # Compare against target
            if actual_value < target.value:
                logger.debug(
                    "Target not met: %s-%s:%s (actual: %.2f)",
                    target.metric, target.statistic, target.value, actual_value,
                )
                failed_targets.append(target)
            else:
                logger.debug(
                    "Target met: %s-%s:%s (actual: %.2f)",
                    target.metric, target.statistic, target.value, actual_value,
                )

        targets_met = len(failed_targets) == 0

        return QualityEvaluation(
            metrics=metrics,
            targets_met=targets_met,
            failed_targets=failed_targets,
            artifacts=artifacts
        )
