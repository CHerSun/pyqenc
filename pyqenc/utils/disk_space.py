"""Disk space checking utilities."""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from pyqenc.constants import (
    OVERHEAD_CHUNKING_LOSSLESS,
    OVERHEAD_CHUNKING_REMUX,
    OVERHEAD_EXTRACTION_AND_AUDIO,
    OVERHEAD_FOR_OPTIMIZATION,
    OVERHEAD_PER_STRATEGY,
    OVERHEAD_TIGHT_MARGIN,
    SUCCESS_SYMBOL_MINOR,
)
from pyqenc.models import ChunkingMode
from pyqenc.utils.log_format import fmt_key_value_table

logger = logging.getLogger(__name__)


@dataclass
class DiskSpaceInfo:
    """Information about disk space availability."""
    total_gb: float
    used_gb: float
    free_gb: float
    percent_used: float


@dataclass
class SpaceEstimate:
    """Estimated space requirements for pipeline."""
    source_size_gb: float
    estimated_required_gb: float
    available_gb: float
    sufficient: bool
    warning_message: str | None = None


def get_disk_space(path: Path) -> DiskSpaceInfo:
    """Get disk space information for the given path.

    Args:
        path: Path to check disk space for

    Returns:
        DiskSpaceInfo with disk space details
    """
    usage = shutil.disk_usage(path)

    total_gb = usage.total / (1024 ** 3)
    used_gb = usage.used / (1024 ** 3)
    free_gb = usage.free / (1024 ** 3)
    percent_used = (usage.used / usage.total) * 100

    return DiskSpaceInfo(
        total_gb=total_gb,
        used_gb=used_gb,
        free_gb=free_gb,
        percent_used=percent_used
    )


def estimate_required_space(
    source_video:         Path,
    num_strategies:       int          = 1,
    include_optimization: bool         = False,
    chunking_mode:        ChunkingMode = ChunkingMode.LOSSLESS,
) -> float:
    """Estimate required disk space for pipeline execution.

    Estimation formula:

    - Extraction + audio overhead: ~1.2x source size
    - Chunks:
        - lossless FFV1 all-intra: ~5.0x source size
        - remux stream-copy:       ~1.0x source size
    - Per strategy overhead:       ~2.5x source size per strategy (encoding attempts, metrics, final output)

    Args:
        source_video:         Path to source video file.
        num_strategies:       Number of encoding strategies.
        include_optimization: Whether optimization phase is enabled.
        chunking_mode:        Chunking strategy — affects chunk size estimate.

    Returns:
        Estimated required space in GB.
    """
    if not source_video.exists():
        logger.warning(f"Source video not found: {source_video}")
        return 0.0

    source_size_gb = source_video.stat().st_size / (1024 ** 3)

    chunks_multiplier = (
        OVERHEAD_CHUNKING_LOSSLESS
        if chunking_mode == ChunkingMode.LOSSLESS
        else OVERHEAD_CHUNKING_REMUX
    )

    return source_size_gb * (
        OVERHEAD_EXTRACTION_AND_AUDIO + chunks_multiplier               # extraction and chunking overheads
        + OVERHEAD_PER_STRATEGY * num_strategies                        # per-strategy overhead scales with number of strategies
        + OVERHEAD_FOR_OPTIMIZATION if include_optimization else 0.0    # optimization overhead if enabled
    )


def check_disk_space(
    source_video:         Path,
    work_dir:             Path,
    num_strategies:       int          = 1,
    include_optimization: bool         = False,
    chunking_mode:        ChunkingMode = ChunkingMode.LOSSLESS,
) -> SpaceEstimate:
    """Check if sufficient disk space is available for pipeline execution.

    Args:
        source_video:         Path to source video file.
        work_dir:             Working directory where files will be stored.
        num_strategies:       Number of encoding strategies.
        include_optimization: Whether optimization phase is enabled.
        chunking_mode:        Chunking strategy — affects chunk size estimate.

    Returns:
        SpaceEstimate with availability details.
    """
    # Get source size
    if not source_video.exists():
        return SpaceEstimate(
            source_size_gb=0.0,
            estimated_required_gb=0.0,
            available_gb=0.0,
            sufficient=False,
            warning_message=f"Source video not found: {source_video}"
        )

    source_size_gb = source_video.stat().st_size / (1024 ** 3)

    # Estimate required space
    estimated_required_gb = estimate_required_space(
        source_video,
        num_strategies,
        include_optimization,
        chunking_mode,
    )

    # Get available space on work_dir's filesystem
    # Create work_dir if it doesn't exist to check its filesystem
    work_dir.mkdir(parents=True, exist_ok=True)
    disk_info = get_disk_space(work_dir)

    # Check if sufficient space available
    # Add 10% safety margin
    required_with_margin = estimated_required_gb * 1.1
    sufficient = disk_info.free_gb >= required_with_margin

    # Generate warning message if insufficient
    warning_message = None
    if not sufficient:                                                          # Warn if space is insufficient
        warning_message = (
            f"Insufficient disk space! "
            f"Required: {required_with_margin:.2f} GB, "
            f"Available: {disk_info.free_gb:.2f} GB, "
            f"Shortfall: {required_with_margin - disk_info.free_gb:.2f} GB"
        )
    elif disk_info.free_gb < required_with_margin * OVERHEAD_TIGHT_MARGIN:      # Warn if space is tight
        warning_message = (
            f"Disk space is limited. "
            f"Required: {required_with_margin:.2f} GB, "
            f"Available: {disk_info.free_gb:.2f} GB. "
            f"Consider freeing up space or using a different work directory."
        )

    return SpaceEstimate(
        source_size_gb=source_size_gb,
        estimated_required_gb=estimated_required_gb,
        available_gb=disk_info.free_gb,
        sufficient=sufficient,
        warning_message=warning_message
    )


def log_disk_space_info(
    source_video:         Path,
    work_dir:             Path,
    num_strategies:       int          = 1,
    include_optimization: bool         = False,
    chunking_mode:        ChunkingMode = ChunkingMode.LOSSLESS,
) -> bool:
    """Check and log disk space information.

    Args:
        source_video:         Path to source video file.
        work_dir:             Working directory where files will be stored.
        num_strategies:       Number of encoding strategies.
        include_optimization: Whether optimization phase is enabled.
        chunking_mode:        Chunking strategy — affects chunk size estimate.

    Returns:
        True if sufficient space available, False otherwise.
    """
    estimate = check_disk_space(
        source_video,
        work_dir,
        num_strategies,
        include_optimization,
        chunking_mode,
    )

    # Log the results in a clear format
    kv_table = {
        "Source video size": f"{estimate.source_size_gb:.2f} GB",
        "Estimated required space": f"{estimate.estimated_required_gb:.2f} GB",
        "Available space": f"{estimate.available_gb:.2f} GB",
    }
    fmt_key_value_table(kv_table)
    logger.info("")

    # Log warnings or success message for disk space
    if estimate.warning_message:
        if not estimate.sufficient:
            logger.critical(estimate.warning_message)
        else:
            logger.warning(estimate.warning_message)
    else:
        logger.info("%s Sufficient disk space available", SUCCESS_SYMBOL_MINOR)

    return estimate.sufficient
