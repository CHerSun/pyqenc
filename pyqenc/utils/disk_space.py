"""Disk space checking utilities."""
# CHerSun 2026

import logging
import shutil
from dataclasses import dataclass
from enum import StrEnum
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

class AvailableSpaceLevel(StrEnum):
    SUFFICIENT = "Sufficient"
    INSUFFICIENT = "Tight"
    TIGHT = "Warning"

@dataclass
class SpaceEstimate:
    """Estimated space requirements for pipeline."""
    source_size_gb: float
    min_estimated_required_gb: float
    max_estimated_required_gb: float
    available_gb: float
    sufficient: AvailableSpaceLevel


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
    min_strategies:       int          = 1,
    max_strategies:       int          = 1,
    include_optimization: bool         = False,
    chunking_mode:        ChunkingMode = ChunkingMode.LOSSLESS,
) -> SpaceEstimate:
    """Check if sufficient disk space is available for pipeline execution. Accounts for multiple strategies possibilities.

    Args:
        source_video:         Path to source video file.
        work_dir:             Working directory where files will be stored.
        num_strategies:       Number of encoding strategies.
        include_optimization: Whether optimization phase is enabled.
        chunking_mode:        Chunking strategy — affects chunk size estimate.

    Returns:
        tuple[SpaceEstimate, SpaceEstimate] with availability details min and max.
    """
    # Get source size
    if not source_video.exists():
        return SpaceEstimate(0.0, 0.0, 0.0, 0.0, AvailableSpaceLevel.INSUFFICIENT)

    source_size_gb = source_video.stat().st_size / (1024 ** 3)

    # Estimate minimal required space (min num strategies)
    estimated_required_gb_min = estimate_required_space(
        source_video,
        min_strategies,
        include_optimization,
        chunking_mode,
    )
    # Estimate maximal required space (max num strategies)
    estimated_required_gb_max = estimate_required_space(
        source_video,
        max_strategies,
        include_optimization,
        chunking_mode,
    )

    # Get available space on work_dir's filesystem
    # Create work_dir if it doesn't exist to check its filesystem
    work_dir.mkdir(parents=True, exist_ok=True)
    disk_info = get_disk_space(work_dir)

    # Check if sufficient space available
    min_required = estimated_required_gb_min
    recommended_required = estimated_required_gb_max * 1.2
    sufficient: bool = disk_info.free_gb >= min_required
    recommended: bool = disk_info.free_gb >= recommended_required
    level: AvailableSpaceLevel = AvailableSpaceLevel.INSUFFICIENT if not sufficient else AvailableSpaceLevel.TIGHT if not recommended else AvailableSpaceLevel.SUFFICIENT

    return SpaceEstimate(source_size_gb, min_required, recommended_required, disk_info.free_gb, level)



def log_disk_space_info(
    source_video:         Path,
    work_dir:             Path,
    min_num_strategies:   int          = 1,
    max_num_strategies:   int          = 1,
    include_optimization: bool         = False,
    chunking_mode:        ChunkingMode = ChunkingMode.LOSSLESS,
) -> AvailableSpaceLevel:
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
        min_num_strategies, max_num_strategies,
        include_optimization,
        chunking_mode,
    )

    # Log the results in a clear format
    kv_table = {
        "Source video size": f"{estimate.source_size_gb:.2f} GB",
        "Estimated required space": f"{estimate.min_estimated_required_gb:.2f} GB",
        "Estimated recommended space": f"{estimate.max_estimated_required_gb:.2f} GB",
        "Available space": f"{estimate.available_gb:.2f} GB",
    }
    fmt_key_value_table(kv_table)

    # Log warnings or success message for disk space
    logger.info("")
    if estimate.sufficient == AvailableSpaceLevel.INSUFFICIENT:
        logger.error("Insufficient disk space! Most likely you won't be able to finish processing. Consider freeing up more space or using `--cleanup` flag.")
    elif estimate.sufficient == AvailableSpaceLevel.TIGHT:
        logger.warning("Disk space is limited. Consider freeing up more space or using `--cleanup` flag.")
    elif estimate.sufficient == AvailableSpaceLevel.SUFFICIENT:
        logger.info(f"{SUCCESS_SYMBOL_MINOR} Sufficient disk space available.")
    logger.info("")

    return estimate.sufficient
