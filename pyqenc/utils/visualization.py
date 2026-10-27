"""Unified visualization and quality metrics analysis for video encoding pipeline."""
# CHerSun 2026

import asyncio
import logging
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Literal, cast

import matplotlib

from pyqenc.constants import (
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import CropParams
from pyqenc.quality import (
    QualityLogs,
    QualityMeasurement,
    run_metrics,
)
from pyqenc.quality_metrics import (
    MeasuredMetrics,
    MetricInfo,
    MetricStatKey,
    MetricType,
    StatisticType,
)
from pyqenc.utils.alive import AdvanceState, BarAdvance, ProgressBar
from pyqenc.utils.ffmpeg_runner import FFmpegInput

matplotlib.use("Agg")  # non-interactive backend — safe to call from any thread
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import ticker

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
# SSIM/VMAF/VIF values live in the upper band (good encodes sit 90+); a 0
# floor crams them into a thin top strip on both the main right axis and the
# distribution bar subplots.
_PCT_Y_MIN:         int = 50
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
_SUMMARY_BOX_Y_POS:        float = 0.02
_SUMMARY_BOX_START_X:      float = 0.02
_SUMMARY_BOX_SPACING:      float = 0.016  # text-to-text gap between boxes, figure fraction
_SUMMARY_BOX_ALPHA:        float = 0.8
_SUMMARY_BOX_METRIC_ALPHA: float = 0.3

# Bar chart
_BAR_HEIGHT: float = 0.7
_BAR_ALPHA:  float = 0.8

# Misc
_PLOT_DPI:         int   = 200
_GRID_ALPHA_MAJOR: float = 0.3
_GRID_ALPHA_MINOR: float = 0.1
_LEGEND_ALPHA:     float = 0.9
_MARKER_SIZE:      int   = 10
_MARKER_ALPHA:     float = 0.9
_TIGHT_LAYOUT_PAD: float = 0.0   # outer padding for tight_layout (default ~1.08 is too large)

# Quality plot
_QUALITY_COLOR:       str   = "#8B0000"   # dark red
_QUALITY_Y_MIN:       float = 0.0
_QUALITY_Y_MAX:       float = 42.0        # assuming 40 is max sane quality value for the plot + a little spacing
_QUALITY_Y_MAJOR_TICK: float = 5.0
_QUALITY_Y_MINOR_TICK: float = 1.0

# VIF metric colors
_VIF_COLOR:      str = "#7B2D8B"   # purple
_VIF_FILL_COLOR: str = "#C084D4"   # light purple (range fill)


# ---------------------------------------------------------------------------
# Statistics (the measurer-owned shared quantile step)
# ---------------------------------------------------------------------------

_QUANTILE_STATS: tuple[tuple[StatisticType, float], ...] = (
    (StatisticType.MIN,  0.00),
    (StatisticType.P05,  0.05),
    (StatisticType.P10,  0.10),
    (StatisticType.P25,  0.25),
    (StatisticType.MEDIAN, 0.50),
    (StatisticType.P75,  0.75),
    (StatisticType.P90,  0.90),
    (StatisticType.P95,  0.95),
)

def _quantile_statistics(
    values:         pd.Series,
    std_cutoff_max: float | None = None,
) -> dict[StatisticType, float]:
    """Compute the closed set's quantile statistics for a numeric series.

    The quantile machinery behind the measurer's shared statistics step —
    pandas stays here. Values are expected to already be on the display
    scale.

    Args:
        values:         The series (already normalized).
        std_cutoff_max: Exclude values above this threshold from std
                        (``None`` = every value).

    Returns:
        Every closed-set statistic for the series.
    """
    stats = {stat: float(values.quantile(q)) for stat, q in _QUANTILE_STATS}
    stats[StatisticType.MAX] = float(values.max())
    std_values = values if std_cutoff_max is None else values[values <= std_cutoff_max]
    stats[StatisticType.STD] = float(std_values.std())
    return stats


def _metric_statistics(
    values: pd.Series,
    info:    MetricInfo,
) -> dict[StatisticType, float]:
    """Compute the quantile-based statistics for one metric's normalized series.

    The shared statistics step (Req 10.2) — owned by the measurer; the
    per-metric classes never duplicate it. The metric's descriptor carries
    its std cutoff.

    Args:
        values: Normalized per-frame values.
        info:   The metric's descriptor.

    Returns:
        Every closed-set statistic for the metric.
    """
    return _quantile_statistics(values, info.std_cutoff_max)




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
        label              = "PSNR",
        color              = "blue",
        unit               = MetricType.PSNR.info.display_unit,
        y_axis             = "left",
        linestyle          = "-",
        linewidth          = _LINE_WIDTH_DEFAULT,
        lossless_threshold = MetricType.PSNR.info.lossless_value,
        lossless_label     = MetricType.PSNR.info.lossless_raw_repr,
    ),
    MetricType.SSIM: MetricVisualStyle(
        label              = "SSIM",
        color              = "green",
        unit               = MetricType.SSIM.info.display_unit,
        y_axis             = "right",
        linestyle          = "-",
        linewidth          = _LINE_WIDTH_DEFAULT,
        lossless_threshold = MetricType.SSIM.info.lossless_value,
        lossless_label     = MetricType.SSIM.info.lossless_raw_repr,
    ),
    MetricType.VMAF: MetricVisualStyle(
        label              = "VMAF",
        color              = "#CC6600",
        unit               = MetricType.VMAF.info.display_unit,
        y_axis             = "right",
        linestyle          = "-",
        linewidth          = _LINE_WIDTH_DEFAULT,
        lossless_threshold = MetricType.VMAF.info.lossless_value,
        lossless_label     = MetricType.VMAF.info.lossless_raw_repr,
    ),
    MetricType.VIF: MetricVisualStyle(
        label              = "VIF",
        color              = _VIF_COLOR,
        unit               = MetricType.VIF.info.display_unit,
        y_axis             = "right",
        linestyle          = "-",
        linewidth          = _LINE_WIDTH_DEFAULT,
        lossless_threshold = MetricType.VIF.info.lossless_value,
        lossless_label     = MetricType.VIF.info.lossless_raw_repr,
    ),
}


# ---------------------------------------------------------------------------
# Plot creation
# ---------------------------------------------------------------------------

def create_unified_plot(
    series_norm:         dict[MetricType, pd.Series],
    stats:               Mapping[MetricType, Mapping[StatisticType, float]],
    factor:              int,
    output_path:         Path,
    title:               str                                  = "Video Quality Metrics Analysis",
    styles:              dict[MetricType, MetricVisualStyle] | None = None,
    fps:                 Fraction | None                      = None,
    chunk_start_seconds: float                               = 0.0,
) -> None:
    """Create a unified quality-metrics plot and save it to disk.

    Generates a figure with:
    - Main plot area with dual Y-axes (PSNR left, SSIM/VMAF right).
    - Horizontal summary boxes at the bottom of the main plot.
    - Per-metric statistics bar subplots below the main plot.

    Args:
        series_norm:         Normalized per-metric series (one per measured
                             metric, indexed by frame number).
        stats:               The statistics to display — one closed-set map
                             per metric (the measurer's shared step output).
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

    Raises:
        ValueError: If ``series_norm`` is empty.
    """
    # Callers pass the exact rational ``fps_fraction`` — coerce to float here
    # so every x-axis arithmetic stays float (Fraction + float index would
    # produce object-dtype arrays matplotlib cannot plot).
    fps_val: float | None = float(fps) if fps is not None else None

    metrics: dict[MetricType, pd.Series] = dict(series_norm)

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
    has_percentage_metrics: bool = has_ssim or has_vmaf or (MetricType.VIF in metrics)
    ax_left:  plt.Axes | None = None
    ax_right: plt.Axes | None = None

    def _configure_psnr_axis(ax: plt.Axes) -> None:
        style = effective_styles[MetricType.PSNR]
        info  = MetricType.PSNR.info
        ax.set_ylabel(f"PSNR ({info.display_unit.strip()})", color=style.color, fontsize=_FONT_AXIS_LABEL, fontweight="bold")
        ax.set_ylim(info.plot_y_min, info.plot_y_max)
        ax.tick_params(axis="y", labelcolor=style.color, labelsize=_FONT_AXIS_TICKS)
        ax.yaxis.set_major_locator(plt.MultipleLocator(_PSNR_Y_MAJOR_TICK))
        ax.yaxis.set_minor_locator(plt.MultipleLocator(_PSNR_Y_MINOR_TICK))
        ax.set_axisbelow(True)
        ax.grid(True, which="major", alpha=_GRID_ALPHA_MAJOR, zorder=0)
        ax.grid(True, which="minor", alpha=_GRID_ALPHA_MINOR, zorder=0)

    def _configure_pct_axis(ax: plt.Axes, color: str) -> None:
        ax.set_ylabel("SSIM / VMAF / VIF", color=color, fontsize=_FONT_AXIS_LABEL, fontweight="bold")
        # All percentage metrics share one plot range; PSNR keeps its own dB range
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
    frame_offset: float = chunk_start_seconds * fps_val if (fps_val is not None and fps_val > 0) else 0.0

    # Values are already normalized (df_norm); build index map per metric.
    scaled_values: dict[MetricType, pd.Series] = {}
    frame_index:   dict[MetricType, pd.Index]  = {}
    for metric_type, series in metrics.items():
        scaled_values[metric_type] = series
        frame_index[metric_type]   = series.index

    if fps_val is not None and fps_val > 0:
        # Capture total_frames before converting index to seconds
        total_frames_for_summary: int = int(max(idx.max() for idx in frame_index.values()) + frame_offset) + 1

        # Convert x-axis to seconds so tick positions align with the quality plot.
        # frame_index values are frame numbers; divide by fps to get seconds.
        for mt in list(frame_index.keys()):
            frame_index[mt] = frame_index[mt] / fps_val
        frame_offset = frame_offset / fps_val  # already seconds/fps, now pure seconds

        max_seconds = max(idx.max() for idx in frame_index.values()) + frame_offset
        min_seconds = min(idx.min() for idx in frame_index.values()) + frame_offset
        x_pad       = (max_seconds - min_seconds) * _X_PADDING_RATIO
        ax_main.set_xlim(min_seconds - x_pad, max_seconds + x_pad)
        ax_main.xaxis.set_major_locator(ticker.MaxNLocator(nbins=_X_MAJOR_TICKS))
        ax_main.xaxis.set_minor_locator(ticker.MaxNLocator(nbins=_X_MINOR_TICKS))
        ax_main.set_xlabel("Timestamp / Frame Number", fontsize=_FONT_AXIS_LABEL, fontweight="bold")

        def _dual_label_formatter(seconds: float, _pos: int | None = None) -> str:
            """Format x-axis tick as 'HH:MM:SS\nframe N'."""
            total_s = int(seconds)
            hours   = total_s // 3600
            minutes = (total_s % 3600) // 60
            secs    = total_s % 60
            frame_n = int(seconds * fps_val)
            return f"{hours:02d}:{minutes:02d}:{secs:02d}\n{frame_n}"

        ax_main.xaxis.set_major_formatter(ticker.FuncFormatter(_dual_label_formatter))
        ax_main.tick_params(axis="x", labelsize=_FONT_AXIS_TICKS_X, pad=4)
    else:
        max_frame = max(idx.max() for idx in frame_index.values()) + frame_offset
        min_frame = min(idx.min() for idx in frame_index.values()) + frame_offset
        x_pad     = (max_frame - min_frame) * _X_PADDING_RATIO
        ax_main.set_xlim(min_frame - x_pad, max_frame + x_pad)
        ax_main.xaxis.set_major_locator(ticker.MaxNLocator(nbins=_X_MAJOR_TICKS, integer=True))
        ax_main.xaxis.set_minor_locator(ticker.MaxNLocator(nbins=_X_MINOR_TICKS, integer=True))
        ax_main.set_xlabel("Frame Number", fontsize=_FONT_AXIS_LABEL, fontweight="bold")

    lines:  list[plt.Line2D] = []
    labels: list[str]        = []

    for metric_type, plot_values in scaled_values.items():
        style  = effective_styles[metric_type]

        if style.y_axis == "left" and ax_left is not None:
            ax = ax_left
        elif style.y_axis == "right" and ax_right is not None:
            ax = ax_right
        else:
            ax = ax_main

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

    # Collect summary box payloads — placed after tight_layout by MEASURED text
    # width (lossless counts and value digits vary the rendered box width run
    # to run; a fixed advance step overlaps on wide boxes).
    _summary_boxes: list[tuple[str, dict]] = []

    total_frames   = total_frames_for_summary if fps_val is not None and fps_val > 0 \
                     else int(max(idx.max() for idx in frame_index.values()) + 1)
    frames_checked = max(len(v) for v in scaled_values.values())

    _summary_boxes.append((
        f"Frames:\n  Total: {total_frames}\n  Checked: {frames_checked}\n  Factor: 1:{factor}",
        {"facecolor": "wheat", "alpha": _SUMMARY_BOX_ALPHA},
    ))

    for metric_type in metrics:
        if metric_type not in scaled_values:
            continue
        metric_stats = stats[metric_type]
        style        = effective_styles[metric_type]
        vals         = scaled_values[metric_type]

        lossless_count = int((vals >= 100.0).sum())

        summary_text = (
            f"{metric_type.value.upper()}:\n"
            f"  Min: {metric_stats[StatisticType.MIN]:>6.2f}{style.unit}\n"
            f"  Med: {metric_stats[StatisticType.MEDIAN]:>6.2f}{style.unit}\n"
            f"  Max: {metric_stats[StatisticType.MAX]:>6.2f}{style.unit}\n"
            f"  Std: {metric_stats[StatisticType.STD]:>6.2f}{style.unit}\n"
            f"  Lossless: {lossless_count} ({style.lossless_label})"
        )
        _summary_boxes.append((
            summary_text,
            {"facecolor": style.color, "alpha": _SUMMARY_BOX_METRIC_ALPHA},
        ))

    ax_main.legend(lines, labels, loc="lower right", fontsize=_FONT_LEGEND, framealpha=_LEGEND_ALPHA)

    # Statistics bar subplots
    bar_stats: tuple[StatisticType, ...] = (
        *[stat for stat, _ in _QUANTILE_STATS],
        StatisticType.MAX,
    )
    subplot_idx = 0

    for metric_type in metrics:
        if metric_type not in scaled_values:
            continue
        metric_stats = stats[metric_type]
        style        = effective_styles[metric_type]
        ax_stats     = fig.add_subplot(gs[1, subplot_idx])
        subplot_idx += 1

        bar_labels = [statistic.display for statistic in bar_stats]
        stat_values = [metric_stats[statistic] for statistic in bar_stats]

        y_positions: np.ndarray                  = np.arange(len(bar_labels))
        base_rgb:    tuple[float, float, float]  = mcolors.to_rgb(style.color)
        colors:      list[tuple[float, ...]]     = [
            tuple(c * (1 - mix) + mix for c in base_rgb)
            for mix in [0.15 + 0.40 * i / (len(bar_labels) - 1) for i in range(len(bar_labels))]
        ]

        for i, (pos, val, label) in enumerate(zip(y_positions, stat_values, bar_labels)):
            if not (np.isnan(val) or np.isinf(val)):
                ax_stats.barh(pos, val, color=colors[i], alpha=_BAR_ALPHA, height=_BAR_HEIGHT)
                ax_stats.text(val, pos, f" {val:.2f}{style.unit}",
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
        plt.tight_layout(pad=_TIGHT_LAYOUT_PAD)

    # Summary boxes use fig.text() (figure-layer coordinates): ax.text would be
    # drawn under the twinx axes — zorder only sorts within one axes. The
    # axes→figure transform must run after tight_layout, which repositions axes.
    # Boxes are placed left-to-right, each advancing by its MEASURED text width
    # plus SPACING — the only way the row packs with uniform gaps regardless of
    # how wide the run's values make the text, while staying clear of the
    # lower-right legend.
    fig.canvas.draw()
    renderer  = fig.canvas.get_renderer()  # ty: ignore[unresolved-attribute] — Agg canvas; base stub lacks it
    axes_to_fig = ax_main.transAxes + fig.transFigure.inverted()
    fx, fy    = axes_to_fig.transform((_SUMMARY_BOX_START_X, _SUMMARY_BOX_Y_POS))
    fig_width = fig.bbox.width
    cursor    = fx
    for text, bbox_kw in _summary_boxes:
        box = fig.text(cursor, fy, text, transform=fig.transFigure,
                       fontsize=_FONT_SUMMARY_BOX, verticalalignment="bottom",
                       bbox=dict(boxstyle="round", **bbox_kw), family="monospace")
        cursor += (box.get_window_extent(renderer).width / fig_width) + _SUMMARY_BOX_SPACING

    fig.set_size_inches(_FIG_WIDTH, _FIG_HEIGHT)
    fig.savefig(output_path, dpi=_PLOT_DPI)
    plt.close(fig)


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

# All four metrics run together in a single ffmpeg pass.
_metrics_all: list[MetricType] = list(MetricType)



# ---------------------------------------------------------------------------
# Quality distribution plot
# ---------------------------------------------------------------------------

def create_quality_plot(
    chunks:        list[tuple[float, float, Decimal]],
    output_path:   Path,
    quality_label: str,
    title:         str = "Quality Distribution",
) -> None:
    """Create a quality-parameter-over-time plot and save it to disk.

    Layout mirrors ``create_unified_plot`` for side-by-side comparison.

    Args:
        chunks:        List of ``(start_seconds, end_seconds, quality_value)`` tuples,
                       one per winning chunk, in timeline order.
        output_path:   Destination path for the saved PNG.
        quality_label: Human-readable label for the quality axis (e.g. ``"CRF"``,
                       ``"CQ"``).
        title:         Plot title.

    Raises:
        ValueError: If ``chunks`` is empty.
    """
    if not chunks:
        raise ValueError(f"No {quality_label} data provided for visualization")

    starts = np.array([c[0] for c in chunks])
    ends   = np.array([c[1] for c in chunks])
    qualities   = np.array([float(c[2]) for c in chunks])

    plt.style.use("seaborn-v0_8")
    plt.rcParams["axes.grid"] = False

    # 2 rows × 3 columns; the stats subplot spans all 3 columns so the main
    # plot width matches the 3-metric metrics plot.
    _N_STAT_COLS: int = 3
    fig = plt.figure(figsize=(_FIG_WIDTH, _FIG_HEIGHT))
    gs  = fig.add_gridspec(
        2, _N_STAT_COLS,
        height_ratios=[_MAIN_PLOT_HEIGHT_RATIO, _STATS_PLOT_HEIGHT_RATIO],
        hspace=_GRID_HSPACE,
        wspace=_GRID_WSPACE,
    )

    ax_left  = fig.add_subplot(gs[0, :])
    ax_right = ax_left.twinx()
    ax_left.set_axisbelow(True)
    ax_right.set_axisbelow(True)

    def _configure_quality_axis(ax: plt.Axes) -> None:
        ax.set_ylabel(quality_label, color=_QUALITY_COLOR, fontsize=_FONT_AXIS_LABEL, fontweight="bold")
        ax.set_ylim(_QUALITY_Y_MIN, _QUALITY_Y_MAX)
        ax.tick_params(axis="y", labelcolor=_QUALITY_COLOR, labelsize=_FONT_AXIS_TICKS)
        ax.yaxis.set_major_locator(plt.MultipleLocator(_QUALITY_Y_MAJOR_TICK))
        ax.yaxis.set_minor_locator(plt.MultipleLocator(_QUALITY_Y_MINOR_TICK))
        ax.set_axisbelow(True)
        ax.grid(True, which="major", alpha=_GRID_ALPHA_MAJOR, zorder=0)
        ax.grid(True, which="minor", alpha=_GRID_ALPHA_MINOR, zorder=0)

    _configure_quality_axis(ax_left)
    _configure_quality_axis(ax_right)

    ax_left.set_title(title, fontsize=_FONT_TITLE, fontweight="bold", pad=20)
    ax_left.tick_params(axis="x", labelsize=_FONT_AXIS_TICKS_X)

    # X-axis: seconds, formatted as HH:MM:SS / s (two lines like the metrics plot)
    x_min = float(starts.min())
    x_max = float(ends.max())
    x_pad = (x_max - x_min) * _X_PADDING_RATIO
    ax_left.set_xlim(x_min - x_pad, x_max + x_pad)
    ax_left.xaxis.set_major_locator(ticker.MaxNLocator(nbins=_X_MAJOR_TICKS, integer=True))
    ax_left.xaxis.set_minor_locator(ticker.MaxNLocator(nbins=_X_MINOR_TICKS, integer=True))
    ax_left.set_xlabel("Timestamp / Seconds", fontsize=_FONT_AXIS_LABEL, fontweight="bold")

    def _ts_formatter(seconds: float, _pos: int | None = None) -> str:
        """Format x-axis tick as 'HH:MM:SS\n{s:.1f}s' — two lines like the metrics plot."""
        total_s = int(seconds)
        h       = total_s // 3600
        m       = (total_s % 3600) // 60
        s       = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}\n{seconds:.1f}s"

    ax_left.xaxis.set_major_formatter(ticker.FuncFormatter(_ts_formatter))
    ax_left.tick_params(axis="x", labelsize=_FONT_AXIS_TICKS_X, pad=4)

    # Main plot: step line on left axis — append sentinel at ends[-1] so last chunk reaches right edge
    order  = np.argsort(starts)
    step_x = np.append(starts[order], ends[order[-1]])
    step_y = np.append(qualities[order],   qualities[order[-1]])
    line, = ax_left.step(
        step_x, step_y,
        where     = "post",
        color     = _QUALITY_COLOR,
        linewidth = _LINE_WIDTH_DEFAULT,
        alpha     = _LINE_ALPHA,
        label     = quality_label,
        zorder    = 3,
    )
    ax_left.legend([line], [quality_label], loc="lower right", fontsize=_FONT_LEGEND, framealpha=_LEGEND_ALPHA)

    # Stats subplot
    quality_series  = pd.Series(qualities)
    quality_stats   = _quantile_statistics(quality_series, std_cutoff_max=_QUALITY_Y_MAX)

    bar_stats   = (
        StatisticType.MIN, StatisticType.P05, StatisticType.P25, StatisticType.MEDIAN,
        StatisticType.P75, StatisticType.P95, StatisticType.MAX,
    )
    bar_labels  = [statistic.display for statistic in bar_stats]
    stat_values = [quality_stats[statistic] for statistic in bar_stats]

    ax_stats    = fig.add_subplot(gs[1, :])
    y_positions: np.ndarray                 = np.arange(len(bar_labels))
    base_rgb:    tuple[float, float, float] = mcolors.to_rgb(_QUALITY_COLOR)
    colors: list[tuple[float, ...]] = [
        tuple(c * (1 - mix) + mix for c in base_rgb)
        for mix in [0.15 + 0.40 * i / (len(bar_labels) - 1) for i in range(len(bar_labels))]
    ]

    for i, (pos, val, _label) in enumerate(zip(y_positions, stat_values, bar_labels)):
        if not (np.isnan(val) or np.isinf(val)):
            ax_stats.barh(pos, val, color=colors[i], alpha=_BAR_ALPHA, height=_BAR_HEIGHT)
            ax_stats.text(val, pos, f" {val:.1f}",
                          va="center", ha="left", fontsize=_FONT_BAR_LABEL, color=_QUALITY_COLOR)

    ax_stats.set_yticks(y_positions)
    ax_stats.set_yticklabels(bar_labels, fontsize=_FONT_AXIS_TICKS)
    ax_stats.set_xlabel(quality_label, fontsize=_FONT_SUBPLOT_XLABEL, fontweight="bold", color=_QUALITY_COLOR)
    ax_stats.set_title(f"{quality_label} Distribution", fontsize=_FONT_SUBPLOT_TITLE, fontweight="bold", color=_QUALITY_COLOR)
    ax_stats.tick_params(axis="x", labelcolor=_QUALITY_COLOR, labelsize=_FONT_AXIS_TICKS_X)
    ax_stats.tick_params(axis="y", labelsize=_FONT_AXIS_TICKS)
    ax_stats.set_xlim(_QUALITY_Y_MIN, _QUALITY_Y_MAX)
    ax_stats.grid(True, axis="x", alpha=_GRID_ALPHA_MAJOR, zorder=0)
    ax_stats.set_axisbelow(True)

    # Summary box — fig.text after tight_layout
    summary_text = (
        f"{quality_label}:\n"
        f"  Chunks: {len(chunks)}\n"
        f"  Min: {quality_stats[StatisticType.MIN]:>5.1f}\n"
        f"  Med: {quality_stats[StatisticType.MEDIAN]:>5.1f}\n"
        f"  Max: {quality_stats[StatisticType.MAX]:>5.1f}\n"
        f"  Std: {quality_stats[StatisticType.STD]:>5.1f}"
    )

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore",
                                message="This figure includes Axes that are not compatible with tight_layout")
        plt.tight_layout(pad=_TIGHT_LAYOUT_PAD)

    axes_to_fig = ax_left.transAxes + fig.transFigure.inverted()
    fx, fy      = axes_to_fig.transform((_SUMMARY_BOX_START_X, _SUMMARY_BOX_Y_POS))
    fig.text(
        fx, fy, summary_text,
        transform         = fig.transFigure,
        fontsize          = _FONT_SUMMARY_BOX,
        verticalalignment = "bottom",
        bbox              = {"boxstyle": "round", "facecolor": _QUALITY_COLOR, "alpha": _SUMMARY_BOX_METRIC_ALPHA},
        family            = "monospace",
    )

    fig.set_size_inches(_FIG_WIDTH, _FIG_HEIGHT)
    fig.savefig(output_path, dpi=_PLOT_DPI)
    plt.close(fig)
    logger.debug("Quality plot saved to %s", output_path)


class QualityMeasurer:
    """Measures encoded chunks against a reference (measurement only).

    This class integrates with the metric runner for metric generation and
    metrics_visualization for parsing and plotting.
    """

    def __init__(self, work_dir: Path) -> None:
        """Initialize quality measurer.

        Args:
            work_dir: Working directory for metric artifacts
        """
        self.work_dir: Path = work_dir

    async def _generate_metrics(
        self,
        encoded:          Path,
        reference:        FFmpegInput,
        ref_crop:         CropParams,
        output_prefix:    str,
        sampling:          int                            = 3,
        bar_advance:      BarAdvance | None = None,
        duration_seconds: float                          = 0.0,
        width:            int                            = 0,
        cwd:              Path | None                    = None,
        fps_value:        Fraction | None                = None,
    ) -> tuple[QualityLogs, int]:
        """Generate metric log files for quality comparison.

        Runs all four metrics (PSNR, SSIM, VMAF, VIF) in a single ffmpeg pass
        via ``run_metrics``.  Each metric is written to a ``.tmp``-suffixed file;
        the ``.tmp`` paths are returned directly in a ``QualityLogs`` instance
        — no rename is performed.

        Args:
            encoded:          Path to encoded video file (read whole).
            reference:        The reference as a runner input — a chunk window
                              arrives as ``chunk.as_input()`` (per-input
                              ``-ss``/``-t``).
            ref_crop:         Crop parameters for the reference input.
            output_prefix:    Full path prefix for metric files (unused for path
                              derivation — ``cwd`` and a UUID prefix are used instead).
            sampling:          Frame subsampling factor (applied to VMAF via
                              ``n_subsample`` and to PSNR/SSIM via ``select``).
            bar_advance:      Optional callable that advances the progress bar.
            duration_seconds: Duration of the comparison window in seconds.
            width:            Scale both inputs to this width (0 = no scaling).
            cwd:              Working directory for metric ``.tmp`` files.  When
                              ``None``, derived from ``output_prefix``'s parent.

        Returns:
            ``(QualityLogs, frame_count)`` — the ``.tmp`` paths for all four
            metrics (``plot`` is ``None`` — filled in by ``_finish_measurement``)
            and the measured file's frame count from the run's progress (the
            distorted input is decoded whole; subsampling reduces scoring
            only, so the count is exact; ``0`` = not determined).
        """
        uuid_hex   = uuid.uuid4().hex
        output_dir = cwd if cwd is not None else Path(output_prefix).parent
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(
            "Generating metrics for %s vs %s (tmp prefix: %s)",
            encoded.name, Path(str(reference.path)).name, uuid_hex,
        )

        # Single ffmpeg pass — progress is linear: total = duration_seconds, weight = 1.0.
        _weight = 1.0

        def _progress_callback(frame: int, out_time_s: float) -> None:
            if bar_advance is None or duration_seconds <= 0:
                return
            nonlocal _last_time
            delta = max(0.0, out_time_s - _last_time)
            delta = min(delta, duration_seconds - _last_time)
            if delta > 0:
                bar_advance(delta * _weight, AdvanceState.PROGRESS)
                _last_time = out_time_s

        _last_time: float = 0.0
        tmp_prefix        = f"{uuid_hex}."

        result = await run_metrics(
            metrics          = _metrics_all,
            distorted        = FFmpegInput(path=encoded),
            reference        = reference,
            crop_distorted   = CropParams(),
            crop_reference   = ref_crop,
            width            = width,
            use_gpu          = False,
            sampling         = sampling,
            output_prefix    = tmp_prefix,
            cwd              = output_dir,
            progress_callback = _progress_callback if bar_advance is not None else None,
            output_extension = ".tmp",
            fps              = float(fps_value) if fps_value is not None else None,
        )
        if not result.success:
            logger.warning("Metrics run had non-zero exit code: %d", result.returncode)

        # Build artifact paths — .tmp files stay as-is, no rename. VIF is
        # embedded in the VMAF JSON (feature=name=vif), so vif_log shares the
        # vmaf_json path.
        vmaf_tmp = output_dir / f"{tmp_prefix}{MetricType.VMAF.value}.tmp"
        artifacts = QualityLogs(
            psnr_log  = output_dir / f"{tmp_prefix}{MetricType.PSNR.value}.tmp",
            ssim_log  = output_dir / f"{tmp_prefix}{MetricType.SSIM.value}.tmp",
            vmaf_json = vmaf_tmp,
            vif_log   = vmaf_tmp,
            plot      = None,
        )

        # Warn for any missing tmp file (ffmpeg failure); distinct paths only.
        checked: set[Path] = set()
        for attr, path in [
            ("psnr_log",  artifacts.psnr_log),
            ("ssim_log",  artifacts.ssim_log),
            ("vmaf_json", artifacts.vmaf_json),
        ]:
            if path is not None and path not in checked:
                checked.add(path)
                if not path.exists():
                    logger.warning("Expected metric tmp file not found: %s", path)

        # None → 0 (the unknown sentinel) at the typed boundary.
        return artifacts, result.frame_count or 0

    async def measure_chunk_async(
        self,
        encoded:             Path,
        reference:           FFmpegInput,
        ref_crop:            CropParams,
        output_dir:          Path,
        duration_seconds:    float,
        fps_value:           Fraction | None,
        sampling:            int               = 10,
        show_progress:       bool | BarAdvance = False,
        plot_path:           Path | None       = None,
        chunk_start_seconds: float             = 0.0,
        width:               int               = 0,
        bar_title:           str | None        = None,
        metrics_output_dir:  Path | None       = None,
    ) -> QualityMeasurement:
        """Async variant of ``measure_chunk`` for use inside a running event loop.

        Identical behaviour to ``measure_chunk`` but ``await``s ``_generate_metrics``
        directly instead of calling ``asyncio.run()``.  Use this when already inside
        an ``async`` context (e.g. ``run_measure``).

        Args:
            bar_title:          Override the progress bar title.  When ``None``,
                                defaults to the encoded file stem.
            metrics_output_dir: Directory for raw metric ``.tmp`` files.  When
                                ``None``, uses ``output_dir``.
            (all other args same as ``measure_chunk``)

        Returns:
            QualityMeasurement with the measured metrics (no verdict —
            Req 51-53: verdicts are pure functions applied by callers).
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        cwd = metrics_output_dir if metrics_output_dir is not None else output_dir

        resolved_title    = bar_title if bar_title is not None \
            else encoded.stem.replace(TIME_SEPARATOR_MS, ".").replace(TIME_SEPARATOR_SAFE, ":")
        _total_complexity = duration_seconds or 0.0  # single ffmpeg run, linear time

        if show_progress is True:
            with ProgressBar(_total_complexity, title=f"Metrics: {resolved_title}", show_counters=False) as advance:
                artifacts, run_frames = await self._generate_metrics(
                    encoded, reference, ref_crop,
                    output_prefix    = str(cwd / f"{encoded.stem}."),
                    sampling        = sampling,
                    bar_advance      = advance,
                    duration_seconds = duration_seconds or 0.0,
                    width            = width,
                    cwd              = cwd,
                    fps_value        = fps_value,
                )
                advance(0, AdvanceState.COMPLETE)
        elif callable(show_progress):
            # Outer bar supplied by caller — pass through directly; the caller
            # owns the bar's lifecycle and must NOT receive a COMPLETE signal here.
            artifacts, run_frames = await self._generate_metrics(
                encoded, reference, ref_crop,
                output_prefix    = str(cwd / f"{encoded.stem}."),
                sampling        = sampling,
                bar_advance      = show_progress,
                duration_seconds = duration_seconds or 0.0,
                width            = width,
                cwd              = cwd,
                fps_value        = fps_value,
            )
        else:
            artifacts, run_frames = await self._generate_metrics(
                encoded, reference, ref_crop,
                output_prefix    = str(cwd / f"{encoded.stem}."),
                sampling        = sampling,
                bar_advance      = None,
                duration_seconds = duration_seconds or 0.0,
                width            = width,
                cwd              = cwd,
                fps_value        = fps_value,
            )

        return self._finish_measurement(
            encoded             = encoded,
            artifacts           = artifacts,
            output_dir          = output_dir,
            sampling            = sampling,
            plot_path           = plot_path,
            fps_value           = fps_value,
            chunk_start_seconds = chunk_start_seconds,
            frame_count         = run_frames,
        )

    def measure_chunk(
        self,
        encoded:             Path,
        reference:           FFmpegInput,
        ref_crop:            CropParams,
        output_dir:          Path,
        duration_seconds:    float,
        fps_value:           Fraction | None,
        sampling:            int               = 10,
        show_progress:       bool | BarAdvance = False,
        plot_path:           Path | None       = None,
        chunk_start_seconds: float             = 0.0,
        width:               int               = 0,
        metrics_output_dir:  Path | None       = None,
    ) -> QualityMeasurement:
        """Measure encoded chunk against a reference (no verdict - Req 51-53).

        Thin sync wrapper over :meth:`measure_chunk_async` (the single
        implementation): runs the coroutine on a fresh event loop.  Use only
        from sync code with no running loop — callers already inside an
        ``async`` context must ``await`` the async twin instead.

        Args:
            encoded:             Path to encoded video file (read whole).
            reference:           The reference as a runner input — a chunk
                                 window arrives as ``chunk.as_input()``.
            ref_crop:            Crop parameters for the reference input.
            output_dir:          Directory for the plot PNG.
            duration_seconds:    The comparison window's duration — supplied by
                                 the caller from the chunk window, never probed.
            fps_value:           Average fps for plot x-axis conversion
                                 (``fps_fraction`` of the source stream).
            sampling:             Frame subsampling factor for metrics.
            show_progress:       ``True`` — open an inner progress bar;
                                 ``BarAdvance`` callable — feed an outer bar
                                 supplied by the caller (no inner bar created);
                                 ``False`` (default) — no progress.
            plot_path:           Explicit path for the PNG plot.  When ``None``,
                                 written as ``<encoded.stem>.png`` inside ``output_dir``.
            chunk_start_seconds: Start timestamp of the chunk in seconds.
            width:               Scale both inputs to this width (0 = no scaling).
            metrics_output_dir:  Directory for raw metric ``.tmp`` files.  When
                                 ``None``, uses ``output_dir``.

        Returns:
            QualityMeasurement with the measured metrics (no verdict —
            Req 51-53: verdicts are pure functions applied by callers).
        """
        return asyncio.run(
            self.measure_chunk_async(
                encoded             = encoded,
                reference           = reference,
                ref_crop            = ref_crop,
                output_dir          = output_dir,
                duration_seconds    = duration_seconds,
                fps_value           = fps_value,
                sampling            = sampling,
                show_progress       = show_progress,
                plot_path           = plot_path,
                chunk_start_seconds = chunk_start_seconds,
                width               = width,
                metrics_output_dir  = metrics_output_dir,
            )
        )

    def _finish_measurement(
        self,
        encoded:             Path,
        artifacts:           QualityLogs,
        output_dir:          Path,
        sampling:            int,
        plot_path:           Path | None,
        fps_value:           Fraction | None,
        chunk_start_seconds: float,
        frame_count:         int = 0,
    ) -> QualityMeasurement:
        """Parse metric files, generate the plot (measurement completion).

        Shared post-processing used by both ``measure_chunk`` and
        ``measure_chunk_async`` after metric files have been produced. Each
        metric's own ``parse_log`` yields its per-frame values; the shared
        statistics step (this measurer) normalizes, indexes, and quantizes
        them into the flat measured set.

        Args:
            encoded:             Path to the encoded video (used for plot title/path).
            artifacts:           ``QualityLogs`` returned by ``_generate_metrics``.
            output_dir:          Directory for the plot PNG.
            sampling:            Frame subsampling factor.
            plot_path:           Explicit path override; auto-derived when ``None``.
            fps_value:           Frames per second for x-axis labelling.
            chunk_start_seconds: Chunk start offset for x-axis.
            frame_count:         The measured file's frame count from the
                                 metric run's progress (``0`` = not determined).

        Returns:
            ``QualityMeasurement`` with the flat measured set and artifact paths.

        Raises:
            ValueError: No metric file could be parsed.
        """
        logger.debug("Parsing metrics and generating plots")
        resolved_plot_path = plot_path if plot_path is not None else output_dir / f"{encoded.stem}.png"

        logs = QualityLogs(
            psnr_log  = artifacts.psnr_log  if artifacts.psnr_log  is not None and artifacts.psnr_log.exists()  else None,
            ssim_log  = artifacts.ssim_log  if artifacts.ssim_log  is not None and artifacts.ssim_log.exists()  else None,
            vmaf_json = artifacts.vmaf_json if artifacts.vmaf_json is not None and artifacts.vmaf_json.exists() else None,
            vif_log   = artifacts.vif_log   if artifacts.vif_log   is not None and artifacts.vif_log.exists()   else None,
        )
        paths: dict[MetricType, Path | None] = {
            MetricType.PSNR: logs.psnr_log,
            MetricType.SSIM: logs.ssim_log,
            MetricType.VMAF: logs.vmaf_json,
            MetricType.VIF:  logs.vif_log,
        }

        series:      dict[MetricType, pd.Series]                    = {}
        stats:       dict[MetricType, dict[StatisticType, float]]   = {}
        measured:    dict[MetricStatKey, float]                     = {}
        for metric_type in _metrics_all:
            path = paths[metric_type]
            if path is None:
                continue
            try:
                values = metric_type.info.parse_log(path)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("Failed to parse %s from %s: %s", metric_type.value, path, exc)
                continue
            # The subsample factor maps the i-th scored frame to source
            # frame i*factor for every metric: PSNR/SSIM logs count their
            # select-filtered output; VMAF/VIF report true frame numbers.
            raw = pd.Series(
                values,
                index = [i * sampling for i in range(len(values))],
                name  = metric_type.value,
                dtype = float,
            )
            normalized = cast(pd.Series, metric_type.info.normalize(raw))
            series[metric_type] = normalized
            stats[metric_type]  = _metric_statistics(normalized, metric_type.info)
            for statistic, value in stats[metric_type].items():
                measured[MetricStatKey(metric_type, statistic)] = value
            logger.debug("Parsed %s (%d frames)", metric_type.value, len(values))

        if not series:
            raise ValueError(
                "No metric files could be parsed from artifacts. "
                "At least one valid metric file (PSNR, SSIM, VMAF, or VIF) is required."
            )

        plot_title = (
            f"Quality metrics\n"
            f"{encoded.stem.replace(TIME_SEPARATOR_MS, '.').replace(TIME_SEPARATOR_SAFE, ':')}"
        )
        create_unified_plot(
            series_norm         = series,
            stats               = stats,
            factor              = sampling,
            output_path         = resolved_plot_path,
            title               = plot_title,
            fps                 = fps_value,
            chunk_start_seconds = chunk_start_seconds,
        )

        # Delete tmp files after both the parse and the plot have consumed
        # them; the set dedups the shared VIF/VMAF path.
        to_delete: set[Path] = {path for path in paths.values() if path is not None}
        for path in to_delete:
            try:
                path.unlink(missing_ok=True)
                logger.debug("Deleted raw metric tmp file: %s", path.name)
            except OSError as exc:
                logger.warning("Could not delete metric tmp file %s: %s", path.name, exc)

        artifacts.plot = resolved_plot_path

        return QualityMeasurement(
            metrics     = MeasuredMetrics(measured),
            logs        = artifacts,
            frame_count = frame_count,
        )
