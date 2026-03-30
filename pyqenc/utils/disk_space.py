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
    BYTES_PER_PIXEL_FFV1,
    OVERHEAD_CHUNKING_LOSSLESS_FALLBACK,
    OVERHEAD_CHUNKING_REMUX,
    OVERHEAD_EXTRACTION_AND_AUDIO,
    OVERHEAD_PER_STRATEGY_FALLBACK,
    OVERHEAD_TIGHT_MARGIN,
    SUCCESS_SYMBOL_MINOR,
)
from pyqenc.models import ChunkingMode, VideoMetadata
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


def _estimate_total_pixels(video: VideoMetadata) -> int | None:
    """Derive total pixel count via VideoMetadata lazy properties.

    Uses cached ``frame_count`` when already available (exact), otherwise
    approximates from ``fps x duration_seconds`` (fast ffprobe — never triggers
    the slow null-encode probe).  Properties cache their results on first access.
    Returns ``None`` if insufficient data is available.
    """
    res = _parse_resolution(video.resolution) if video.resolution else None
    if res is None:
        return None

    # Use exact frame count only if already cached — intentionally bypass the
    # ``frame_count`` property to avoid triggering the slow null-encode probe (~2-3 s).
    # Falls back to fps * duration below, which is accurate enough for a space estimate.
    fc = video._frame_count  # noqa: SLF001
    if fc is None:
        fps      = video.fps
        duration = video.duration_seconds
        if fps is not None and duration is not None and fps > 0:
            fc = int(fps * duration)

    if fc is None:
        return None

    return res[0] * res[1] * fc


def estimate_required_space(
    video:          VideoMetadata,
    num_strategies: int          = 1,
    chunking_mode:  ChunkingMode = ChunkingMode.LOSSLESS,
) -> float:
    """Estimate required disk space for pipeline execution.

    Derives source path and file size directly from ``video``.  Uses
    pixel-based estimation when cached metadata fields are available, which is
    significantly more accurate than source-size multipliers.  Never triggers
    additional I/O probes — reads only already-cached private fields.

    Estimation components (pixel-based path):

    - Extraction + audio: ``source_size x OVERHEAD_EXTRACTION_AND_AUDIO``
      (audio folded into this rough multiplier — good enough without AudioMetadata)
    - FFV1 chunks:  ``total_pixels x BYTES_PER_PIXEL_FFV1``
    - Remux chunks: ``source_size x OVERHEAD_CHUNKING_REMUX``
    - Attempts:     ``total_pixels x (BITS_PER_PIXEL_ENCODED / 8) x AVG_ATTEMPTS_PER_CHUNK x num_strategies``
    - Final output: ``total_pixels x (BITS_PER_PIXEL_ENCODED / 8) x num_strategies``

    Falls back to source-size multipliers when pixel data is unavailable.

    Args:
        video:          ``VideoMetadata`` for the source file — provides path,
                        file size, and cached pixel data.
        num_strategies: Number of encoding strategies to estimate for.
        chunking_mode:  Chunking strategy — affects chunk size estimate.

    Returns:
        Estimated required space in GB.
    """
    size_bytes = video._file_size_bytes  # noqa: SLF001
    if size_bytes is None:
        size_bytes = video.file_size_bytes  # triggers one stat() if not cached
    if size_bytes is None:
        logger.warning("Cannot determine source file size for %s", video.path)
        return 0.0

    source_size_gb = size_bytes / (1024 ** 3)
    total_pixels   = _estimate_total_pixels(video)

    if total_pixels is not None:
        logger.debug("Space estimate: pixel-based (%d Mpx total)", total_pixels // 1_000_000)
        extraction_gb        = source_size_gb * OVERHEAD_EXTRACTION_AND_AUDIO
        bytes_per_encoded_px = BITS_PER_PIXEL_ENCODED / 8
        chunks_gb   = (total_pixels * BYTES_PER_PIXEL_FFV1 / (1024 ** 3)
                       if chunking_mode == ChunkingMode.LOSSLESS
                       else source_size_gb * OVERHEAD_CHUNKING_REMUX)
        attempts_gb = total_pixels * bytes_per_encoded_px * AVG_ATTEMPTS_PER_CHUNK * num_strategies / (1024 ** 3)
        final_gb    = total_pixels * bytes_per_encoded_px * num_strategies / (1024 ** 3)
        total_gb    = extraction_gb + chunks_gb + attempts_gb + final_gb
        logger.debug(
            "Space estimate breakdown: extraction=%.2f GB, chunks=%.2f GB, "
            "attempts=%.2f GB, final=%.2f GB -> total=%.2f GB",
            extraction_gb, chunks_gb, attempts_gb, final_gb, total_gb,
        )
        return total_gb

    logger.debug("Space estimate: falling back to source-size multipliers (no pixel data cached)")
    chunks_mult = OVERHEAD_CHUNKING_LOSSLESS_FALLBACK if chunking_mode == ChunkingMode.LOSSLESS else OVERHEAD_CHUNKING_REMUX
    return source_size_gb * (
        OVERHEAD_EXTRACTION_AND_AUDIO
        + chunks_mult
        + OVERHEAD_PER_STRATEGY_FALLBACK * num_strategies
    )


def check_disk_space(
    video:          VideoMetadata,
    work_dir:       Path,
    min_strategies: int          = 1,
    max_strategies: int          = 1,
    chunking_mode:  ChunkingMode = ChunkingMode.LOSSLESS,
) -> SpaceEstimate:
    """Check if sufficient disk space is available for pipeline execution.

    Calls ``estimate_required_space`` twice — once for the minimum strategy
    count (lower bound) and once for the maximum (upper bound).  The
    recommended threshold adds a ``OVERHEAD_TIGHT_MARGIN`` safety buffer on
    top of the upper-bound estimate.

    Args:
        video:          ``VideoMetadata`` for the source file.
        work_dir:       Working directory where files will be stored.
        min_strategies: Minimum number of strategies (lower-bound estimate).
        max_strategies: Maximum number of strategies (upper-bound estimate).
        chunking_mode:  Chunking strategy — affects chunk size estimate.

    Returns:
        ``SpaceEstimate`` with min/max required and available space.
    """
    source_size_gb  = (video._file_size_bytes or 0) / (1024 ** 3)  # noqa: SLF001
    min_required_gb = estimate_required_space(video, min_strategies, chunking_mode)
    max_required_gb = estimate_required_space(video, max_strategies, chunking_mode)
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
    video:          VideoMetadata,
    work_dir:       Path,
    min_strategies: int          = 1,
    max_strategies: int          = 1,
    chunking_mode:  ChunkingMode = ChunkingMode.LOSSLESS,
) -> AvailableSpaceLevel:
    """Check and log disk space information.

    When ``min_strategies == max_strategies`` (fixed strategy count), logs a
    single estimate value.  When they differ (optimization mode, where 1 to N
    strategies may run), logs a ``{min} ... {max} GB`` range so the user
    understands the uncertainty.

    Args:
        video:          ``VideoMetadata`` for the source file.
        work_dir:       Working directory where files will be stored.
        min_strategies: Minimum number of strategies (lower-bound estimate).
        max_strategies: Maximum number of strategies (upper-bound estimate).
        chunking_mode:  Chunking strategy — affects chunk size estimate.

    Returns:
        ``AvailableSpaceLevel`` indicating whether space is sufficient.
    """
    estimate = check_disk_space(video, work_dir, min_strategies, max_strategies, chunking_mode)

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
