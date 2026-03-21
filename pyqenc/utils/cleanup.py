"""Cleanup utilities for managing intermediate files."""
# CHerSun 2026

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CleanupStats:
    """Statistics about cleanup operation."""
    chunks_removed: int
    encoded_removed: int
    metrics_kept: int
    space_freed_mb: float
    success: bool
    error: str | None = None


def estimate_cleanup_space(work_dir: Path, keep_metrics: bool = True) -> float:
    """Estimate disk space that would be freed by cleanup.

    Args:
        work_dir: Working directory containing intermediate files
        keep_metrics: If True, keep metric files and plots

    Returns:
        Estimated space in MB that would be freed
    """
    total_size = 0

    # Chunks directory
    chunks_dir = work_dir / "chunks"
    if chunks_dir.exists():
        for chunk_file in chunks_dir.glob("chunk_*.mkv"):
            total_size += chunk_file.stat().st_size

    # Encoded chunks (but not metrics if keeping them)
    encoded_dir = work_dir / "encoded"
    if encoded_dir.exists():
        for strategy_dir in encoded_dir.iterdir():
            if not strategy_dir.is_dir():
                continue

            for encoded_file in strategy_dir.glob("chunk_*_attempt_*.mkv"):
                total_size += encoded_file.stat().st_size

            # If not keeping metrics, count those too
            if not keep_metrics:
                for metric_file in strategy_dir.glob("chunk_*_attempt_*.*"):
                    if metric_file.suffix in [".log", ".json", ".stats", ".png"]:
                        total_size += metric_file.stat().st_size

    return total_size / (1024 * 1024)  # Convert to MB


def cleanup_intermediate_files(
    work_dir: Path,
    keep_metrics: bool = True,
    dry_run: bool = False
) -> CleanupStats:
    """Clean up intermediate files after successful pipeline completion.

    Removes:
    - Original chunks (work_dir/chunks/)
    - Encoded chunk attempts (work_dir/encoded/*/chunk_*.mkv)

    Keeps:
    - Final output files (work_dir/final/)
    - Progress tracker (work_dir/progress.json)
    - Extracted streams (work_dir/extracted/) - may be useful for re-runs
    - Processed audio (work_dir/audio/) - may be useful for re-runs
    - Metrics and plots (if keep_metrics=True)

    Args:
        work_dir: Working directory containing intermediate files
        keep_metrics: If True, keep metric files and plots (default: True)
        dry_run: If True, only report what would be cleaned without removing

    Returns:
        CleanupStats with operation results
    """
    chunks_removed = 0
    encoded_removed = 0
    metrics_kept = 0
    space_freed = 0

    try:
        # Clean up chunks directory
        chunks_dir = work_dir / "chunks"
        if chunks_dir.exists():
            chunk_files = list(chunks_dir.glob("chunk_*.mkv"))
            for chunk_file in chunk_files:
                size = chunk_file.stat().st_size
                if dry_run:
                    logger.info(f"[DRY-RUN] Would remove: {chunk_file} ({size / 1024 / 1024:.2f} MB)")
                else:
                    logger.debug(f"Removing chunk: {chunk_file}")
                    chunk_file.unlink()
                chunks_removed += 1
                space_freed += size

            # Remove empty chunks directory
            if not dry_run and not any(chunks_dir.iterdir()):
                chunks_dir.rmdir()
                logger.debug(f"Removed empty directory: {chunks_dir}")

        # Clean up encoded chunks (but optionally keep metrics)
        encoded_dir = work_dir / "encoded"
        if encoded_dir.exists():
            for strategy_dir in encoded_dir.iterdir():
                if not strategy_dir.is_dir():
                    continue

                # Remove encoded chunk files
                for encoded_file in strategy_dir.glob("chunk_*_attempt_*.mkv"):
                    size = encoded_file.stat().st_size
                    if dry_run:
                        logger.info(f"[DRY-RUN] Would remove: {encoded_file} ({size / 1024 / 1024:.2f} MB)")
                    else:
                        logger.debug(f"Removing encoded chunk: {encoded_file}")
                        encoded_file.unlink()
                    encoded_removed += 1
                    space_freed += size

                # Handle metrics files
                if keep_metrics:
                    # Count metrics files we're keeping
                    for metric_file in strategy_dir.glob("chunk_*_attempt_*.*"):
                        if metric_file.suffix in [".log", ".json", ".stats", ".png"]:
                            metrics_kept += 1
                    logger.debug(f"Keeping {metrics_kept} metric files in {strategy_dir}")
                else:
                    # Remove metrics files too
                    for metric_file in strategy_dir.glob("chunk_*_attempt_*.*"):
                        if metric_file.suffix in [".log", ".json", ".stats", ".png"]:
                            size = metric_file.stat().st_size
                            if dry_run:
                                logger.info(f"[DRY-RUN] Would remove: {metric_file}")
                            else:
                                logger.debug(f"Removing metric file: {metric_file}")
                                metric_file.unlink()
                            space_freed += size

                # Remove empty strategy directory if no metrics kept
                if not dry_run and not keep_metrics and not any(strategy_dir.iterdir()):
                    strategy_dir.rmdir()
                    logger.debug(f"Removed empty directory: {strategy_dir}")

            # Remove empty encoded directory if no metrics kept
            if not dry_run and not keep_metrics and not any(encoded_dir.iterdir()):
                encoded_dir.rmdir()
                logger.debug(f"Removed empty directory: {encoded_dir}")

        space_freed_mb = space_freed / (1024 * 1024)

        if dry_run:
            logger.info(f"[DRY-RUN] Would free approximately {space_freed_mb:.2f} MB")
        else:
            logger.info(f"Cleanup completed: freed {space_freed_mb:.2f} MB")

        return CleanupStats(
            chunks_removed=chunks_removed,
            encoded_removed=encoded_removed,
            metrics_kept=metrics_kept,
            space_freed_mb=space_freed_mb,
            success=True,
            error=None
        )

    except Exception as e:
        logger.error(f"Cleanup failed: {e}", exc_info=True)
        return CleanupStats(
            chunks_removed=chunks_removed,
            encoded_removed=encoded_removed,
            metrics_kept=metrics_kept,
            space_freed_mb=space_freed / (1024 * 1024) if space_freed > 0 else 0,
            success=False,
            error=str(e)
        )


def prompt_cleanup(work_dir: Path) -> bool:
    """Prompt user for cleanup confirmation.

    Args:
        work_dir: Working directory containing intermediate files

    Returns:
        True if user confirms cleanup, False otherwise
    """
    # Estimate space that would be freed
    space_with_metrics = estimate_cleanup_space(work_dir, keep_metrics=True)
    space_without_metrics = estimate_cleanup_space(work_dir, keep_metrics=False)

    print()
    print("=" * 80)
    print("Pipeline completed successfully!")
    print("=" * 80)
    print()
    print("Intermediate files can now be cleaned up to free disk space:")
    print(f"  - Keep metrics (logs, plots): ~{space_with_metrics:.2f} MB freed")
    print(f"  - Remove all intermediate files: ~{space_without_metrics:.2f} MB freed")
    print()
    print("Files that will be kept:")
    print(f"  - Final output: {work_dir / 'final'}")
    print(f"  - Progress tracker: {work_dir / 'progress.json'}")
    print(f"  - Extracted streams: {work_dir / 'extracted'} (for re-runs)")
    print(f"  - Processed audio: {work_dir / 'audio'} (for re-runs)")
    print()

    while True:
        response = input("Clean up intermediate files? [k]eep metrics / [a]ll / [n]o: ").strip().lower()

        if response in ["k", "keep"]:
            return "keep_metrics"
        elif response in ["a", "all"]:
            return "remove_all"
        elif response in ["n", "no", ""]:
            return "no_cleanup"
        else:
            print("Invalid response. Please enter 'k', 'a', or 'n'.")
