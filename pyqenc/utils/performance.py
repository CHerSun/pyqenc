"""Performance profiling and optimization utilities."""
# CHerSun 2026

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """Performance metrics for a specific operation."""
    operation: str
    count: int = 0
    total_time: float = 0.0
    min_time: float = float('inf')
    max_time: float = 0.0

    def add_measurement(self, duration: float) -> None:
        """Add a timing measurement.

        Args:
            duration: Duration in seconds
        """
        self.count += 1
        self.total_time += duration
        self.min_time = min(self.min_time, duration)
        self.max_time = max(self.max_time, duration)

    @property
    def avg_time(self) -> float:
        """Calculate average time per operation."""
        return self.total_time / self.count if self.count > 0 else 0.0

    def __str__(self) -> str:
        """String representation of metrics."""
        if self.count == 0:
            return f"{self.operation}: No measurements"

        return (
            f"{self.operation}: "
            f"count={self.count}, "
            f"total={self.total_time:.3f}s, "
            f"avg={self.avg_time:.3f}s, "
            f"min={self.min_time:.3f}s, "
            f"max={self.max_time:.3f}s"
        )


class PerformanceProfiler:
    """Simple performance profiler for tracking operation timings."""

    def __init__(self):
        """Initialize profiler."""
        self._metrics: dict[str, PerformanceMetrics] = {}
        self._enabled = False

    def enable(self) -> None:
        """Enable profiling."""
        self._enabled = True
        logger.debug("Performance profiling enabled")

    def disable(self) -> None:
        """Disable profiling."""
        self._enabled = False
        logger.debug("Performance profiling disabled")

    @contextmanager
    def measure(self, operation: str):
        """Context manager for measuring operation duration.

        Args:
            operation: Name of the operation being measured

        Yields:
            None

        Example:
            with profiler.measure("encode_chunk"):
                encode_chunk(...)
        """
        if not self._enabled:
            yield
            return

        start_time = time.perf_counter()
        try:
            yield
        finally:
            duration = time.perf_counter() - start_time

            if operation not in self._metrics:
                self._metrics[operation] = PerformanceMetrics(operation)

            self._metrics[operation].add_measurement(duration)

    def get_metrics(self, operation: str | None = None) -> dict[str, PerformanceMetrics] | PerformanceMetrics | None:
        """Get performance metrics.

        Args:
            operation: Specific operation to get metrics for (None for all)

        Returns:
            Metrics for specific operation or all metrics
        """
        if operation:
            return self._metrics.get(operation)
        return self._metrics.copy()

    def log_summary(self, min_count: int = 1) -> None:
        """Log performance summary.

        Args:
            min_count: Minimum operation count to include in summary
        """
        if not self._metrics:
            logger.info("No performance metrics collected")
            return

        logger.info("=" * 80)
        logger.info("Performance Summary")
        logger.info("=" * 80)

        # Sort by total time descending
        sorted_metrics = sorted(
            self._metrics.values(),
            key=lambda m: m.total_time,
            reverse=True
        )

        for metrics in sorted_metrics:
            if metrics.count >= min_count:
                logger.info(str(metrics))

        logger.info("=" * 80)

    def reset(self) -> None:
        """Reset all metrics."""
        self._metrics.clear()
        logger.debug("Performance metrics reset")


# Global profiler instance
_profiler = PerformanceProfiler()


def get_profiler() -> PerformanceProfiler:
    """Get the global performance profiler instance.

    Returns:
        Global PerformanceProfiler instance
    """
    return _profiler


@contextmanager
def measure_time(operation: str):
    """Convenience context manager for measuring operation time.

    Args:
        operation: Name of the operation being measured

    Yields:
        None

    Example:
        with measure_time("encode_chunk"):
            encode_chunk(...)
    """
    with _profiler.measure(operation):
        yield


def enable_profiling() -> None:
    """Enable performance profiling globally."""
    _profiler.enable()


def disable_profiling() -> None:
    """Disable performance profiling globally."""
    _profiler.disable()


def log_performance_summary(min_count: int = 1) -> None:
    """Log performance summary.

    Args:
        min_count: Minimum operation count to include in summary
    """
    _profiler.log_summary(min_count)


def reset_profiling() -> None:
    """Reset all performance metrics."""
    _profiler.reset()
