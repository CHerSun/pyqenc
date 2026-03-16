"""
Enhanced progress reporting with visual feedback.

This module provides progress bars, phase transition messages, and
status updates using alive-progress for a better user experience.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from alive_progress import alive_bar, config_handler

from pyqenc.constants import FAILURE_SYMBOL_MINOR, PADDING_CRF, SUCCESS_SYMBOL_MINOR

config_handler.set_global(enrich_print=False) # type: ignore
logger = logging.getLogger(__name__)


class ProgressReporter:
    """Unified progress reporting across pipeline phases.

    Provides visual progress bars, phase transition messages,
    and detailed status updates for user feedback.
    """

    def __init__(self, log_level: str = "info"):
        """Initialize progress reporter.

        Args:
            log_level: Logging level (debug, info, warning, critical)
        """
        self.log_level = log_level
        self._phase_start_time: datetime | None = None
        self._chunk_start_time: datetime | None = None

    def report_phase_start(self, phase: str, total_items: int | None = None) -> None:
        """Report phase start with estimated total items.

        Args:
            phase: Phase name
            total_items: Total items to process (if known)
        """
        self._phase_start_time = datetime.now()

        if total_items:
            logger.info(f"Phase: {phase} ({total_items} items)")
        else:
            logger.info(f"Phase: {phase}")

    def report_phase_complete(self, phase: str, items_processed: int = 0, items_reused: int = 0) -> None:
        """Report phase completion with summary.

        Args:
            phase: Phase name
            items_processed: Number of items newly processed
            items_reused: Number of items reused from previous runs
        """
        if self._phase_start_time:
            elapsed = datetime.now() - self._phase_start_time
            elapsed_str = self._format_duration(elapsed)

            if items_reused > 0:
                logger.info(
                    f"Phase: {phase} completed in {elapsed_str} "
                    f"({items_processed} processed, {items_reused} reused)"
                )
            elif items_processed > 0:
                logger.info(f"Phase: {phase} completed in {elapsed_str} ({items_processed} items)")
            else:
                logger.info(f"Phase: {phase} completed in {elapsed_str}")
        else:
            logger.info(f"Phase: {phase} completed")

        self._phase_start_time = None

    def report_phase_skipped(self, phase: str, reason: str = "already complete") -> None:
        """Report phase skipped.

        Args:
            phase: Phase name
            reason: Reason for skipping
        """
        logger.info(f"Phase: {phase} skipped ({reason})")

    def report_chunk_start(self, chunk_id: str, strategy: str, attempt: int, crf: float) -> None:
        """Report chunk encoding start.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
            attempt: Attempt number
            crf: CRF value being used
        """
        self._chunk_start_time = datetime.now()

        if self.log_level == "debug":
            logger.debug(f"Chunk {chunk_id}: attempt {attempt}, CRF {crf:{PADDING_CRF}}, strategy {strategy}")

    def report_chunk_complete(
        self,
        chunk_id: str,
        strategy: str,
        attempt: int,
        crf: float,
        success: bool,
        metrics: dict[str, float] | None = None
    ) -> None:
        """Report chunk encoding completion.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
            attempt: Attempt number
            crf: CRF value used
            success: Whether quality targets were met
            metrics: Quality metrics (if available)
        """
        if self.log_level == "debug":
            elapsed = ""
            if self._chunk_start_time:
                duration = datetime.now() - self._chunk_start_time
                elapsed = f" ({self._format_duration(duration)})"

            status_symbol: str = SUCCESS_SYMBOL_MINOR if success else FAILURE_SYMBOL_MINOR

            if metrics:
                metrics_str = ", ".join(f"{k}: {v:.2f}" for k, v in metrics.items())
                logger.debug(
                    f"Chunk {chunk_id}: attempt {attempt}, CRF {crf:{PADDING_CRF}} → {metrics_str} {status_symbol}{elapsed}"
                )
            else:
                logger.debug(
                    f"Chunk {chunk_id}: attempt {attempt}, CRF {crf:{PADDING_CRF}} {status_symbol}{elapsed}"
                )

        self._chunk_start_time = None

    def report_chunk_reused(self, chunk_id: str, strategy: str, crf: float) -> None:
        """Report chunk reused from previous run.

        Args:
            chunk_id: Chunk identifier
            strategy: Strategy name
            crf: CRF value from previous encoding
        """
        if self.log_level == "debug":
            logger.debug(f"Chunk {chunk_id}: reusing existing encoding (CRF {crf:{PADDING_CRF}}, strategy {strategy})")

    def report_extraction_info(
        self,
        video_count: int,
        audio_count: int,
        crop_detected: bool = False,
        crop_params: str | None = None,
        original_resolution: str | None = None,
        cropped_resolution: str | None = None
    ) -> None:
        """Report extraction phase information.

        Args:
            video_count: Number of video streams extracted
            audio_count: Number of audio streams extracted
            crop_detected: Whether black borders were detected
            crop_params: Crop parameters (if detected)
            original_resolution: Original video resolution
            cropped_resolution: Cropped video resolution
        """
        logger.info(f"Extracted {video_count} video stream(s) and {audio_count} audio stream(s)")

        if crop_detected and crop_params:
            parts = crop_params.split()
            if len(parts) >= 2:
                top, bottom = int(parts[0]), int(parts[1])
                left = int(parts[2]) if len(parts) > 2 else 0
                right = int(parts[3]) if len(parts) > 3 else 0

                total_vertical = top + bottom
                total_horizontal = left + right

                if total_vertical > 0 and total_horizontal > 0:
                    logger.info(
                        f"Detected black borders: {top} top, {bottom} bottom, {left} left, {right} right "
                        f"(removed {total_vertical}px vertical, {total_horizontal}px horizontal)"
                    )
                elif total_vertical > 0:
                    logger.info(
                        f"Detected black borders: {top} top, {bottom} bottom "
                        f"(removed {total_vertical}px vertical)"
                    )
                elif total_horizontal > 0:
                    logger.info(
                        f"Detected black borders: {left} left, {right} right "
                        f"(removed {total_horizontal}px horizontal)"
                    )

        if original_resolution and cropped_resolution and original_resolution != cropped_resolution:
            logger.info(f"Original resolution: {original_resolution}, Cropped resolution: {cropped_resolution}")

    def report_chunking_info(self, chunk_count: int, total_frames: int, duration: float) -> None:
        """Report chunking phase information.

        Args:
            chunk_count: Number of chunks created
            total_frames: Total frame count
            duration: Total duration in seconds
        """
        duration_str = self._format_duration(timedelta(seconds=duration))
        logger.info(f"Created {chunk_count} chunks ({duration_str} total, {total_frames} frames)")

    def report_optimization_info(
        self,
        test_chunk_count: int,
        strategies_tested: int,
        optimal_strategy: str,
        avg_crf: float,
        avg_size_mb: float
    ) -> None:
        """Report optimization phase information.

        Args:
            test_chunk_count: Number of test chunks used
            strategies_tested: Number of strategies tested
            optimal_strategy: Selected optimal strategy
            avg_crf: Average CRF for optimal strategy
            avg_size_mb: Average file size in MB for optimal strategy
        """
        logger.info(f"Testing {strategies_tested} strategies on {test_chunk_count} chunks...")
        logger.info(
            f"Optimal strategy: {optimal_strategy} "
            f"(avg CRF: {avg_crf:.2f}, avg size: {avg_size_mb:.1f} MB)"
        )

    def report_encoding_summary(
        self,
        total_chunks: int,
        encoded_count: int,
        reused_count: int,
        failed_count: int,
        strategies: list[str]
    ) -> None:
        """Report encoding phase summary.

        Args:
            total_chunks: Total number of chunks
            encoded_count: Number of chunks newly encoded
            reused_count: Number of chunks reused
            failed_count: Number of chunks that failed
            strategies: List of strategies used
        """
        strategies_str = ", ".join(strategies)

        if failed_count > 0:
            logger.warning(
                f"Encoding completed: {encoded_count} encoded, {reused_count} reused, "
                f"{failed_count} failed (strategies: {strategies_str})"
            )
        else:
            logger.info(
                f"Encoding completed: {encoded_count} encoded, {reused_count} reused "
                f"(strategies: {strategies_str})"
            )

    def report_audio_info(self, day_count: int, night_count: int) -> None:
        """Report audio processing information.

        Args:
            day_count: Number of day mode audio files processed
            night_count: Number of night mode audio files processed
        """
        logger.info(f"Processed audio: {day_count} day mode, {night_count} night mode")

    def report_merge_info(self, output_file: Path, frame_count: int, file_size_mb: float) -> None:
        """Report merge phase information.

        Args:
            output_file: Path to output file
            frame_count: Total frame count in output
            file_size_mb: Output file size in MB
        """
        logger.info(
            f"Merged final video: {output_file.name} "
            f"({frame_count} frames, {file_size_mb:.1f} MB)"
        )

    def report_dry_run_phase(
        self,
        phase: str,
        complete: bool,
        reused_items: list[str] | None = None,
        missing_items: list[str] | None = None,
        details: str | None = None
    ) -> None:
        """Report dry-run phase status.

        Args:
            phase: Phase name
            complete: Whether phase is complete
            reused_items: List of items that would be reused
            missing_items: List of items that would be created
            details: Additional details about the phase
        """
        if complete:
            logger.info(f"[DRY-RUN] Phase: {phase}")
            if reused_items:
                for item in reused_items[:3]:  # Show first 3
                    logger.info(f"[DRY-RUN]   {SUCCESS_SYMBOL_MINOR} {item}")
                if len(reused_items) > 3:
                    logger.info(f"[DRY-RUN]   {SUCCESS_SYMBOL_MINOR} ... and {len(reused_items) - 3} more")
            logger.info("[DRY-RUN]   Status: Complete (reusing existing files)")
        else:
            logger.info(f"[DRY-RUN] Phase: {phase}")
            if missing_items:
                for item in missing_items[:3]:  # Show first 3
                    logger.info(f"[DRY-RUN]   {FAILURE_SYMBOL_MINOR} {item}")
                if len(missing_items) > 3:
                    logger.info(f"[DRY-RUN]   {FAILURE_SYMBOL_MINOR} ... and {len(missing_items) - 3} more")
            if details:
                logger.info(f"[DRY-RUN]   {details}")
            logger.info("[DRY-RUN]   Status: Needs work")

    def report_dry_run_stop(self) -> None:
        """Report dry-run stopping at first incomplete phase."""
        logger.info("")
        logger.info("[DRY-RUN] Stopping at first incomplete phase.")
        logger.info("[DRY-RUN] Run with -y to execute.")

    def report_error(self, phase: str, error: str) -> None:
        """Report phase error.

        Args:
            phase: Phase name
            error: Error message
        """
        logger.error(f"Phase: {phase} failed - {error}")

    def report_warning(self, message: str) -> None:
        """Report warning message.

        Args:
            message: Warning message
        """
        logger.warning(message)

    def report_disk_space_warning(self, required_gb: float, available_gb: float) -> None:
        """Report disk space warning.

        Args:
            required_gb: Estimated required space in GB
            available_gb: Available space in GB
        """
        logger.warning(
            f"Disk space may be insufficient: {available_gb:.1f} GB available, "
            f"~{required_gb:.1f} GB estimated required"
        )

    def report_cleanup_prompt(self, work_dir: Path, size_mb: float) -> None:
        """Report cleanup prompt after successful completion.

        Args:
            work_dir: Working directory path
            size_mb: Size of working directory in MB
        """
        logger.info("")
        logger.info(f"Pipeline completed successfully!")
        logger.info(f"Working directory: {work_dir} ({size_mb:.1f} MB)")
        logger.info(f"You can safely delete the working directory if you're satisfied with the results.")

    @staticmethod
    def _format_duration(duration: timedelta) -> str:
        """Format duration as human-readable string.

        Args:
            duration: Duration to format

        Returns:
            Formatted duration string (e.g., "1h 23m 45s")
        """
        total_seconds = int(duration.total_seconds())

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60

        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0 or not parts:
            parts.append(f"{seconds}s")

        return " ".join(parts)
