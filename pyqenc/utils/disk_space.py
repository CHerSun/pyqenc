"""Disk space checking utilities."""
# CHerSun 2026

import logging
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pyqenc.constants import (
    AVG_ATTEMPTS_PER_CHUNK,
    BITS_PER_PIXEL_ENCODED,
    OVERHEAD_PER_STRATEGY_FALLBACK,
    OVERHEAD_TIGHT_MARGIN,
    SUCCESS_SYMBOL_MINOR,
)
from pyqenc.stream_model import VideoStream
from pyqenc.utils.log_format import fmt_key_value_table

logger = logging.getLogger(__name__)


@dataclass
class DiskSpaceInfo:
    """Information about disk space availability."""
    total_gb:     float
    used_gb:      float
    free_gb:      float
    percent_used: float


class AvailableSpaceLevel(StrEnum):
    SUFFICIENT   = "Sufficient"
    INSUFFICIENT = "Tight"
    TIGHT        = "Warning"


@dataclass
class SpaceEstimate:
    """Estimated space requirements for pipeline.

    Attributes:
        source_size_gb:  Size of the source video file in GB.
        min_required_gb: Lower-bound estimate (minimum strategies).
        max_required_gb: Upper-bound estimate with safety margin (maximum strategies).
        available_gb:    Free space on the work directory filesystem.
        level:           Whether available space is sufficient, tight, or insufficient.
    """
    source_size_gb:  float
    min_required_gb: float
    max_required_gb: float
    available_gb:    float
    level:           AvailableSpaceLevel


def get_disk_space(path: Path) -> DiskSpaceInfo:
    """Get disk space information for the given path.

    Args:
        path: Path to check disk space for.

    Returns:
        DiskSpaceInfo with disk space details.
    """
    usage        = shutil.disk_usage(path)
    total_gb     = usage.total / (1024 ** 3)
    used_gb      = usage.used  / (1024 ** 3)
    free_gb      = usage.free  / (1024 ** 3)
    percent_used = (usage.used / usage.total) * 100
    return DiskSpaceInfo(total_gb=total_gb, used_gb=used_gb, free_gb=free_gb, percent_used=percent_used)


def _parse_resolution(resolution: str) -> tuple[int, int] | None:
    """Parse a ``'WxH'`` resolution string into ``(width, height)``.

    Returns ``None`` if parsing fails.
    """
    try:
        w, h = resolution.split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return None


def _estimate_total_pixels(stream: VideoStream) -> int | None:
    """Derive total pixel count from the stream's real fast-facet data.

    Uses ``resolution`` and ``fps * duration_seconds`` from the enumerated
    :class:`~pyqenc.stream_model.VideoStreamInfo`; returns ``None`` if
    insufficient data is available.
    """
    res = _parse_resolution(stream.info.resolution) if stream.info.resolution else None
    if res is None:
        return None

    fps      = stream.info.fps
    duration = stream.info.duration_seconds
    if fps is None or duration is None or fps <= 0:
        return None

    return res[0] * res[1] * int(fps * duration)


def estimate_required_space(
    stream:         VideoStream,
    num_strategies: int = 1,
) -> float:
    """Estimate required disk space for pipeline execution.

    The direct-from-source pipeline materializes only encoding attempts and
    final outputs, so the estimate covers exactly those:

    - Attempts: ``total_pixels x (BITS_PER_PIXEL_ENCODED / 8) x AVG_ATTEMPTS_PER_CHUNK x num_strategies``
    - Final output: ``total_pixels x (BITS_PER_PIXEL_ENCODED / 8) x num_strategies``

    Pixel data comes from the enumerated :class:`~pyqenc.stream_model.VideoStreamInfo`;
    falls back to a source-size multiplier per strategy when it is unavailable.

    Args:
        stream:         The source's video stream — file size and pixel data.
        num_strategies: Number of encoding strategies to estimate for.

    Returns:
        Estimated required space in GB.
    """
    size_bytes = stream.file.file_size_bytes
    if size_bytes is None:
        logger.warning("Cannot determine source file size for %s", stream.file.path)
        return 0.0

    source_size_gb = size_bytes / (1024 ** 3)
    total_pixels   = _estimate_total_pixels(stream)

    if total_pixels is not None:
        logger.debug("Space estimate: pixel-based (%d Mpx total)", total_pixels // 1_000_000)
        bytes_per_encoded_px = BITS_PER_PIXEL_ENCODED / 8
        attempts_gb = total_pixels * bytes_per_encoded_px * AVG_ATTEMPTS_PER_CHUNK * num_strategies / (1024 ** 3)
        final_gb    = total_pixels * bytes_per_encoded_px * num_strategies / (1024 ** 3)
        total_gb    = attempts_gb + final_gb
        logger.debug(
            "Space estimate breakdown: attempts=%.2f GB, final=%.2f GB -> total=%.2f GB",
            attempts_gb, final_gb, total_gb,
        )
        return total_gb

    logger.debug("Space estimate: falling back to source-size multipliers (no pixel data)")
    return source_size_gb * OVERHEAD_PER_STRATEGY_FALLBACK * num_strategies


def check_disk_space(
    stream:         VideoStream,
    work_dir:       Path,
    min_strategies: int = 1,
    max_strategies: int = 1,
) -> SpaceEstimate:
    """Check if sufficient disk space is available for pipeline execution.

    Calls ``estimate_required_space`` twice — once for the minimum strategy
    count (lower bound) and once for the maximum (upper bound).  The
    recommended threshold adds a ``OVERHEAD_TIGHT_MARGIN`` safety buffer on
    top of the upper-bound estimate.

    Args:
        stream:         The source's video stream.
        work_dir:       Working directory where files will be stored.
        min_strategies: Minimum number of strategies (lower-bound estimate).
        max_strategies: Maximum number of strategies (upper-bound estimate).

    Returns:
        ``SpaceEstimate`` with min/max required and available space.
    """
    min_required_gb = estimate_required_space(stream, min_strategies)
    max_required_gb = estimate_required_space(stream, max_strategies)
    source_size_gb  = (stream.file.file_size_bytes or 0) / (1024 ** 3)
    recommended_gb  = max_required_gb * OVERHEAD_TIGHT_MARGIN

    work_dir.mkdir(parents=True, exist_ok=True)
    disk_info = get_disk_space(work_dir)

    sufficient:  bool = disk_info.free_gb >= min_required_gb
    recommended: bool = disk_info.free_gb >= recommended_gb
    level = (
        AvailableSpaceLevel.INSUFFICIENT if not sufficient  else
        AvailableSpaceLevel.TIGHT        if not recommended else
        AvailableSpaceLevel.SUFFICIENT
    )

    return SpaceEstimate(
        source_size_gb  = source_size_gb,
        min_required_gb = min_required_gb,
        max_required_gb = recommended_gb,
        available_gb    = disk_info.free_gb,
        level           = level,
    )


def log_disk_space_info(
    stream:         VideoStream,
    work_dir:       Path,
    min_strategies: int = 1,
    max_strategies: int = 1,
) -> AvailableSpaceLevel:
    """Check and log disk space information.

    When ``min_strategies == max_strategies`` (fixed strategy count), logs a
    single estimate value.  When they differ (optimization mode, where 1 to N
    strategies may run), logs a ``{min} ... {max} GB`` range so the user
    understands the uncertainty.

    Args:
        stream:         The source's video stream.
        work_dir:       Working directory where files will be stored.
        min_strategies: Minimum number of strategies (lower-bound estimate).
        max_strategies: Maximum number of strategies (upper-bound estimate).

    Returns:
        ``AvailableSpaceLevel`` indicating whether space is sufficient.
    """
    estimate = check_disk_space(stream, work_dir, min_strategies, max_strategies)

    is_range = min_strategies != max_strategies
    if is_range:
        # max_required_gb has the margin baked in; strip it back for the raw upper bound display.
        raw_max         = estimate.max_required_gb / OVERHEAD_TIGHT_MARGIN
        required_str    = f"{estimate.min_required_gb:.2f} ... {raw_max:.2f} GB"
        recommended_str = f"{estimate.min_required_gb * OVERHEAD_TIGHT_MARGIN:.2f} ... {estimate.max_required_gb:.2f} GB"
    else:
        required_str    = f"{estimate.min_required_gb:.2f} GB"
        recommended_str = f"{estimate.max_required_gb:.2f} GB"

    kv_table = {
        "Source video size":           f"{estimate.source_size_gb:.2f} GB",
        "Estimated required space":    required_str,
        "Estimated recommended space": recommended_str,
        "Available space":             f"{estimate.available_gb:.2f} GB",
    }
    fmt_key_value_table(kv_table)

    logger.info("")
    if estimate.level == AvailableSpaceLevel.INSUFFICIENT:
        logger.error("Insufficient disk space! Most likely you won't be able to finish processing. Consider freeing up more space or using `--cleanup` flag.")
    elif estimate.level == AvailableSpaceLevel.TIGHT:
        logger.warning("Disk space is limited. Consider freeing up more space or using `--cleanup` flag.")
    elif estimate.level == AvailableSpaceLevel.SUFFICIENT:
        logger.info("%s Sufficient disk space available.", SUCCESS_SYMBOL_MINOR)
    logger.info("")

    return estimate.level

