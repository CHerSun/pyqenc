"""Pipeline orchestrator for coordinating phase execution."""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pyqenc.constants import SUCCESS_SYMBOL_MINOR, TEMP_SUFFIX, THICK_LINE, THIN_LINE
from pyqenc.models import (
    CropParams,
    PhaseMetadata,
    PhaseStatus,
    PhaseUpdate,
    PipelineConfig,
    PipelineState,
)
from pyqenc.progress import ProgressTracker

logger = logging.getLogger(__name__)


def _get_persisted_test_chunk_ids(state: "PipelineState | None") -> list[str]:
    """Return test chunk IDs persisted from a previous optimization run, or empty list."""
    if state is None:
        return []
    opt_phase = state.phases.get("optimization")
    if opt_phase is not None and opt_phase.metadata is not None:
        return opt_phase.metadata.test_chunk_ids
    return []


class Phase(Enum):
    """Pipeline phases in execution order."""
    EXTRACTION = "extraction"
    CHUNKING = "chunking"
    OPTIMIZATION = "optimization"
    ENCODING = "encoding"
    AUDIO = "audio"
    MERGE = "merge"


@dataclass
class PhaseResult:
    """Result of executing a single phase."""
    phase: Phase
    success: bool
    reused: bool  # True if existing artifacts were reused
    needs_work: bool  # True if phase needs work (dry-run mode)
    message: str
    error: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class PipelineResult:
    """Result of complete pipeline execution."""
    success: bool
    phases_executed: list[Phase]
    phases_reused: list[Phase]
    phases_needing_work: list[Phase]
    output_files: list[Path]
    error: str | None = None


class PipelineOrchestrator:
    """Orchestrates pipeline phase execution with artifact-based resumption."""

    def __init__(self, config: PipelineConfig, tracker: ProgressTracker):
        """Initialize orchestrator with configuration and progress tracker.

        Args:
            config: Pipeline configuration
            tracker: Progress tracker for state management
        """
        self.config = config
        self.tracker = tracker
        self._optimal_strategy: str | None = None
        self._phase_order = [
            Phase.EXTRACTION,
            Phase.CHUNKING,
            Phase.OPTIMIZATION,
            Phase.ENCODING,
            Phase.AUDIO,
            Phase.MERGE,
        ]

    def run(self, dry_run: bool = True, max_phases: int | None = None) -> PipelineResult:
        """Execute complete pipeline.

        Always attempts all phases from scratch. Each phase checks for
        existing artifacts and reuses them if valid, or performs work if needed.

        Args:
            dry_run: If True, only print what would be done (default)
            max_phases: Maximum number of phases to execute (None = all)

        Returns:
            PipelineResult with execution summary

        In dry-run mode:
        - Prints status of each phase (complete/needs work)
        - Shows what artifacts exist and what would be created
        - Stops after first phase that needs work
        - Returns without performing any modifications
        """
        logger.info("")

        # Check disk space before starting (only in execute mode)
        if not dry_run:
            from pyqenc.utils.disk_space import log_disk_space_info

            sufficient_space = log_disk_space_info(
                source_video=self.config.source_video,
                work_dir=self.config.work_dir,
                num_strategies=len(self.config.strategies),
                include_optimization=self.config.optimize,
                chunking_mode=self.config.chunking_mode,
            )

            if not sufficient_space:
                return PipelineResult(
                    success=False,
                    phases_executed=[],
                    phases_reused=[],
                    phases_needing_work=[],
                    output_files=[],
                    error="Insufficient disk space. Free up space or use a different work directory."
                )

        phases_executed = []
        phases_reused = []
        phases_needing_work = []
        output_files = []

        logger.info("")

        # Load existing state or initialize new one
        state = self.tracker.load_state()
        if state:
            logger.info(f"Loaded existing state from: {self.tracker.state_file}")
            # Restore optimal strategy from persisted optimization result
            opt_phase = state.phases.get(Phase.OPTIMIZATION.value)
            if (
                opt_phase is not None
                and opt_phase.metadata is not None
                and opt_phase.metadata.optimal_strategy
            ):
                self._optimal_strategy = opt_phase.metadata.optimal_strategy
                logger.info("Restored optimal strategy from state: %s", self._optimal_strategy)
        else:
            logger.info("Starting fresh pipeline execution")
            # Initialize new state
            from pyqenc.models import PipelineState, VideoMetadata
            state = PipelineState(
                version="1.0",
                source_video=VideoMetadata(path=self.config.source_video),
                current_phase="",
                phases={},
                chunks_metadata={},
                chunks={}
            )
            # Eagerly populate all source video metadata so it's persisted
            # and available for change-detection on subsequent restarts.
            sv = state.source_video
            _ = sv.file_size_bytes   # filesystem stat — instant
            _ = sv.duration_seconds  # ffprobe — fast
            _ = sv.fps
            _ = sv.resolution
            _ = sv.frame_count       # ffmpeg null-encode — slower but done once
            # Save initial state
            if not dry_run:
                self.tracker.save_state(state, force=True)

        # Determine which phases to execute
        phases_to_run = self._phase_order[:max_phases] if max_phases else self._phase_order

        # Skip optimization phase if not enabled
        if not self.config.optimize and Phase.OPTIMIZATION in phases_to_run:
            phases_to_run = [p for p in phases_to_run if p != Phase.OPTIMIZATION]
            logger.info("Optimization phase disabled")

        # Execute phases
        for phase in phases_to_run:
            logger.info(THIN_LINE)
            logger.info("Phase: %s", phase.value.upper())
            logger.info(THIN_LINE)

            try:
                result = self._execute_phase(phase, dry_run=dry_run)

                if result.success:
                    if result.reused:
                        phases_reused.append(phase)
                        logger.info(f"{SUCCESS_SYMBOL_MINOR} {phase.value}: {result.message} (reused)")
                    else:
                        phases_executed.append(phase)
                        logger.info(f"{SUCCESS_SYMBOL_MINOR} {phase.value}: {result.message}")

                    # Track output files
                    if result.metadata and "output_files" in result.metadata:
                        output_files.extend(result.metadata["output_files"])

                    # Update progress tracker — preserve existing phase metadata
                    if not dry_run:
                        existing_phase = self.tracker._state.phases.get(phase.value) if self.tracker._state else None
                        self.tracker.update_phase(PhaseUpdate(
                            phase=phase.value,
                            status=PhaseStatus.COMPLETED,
                            metadata=existing_phase.metadata if existing_phase else None,
                        ))

                elif result.needs_work:
                    phases_needing_work.append(phase)
                    logger.info(f"⚠ {phase.value}: {result.message}")

                    if dry_run:
                        logger.info("")
                        logger.info("[DRY-RUN] Stopping at first incomplete phase.")
                        logger.info("[DRY-RUN] Run with -y to execute.")
                        break

                else:
                    # Phase failed
                    logger.critical(f" {phase.value}: {result.message}")
                    if result.error:
                        logger.critical(f"Error: {result.error}")

                    return PipelineResult(
                        success=False,
                        phases_executed=phases_executed,
                        phases_reused=phases_reused,
                        phases_needing_work=phases_needing_work,
                        output_files=output_files,
                        error=f"Phase {phase.value} failed: {result.error or result.message}"
                    )

            except Exception as e:
                logger.critical(f"✗ {phase.value}: Unexpected error", exc_info=True)
                return PipelineResult(
                    success=False,
                    phases_executed=phases_executed,
                    phases_reused=phases_reused,
                    phases_needing_work=phases_needing_work,
                    output_files=output_files,
                    error=f"Phase {phase.value} failed with exception: {e}"
                )

        # Summary
        logger.info(THICK_LINE)
        if dry_run:
            logger.info("[DRY-RUN] Pipeline preview completed")
            logger.info(f"Phases complete: {len(phases_reused)}")
            logger.info(f"Phases needing work: {len(phases_needing_work)}")
        else:
            logger.info("Pipeline execution completed")
            logger.info(f"Phases executed: {len(phases_executed)}")
            logger.info(f"Phases reused: {len(phases_reused)}")
            if output_files:
                logger.info(f"Output files: {len(output_files)}")
                for output_file in output_files:
                    logger.info(f"  - {output_file}")
        logger.info(THICK_LINE)

        result = PipelineResult(
            success=True,
            phases_executed=phases_executed,
            phases_reused=phases_reused,
            phases_needing_work=phases_needing_work,
            output_files=output_files,
            error=None
        )

        # Prompt for cleanup if pipeline completed successfully and not in dry-run mode
        if not dry_run and result.success and not phases_needing_work:
            # Check if keep_all flag is set in config
            if not getattr(self.config, 'keep_all', False):
                from pyqenc.utils.cleanup import (
                    cleanup_intermediate_files,
                    prompt_cleanup,
                )

                cleanup_choice = prompt_cleanup(self.config.work_dir)

                if cleanup_choice == "keep_metrics":
                    logger.info("Cleaning up intermediate files (keeping metrics)...")
                    cleanup_stats = cleanup_intermediate_files(
                        self.config.work_dir,
                        keep_metrics=True,
                        dry_run=False
                    )
                    if cleanup_stats.success:
                        logger.info(
                            f"Cleanup completed: removed {cleanup_stats.chunks_removed} chunks, "
                            f"{cleanup_stats.encoded_removed} encoded files, "
                            f"kept {cleanup_stats.metrics_kept} metric files, "
                            f"freed {cleanup_stats.space_freed_mb:.2f} MB"
                        )
                    else:
                        logger.warning(f"Cleanup failed: {cleanup_stats.error}")

                elif cleanup_choice == "remove_all":
                    logger.info("Cleaning up all intermediate files...")
                    cleanup_stats = cleanup_intermediate_files(
                        self.config.work_dir,
                        keep_metrics=False,
                        dry_run=False
                    )
                    if cleanup_stats.success:
                        logger.info(
                            f"Cleanup completed: removed {cleanup_stats.chunks_removed} chunks, "
                            f"{cleanup_stats.encoded_removed} encoded files, "
                            f"freed {cleanup_stats.space_freed_mb:.2f} MB"
                        )
                    else:
                        logger.warning(f"Cleanup failed: {cleanup_stats.error}")

                else:
                    logger.info("Skipping cleanup. Intermediate files preserved.")
            else:
                logger.info("Cleanup skipped (--keep-all flag set). All intermediate files preserved.")

        return result

    def _execute_phase(self, phase: Phase, dry_run: bool = False) -> PhaseResult:
        """Execute a single phase and update progress.

        Phase determines internally if work is needed based on artifacts.
        In dry-run mode, phase only reports status without performing work.

        Args:
            phase: Phase to execute
            dry_run: If True, only report status without performing work

        Returns:
            PhaseResult with execution details
        """
        if phase == Phase.EXTRACTION:
            return self._execute_extraction(dry_run)
        elif phase == Phase.CHUNKING:
            return self._execute_chunking(dry_run)
        elif phase == Phase.OPTIMIZATION:
            return self._execute_optimization(dry_run)
        elif phase == Phase.ENCODING:
            return self._execute_encoding(dry_run)
        elif phase == Phase.AUDIO:
            return self._execute_audio(dry_run)
        elif phase == Phase.MERGE:
            return self._execute_merge(dry_run)
        else:
            return PhaseResult(
                phase=phase,
                success=False,
                reused=False,
                needs_work=False,
                message=f"Unknown phase: {phase}",
                error="Phase not implemented"
            )

    def _resolve_crop_params(self) -> CropParams | None:
        """Read crop parameters from persisted extraction phase metadata.

        Returns:
            CropParams if extraction phase stored valid crop data, None otherwise.
        """
        state = self.tracker._state
        if state is None:
            return None
        ext_phase = state.phases.get(Phase.EXTRACTION.value)
        if ext_phase and ext_phase.metadata and ext_phase.metadata.crop_params:
            try:
                return CropParams.parse(ext_phase.metadata.crop_params)
            except ValueError:
                logger.warning(
                    "Could not parse persisted crop_params '%s'; proceeding without crop",
                    ext_phase.metadata.crop_params,
                )
        return None

    def _execute_extraction(self, dry_run: bool) -> PhaseResult:
        """Execute extraction phase.

        Args:
            dry_run: If True, only report status

        Returns:
            PhaseResult with execution details
        """
        # Import here to avoid circular dependencies
        from pyqenc.phases.extraction import extract_streams

        output_dir = self.config.work_dir / "extracted"

        try:
            result = extract_streams(
                source_video=self.config.source_video,
                output_dir=output_dir,
                video_filter=self.config.video_filter,
                audio_filter=self.config.audio_filter,
                detect_crop=self.config.crop_params is None,  # Auto-detect if not manually specified
                manual_crop=str(self.config.crop_params) if self.config.crop_params else None,
                force=False,
                dry_run=dry_run,
            )

            if dry_run and result.needs_work:
                return PhaseResult(
                    phase=Phase.EXTRACTION,
                    success=True,
                    reused=False,
                    needs_work=True,
                    message="Needs work: streams not extracted",
                    metadata={"result": result}
                )

            if result.success:
                # Store crop parameters if detected
                if result.crop_params and not dry_run:
                    self.tracker.update_phase(PhaseUpdate(
                        phase=Phase.EXTRACTION.value,
                        status=PhaseStatus.COMPLETED,
                        metadata=PhaseMetadata(crop_params=str(result.crop_params)),
                    ))

                return PhaseResult(
                    phase=Phase.EXTRACTION,
                    success=True,
                    reused=result.reused,
                    needs_work=False,
                    message=f"Extracted {len(result.video_files)} video, {len(result.audio_files)} audio streams",
                    metadata={
                        "video_files": result.video_files,
                        "audio_files": result.audio_files,
                        "crop_params": result.crop_params,
                    }
                )
            else:
                return PhaseResult(
                    phase=Phase.EXTRACTION,
                    success=False,
                    reused=False,
                    needs_work=False,
                    message="Extraction failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Extraction phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.EXTRACTION,
                success=False,
                reused=False,
                needs_work=False,
                message="Extraction failed with exception",
                error=str(e)
            )

    def _execute_chunking(self, dry_run: bool) -> PhaseResult:
        """Execute chunking phase.

        Args:
            dry_run: If True, only report status

        Returns:
            PhaseResult with execution details
        """
        # Import here to avoid circular dependencies
        from pyqenc.phases.chunking import chunk_video

        # Use source video directly (not extracted stream) to avoid timestamp issues
        video_file = self.config.source_video
        output_dir = self.config.work_dir / "chunks"

        try:
            result = chunk_video(
                video_file=video_file,
                output_dir=output_dir,
                chunking_mode=self.config.chunking_mode,
                force=False,
                dry_run=dry_run,
                tracker=self.tracker
            )

            if dry_run and result.needs_work:
                return PhaseResult(
                    phase=Phase.CHUNKING,
                    success=True,
                    reused=False,
                    needs_work=True,
                    message="Needs work: video not chunked",
                    metadata={"result": result}
                )

            if result.success:
                # Verify chunk frame total matches source
                state = self.tracker._state
                source_frame_count = state.source_video._frame_count if state else None
                if source_frame_count and result.total_frames != source_frame_count:
                    diff = result.total_frames - source_frame_count
                    sign = "+" if diff > 0 else ""
                    logger.warning(
                        "Chunk frame total (%d) differs from source (%d) by %s%d frames. "
                        "This is expected with stream-copy I-frame snapping — see chunking TODO.",
                        result.total_frames, source_frame_count, sign, diff,
                    )
                elif source_frame_count:
                    logger.info(
                        "Frame count verified: %d chunk frames == %d source frames %s",
                        result.total_frames, source_frame_count, SUCCESS_SYMBOL_MINOR
                    )

                return PhaseResult(
                    phase=Phase.CHUNKING,
                    success=True,
                    reused=result.reused,
                    needs_work=False,
                    message=f"Created {len(result.chunks)} chunks ({result.total_frames} frames)",
                    metadata={"chunks": result.chunks}
                )
            else:
                return PhaseResult(
                    phase=Phase.CHUNKING,
                    success=False,
                    reused=False,
                    needs_work=False,
                    message="Chunking failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Chunking phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.CHUNKING,
                success=False,
                reused=False,
                needs_work=False,
                message="Chunking failed with exception",
                error=str(e)
            )

    def _execute_optimization(self, dry_run: bool) -> PhaseResult:
        """Execute optimization phase."""
        from pyqenc.config import ConfigManager
        from pyqenc.phases.encoding import ChunkInfo
        from pyqenc.phases.optimization import find_optimal_strategy

        chunks_dir = self.config.work_dir / "chunks"

        if not chunks_dir.exists():
            return PhaseResult(
                phase=Phase.OPTIMIZATION,
                success=False,
                reused=False,
                needs_work=True,
                message="Chunks directory not found",
                error="Chunking phase must complete first",
            )

        chunk_files = sorted(chunks_dir.glob("*.mkv"))
        if not chunk_files:
            return PhaseResult(
                phase=Phase.OPTIMIZATION,
                success=False,
                reused=False,
                needs_work=True,
                message="No chunks found in chunks directory",
                error="Chunking phase must complete first",
            )

        # Fast-path: reuse cached result from a previous run
        state = self.tracker._state
        if state and not dry_run:
            opt_phase = state.phases.get(Phase.OPTIMIZATION.value)
            if (
                opt_phase is not None
                and opt_phase.status.value == "completed"
                and opt_phase.metadata is not None
                and opt_phase.metadata.optimal_strategy
            ):
                optimal = opt_phase.metadata.optimal_strategy
                logger.info("Optimization already completed — reusing result: %s", optimal)
                self._optimal_strategy = optimal
                return PhaseResult(
                    phase=Phase.OPTIMIZATION,
                    success=True,
                    reused=True,
                    needs_work=False,
                    message=f"Optimal strategy (cached): {optimal}",
                    metadata={"optimal_strategy": optimal},
                )

        config_manager = ConfigManager()

        # Resolve strategies the same way encoding does
        strategies_input = self.config.strategies if self.config.strategies else None
        resolved = config_manager.expand_strategies(strategies_input)
        seen: set[str] = set()
        strategy_names: list[str] = []
        for sc in resolved:
            key = f"{sc.preset}+{sc.profile}"
            if key not in seen:
                seen.add(key)
                strategy_names.append(key)

        logger.info("Found %d chunks for optimization testing", len(chunk_files))

        chunks = [
            ChunkInfo(
                chunk_id=f.stem,
                file_path=f,
                start_frame=0,
                end_frame=0,
                frame_count=0,
                duration=0.0,
            )
            for f in chunk_files
        ]

        try:
            crop_params = self._resolve_crop_params()
            if crop_params:
                logger.info("Crop params loaded: %s", crop_params)
            else:
                logger.info("No crop params; encoding without crop")

            result = find_optimal_strategy(
                chunks=chunks,
                reference_dir=chunks_dir,
                strategies=strategy_names,
                quality_targets=self.config.quality_targets,
                work_dir=self.config.work_dir,
                config_manager=config_manager,
                progress_tracker=self.tracker,
                dry_run=dry_run,
                crop_params=crop_params,
                max_parallel=self.config.max_parallel,
                persisted_chunk_ids=_get_persisted_test_chunk_ids(state),
            )

            if dry_run:
                return PhaseResult(
                    phase=Phase.OPTIMIZATION,
                    success=True,
                    reused=False,
                    needs_work=True,
                    message="Would test strategies on representative chunks",
                )

            if result.success:
                logger.info("Strategy test results:")
                for strategy, test_result in result.test_results.items():
                    if test_result.all_passed:
                        logger.info(
                            "  %s: avg CRF %.2f, avg size %.2f MB",
                            strategy,
                            test_result.avg_crf,
                            test_result.avg_file_size / 1024 / 1024,
                        )
                    else:
                        logger.warning("  %s: failed to encode all test chunks", strategy)

                # Store optimal strategy for encoding phase and persist to state
                self._optimal_strategy = result.optimal_strategy
                if result.optimal_strategy:
                    from pyqenc.models import PhaseMetadata, PhaseStatus, PhaseUpdate

                    # Preserve test_chunk_ids that were persisted during selection
                    existing_chunk_ids = _get_persisted_test_chunk_ids(state)
                    self.tracker.update_phase(
                        PhaseUpdate(
                            phase=Phase.OPTIMIZATION.value,
                            status=PhaseStatus.COMPLETED,
                            metadata=PhaseMetadata(
                                optimal_strategy=result.optimal_strategy,
                                test_chunk_ids=existing_chunk_ids,
                            ),
                        )
                    )

                logger.info("Optimal strategy: %s", result.optimal_strategy)
                return PhaseResult(
                    phase=Phase.OPTIMIZATION,
                    success=True,
                    reused=False,
                    needs_work=False,
                    message=f"Optimal strategy: {result.optimal_strategy}",
                    metadata={"optimal_strategy": result.optimal_strategy},
                )
            else:
                return PhaseResult(
                    phase=Phase.OPTIMIZATION,
                    success=False,
                    reused=False,
                    needs_work=False,
                    message="Optimization failed",
                    error=result.error,
                )

        except Exception as e:
            logger.error("Optimization phase error: %s", e, exc_info=True)
            return PhaseResult(
                phase=Phase.OPTIMIZATION,
                success=False,
                reused=False,
                needs_work=False,
                message="Optimization failed with exception",
                error=str(e),
            )

    def _execute_encoding(self, dry_run: bool) -> PhaseResult:
        """Execute encoding phase.

        Args:
            dry_run: If True, only report status

        Returns:
            PhaseResult with execution details
        """
        # Import here to avoid circular dependencies
        from pyqenc.config import ConfigManager
        from pyqenc.phases.encoding import ChunkInfo, encode_all_chunks

        chunks_dir = self.config.work_dir / "chunks"

        if not chunks_dir.exists():
            return PhaseResult(
                phase=Phase.ENCODING,
                success=False,
                reused=False,
                needs_work=True,
                message="Chunks directory not found",
                error="Chunking phase must complete first"
            )

        # Load chunk information from chunking phase
        # Get chunks from directory
        chunk_files = sorted(chunks_dir.glob("*.mkv"))
        if not chunk_files:
            return PhaseResult(
                phase=Phase.ENCODING,
                success=False,
                reused=False,
                needs_work=True,
                message="No chunks found in chunks directory",
                error="Chunking phase must complete first"
            )

        # Create ChunkInfo objects
        # Note: We don't have frame counts readily available, but they're not critical for encoding
        chunks = []
        for chunk_file in chunk_files:
            chunk_id = chunk_file.stem  # e.g., "chunk.000000-000319"
            chunks.append(ChunkInfo(
                chunk_id=chunk_id,
                file_path=chunk_file,
                start_frame=0,  # Not critical for encoding
                end_frame=0,    # Not critical for encoding
                frame_count=0,  # Not critical for encoding
                duration=0.0    # Not critical for encoding
            ))

        logger.info(f"Found {len(chunks)} chunks to encode")

        # Initialize config manager
        config_manager = ConfigManager()

        # Resolve strategies: if optimization ran and picked one, use it;
        # otherwise use config strategies (or defaults from config file)
        if self._optimal_strategy:
            strategy_names = [self._optimal_strategy]
            logger.info("Using optimal strategy from optimization phase: %s", self._optimal_strategy)
        else:
            strategies = self.config.strategies if self.config.strategies else None
            resolved_strategy_configs = config_manager.expand_strategies(strategies)
            strategy_names = []
            seen_strategies: set[str] = set()
            for sc in resolved_strategy_configs:
                key = f"{sc.preset}+{sc.profile}"
                if key not in seen_strategies:
                    seen_strategies.add(key)
                    strategy_names.append(key)

        logger.info(f"Resolved {len(strategy_names)} strategies: {strategy_names}")

        try:
            crop_params = self._resolve_crop_params()
            if crop_params:
                logger.info("Crop params loaded: %s", crop_params)
            else:
                logger.info("No crop params; encoding without crop")

            result = encode_all_chunks(
                chunks=chunks,
                reference_dir=chunks_dir,  # Reference chunks are the original chunks
                strategies=strategy_names,
                quality_targets=self.config.quality_targets,
                work_dir=self.config.work_dir,
                config_manager=config_manager,
                progress_tracker=self.tracker,
                max_parallel=self.config.max_parallel,
                force=False,
                crop_params=crop_params,
                dry_run=dry_run,
            )

            # Calculate needs_work for dry-run
            needs_work = False
            if dry_run:
                total_work = len(chunks) * len(strategy_names)
                completed_work = result.reused_count
                needs_work = completed_work < total_work

            if dry_run and needs_work:
                chunks_needing_work = (len(chunks) * len(strategy_names)) - result.reused_count
                return PhaseResult(
                    phase=Phase.ENCODING,
                    success=True,
                    reused=False,
                    needs_work=True,
                    message=f"Needs work: {chunks_needing_work} chunk+strategy combinations need encoding",
                    metadata={"result": result}
                )

            if result.success:
                return PhaseResult(
                    phase=Phase.ENCODING,
                    success=True,
                    reused=result.reused_count > 0,
                    needs_work=False,
                    message=f"Encoded {result.encoded_count} chunks, reused {result.reused_count}",
                    metadata={"encoded_chunks": result.encoded_chunks}
                )
            else:
                return PhaseResult(
                    phase=Phase.ENCODING,
                    success=False,
                    reused=False,
                    needs_work=False,
                    message=f"Encoding failed: {len(result.failed_chunks)} chunks failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Encoding phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.ENCODING,
                success=False,
                reused=False,
                needs_work=False,
                message="Encoding failed with exception",
                error=str(e)
            )

    def _execute_audio(self, dry_run: bool) -> PhaseResult:
        """Execute audio processing phase.

        Args:
            dry_run: If True, only report status

        Returns:
            PhaseResult with execution details
        """
        # Import here to avoid circular dependencies
        from pyqenc.phases.audio import process_audio_streams

        extracted_dir = self.config.work_dir / "extracted"
        # Look for audio files - they have (audio-*) in the name from pymkvextract
        audio_files = [f for f in extracted_dir.glob("*.mka") if "(audio-" in f.name]

        if not audio_files:
            return PhaseResult(
                phase=Phase.AUDIO,
                success=False,
                reused=False,
                needs_work=True,
                message="No extracted audio files found",
                error="Extraction phase must complete first"
            )

        output_dir = self.config.work_dir / "audio"

        try:
            result = process_audio_streams(
                audio_files=audio_files,
                output_dir=output_dir,
                dry_run=dry_run,
            )

            if dry_run and result.needs_work:
                return PhaseResult(
                    phase=Phase.AUDIO,
                    success=True,
                    reused=False,
                    needs_work=True,
                    message="Needs work: audio not processed",
                    metadata={"result": result}
                )

            if result.success:
                return PhaseResult(
                    phase=Phase.AUDIO,
                    success=True,
                    reused=result.reused,
                    needs_work=False,
                    message=f"Processed {len(result.day_mode_files)} day, {len(result.night_mode_files)} night audio streams",
                    metadata={
                        "day_mode_files": result.day_mode_files,
                        "night_mode_files": result.night_mode_files,
                    }
                )
            else:
                return PhaseResult(
                    phase=Phase.AUDIO,
                    success=False,
                    reused=False,
                    needs_work=False,
                    message="Audio processing failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Audio phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.AUDIO,
                success=False,
                reused=False,
                needs_work=False,
                message="Audio processing failed with exception",
                error=str(e)
            )

    def _execute_merge(self, dry_run: bool) -> PhaseResult:
        """Execute merge phase.

        Args:
            dry_run: If True, only report status

        Returns:
            PhaseResult with execution details
        """
        # Import here to avoid circular dependencies
        from pyqenc.phases.merge import merge_final_video
        from pyqenc.utils.ffmpeg import get_frame_count

        encoded_dir = self.config.work_dir / "encoded"
        audio_dir = self.config.work_dir / "audio"
        output_dir = self.config.work_dir / "final"
        chunks_dir = self.config.work_dir / "chunks"

        if not encoded_dir.exists():
            return PhaseResult(
                phase=Phase.MERGE,
                success=False,
                reused=False,
                needs_work=True,
                message="Encoded directory not found",
                error="Encoding phase must complete first"
            )

        if not audio_dir.exists():
            return PhaseResult(
                phase=Phase.MERGE,
                success=False,
                reused=False,
                needs_work=True,
                message="Audio directory not found",
                error="Audio phase must complete first"
            )

        # Collect audio files (both day and night modes)
        audio_files = sorted(audio_dir.glob("audio_*_day.aac")) + sorted(audio_dir.glob("audio_*_night.aac"))

        if not audio_files:
            logger.warning("No processed audio files found, merging video only")

        # Get source frame count for verification
        source_frame_count = None
        try:
            # Try to get from first chunk's parent (original video)
            # Or sum up all chunks
            chunk_files = sorted(chunks_dir.glob("chunk.*.mkv"))
            if chunk_files:
                source_frame_count = sum(get_frame_count(cf) for cf in chunk_files)
                logger.debug(f"Source frame count from chunks: {source_frame_count}")
        except Exception as e:
            logger.warning(f"Could not determine source frame count: {e}")

        # Collect encoded chunks organized by chunk_id and strategy
        # Structure: {chunk_id: {strategy: path}}
        encoded_chunks = {}

        # Scan encoded directory for all strategies
        for strategy_dir in encoded_dir.iterdir():
            if not strategy_dir.is_dir():
                continue

            strategy_name = strategy_dir.name

            # Find all successful encodings for this strategy
            chunk_files = {}

            for encoded_file in strategy_dir.glob("*.mkv"):
                if TEMP_SUFFIX in encoded_file.stem:
                    continue  # Skip temp files
                # Extract chunk_id from filename. Expected format: <start>-<end>.<resolution>.crf<CRF>.mkv
                # chunk_id is the first part: "<start>-<end>"
                parts = encoded_file.stem.split(".")
                if len(parts) >= 2:
                    chunk_id = parts[0]

                    # TODO: Check what the hell is happening here. We shouldn't count attempts, rather check presence of artifacts.
                    # Keep track of highest attempt number for each chunk
                    if chunk_id not in chunk_files:
                        chunk_files[chunk_id] = encoded_file
                    else:
                        # Compare attempt numbers — extract from "attempt_N" segment
                        def _attempt_num(p: Path) -> int:
                            for seg in p.stem.split("."):
                                if seg.startswith("attempt_"):
                                    try:
                                        return int(seg.split("_")[1])
                                    except (IndexError, ValueError):
                                        pass
                            return 0
                        if _attempt_num(encoded_file) > _attempt_num(chunk_files[chunk_id]):
                            chunk_files[chunk_id] = encoded_file

            # Add to encoded_chunks structure
            for chunk_id, chunk_path in chunk_files.items():
                if chunk_id not in encoded_chunks:
                    encoded_chunks[chunk_id] = {}
                encoded_chunks[chunk_id][strategy_name] = chunk_path

        if not encoded_chunks:
            return PhaseResult(
                phase=Phase.MERGE,
                success=False,
                reused=False,
                needs_work=True,
                message="No encoded chunks found",
                error="Encoding phase must complete first"
            )

        logger.info(f"Found {len(encoded_chunks)} chunks across {len(set(s for cs in encoded_chunks.values() for s in cs.keys()))} strategies")

        try:
            result = merge_final_video(
                encoded_chunks=encoded_chunks,
                audio_files=audio_files,
                output_dir=output_dir,
                source_frame_count=source_frame_count,
                verify_frames=True,
                force=False,
                dry_run=dry_run,
            )

            if dry_run and result.needs_work:
                return PhaseResult(
                    phase=Phase.MERGE,
                    success=True,
                    reused=False,
                    needs_work=True,
                    message="Needs work: final videos not merged",
                    metadata={"result": result}
                )

            if result.success:
                # Log frame counts if available
                if result.frame_counts:
                    for strategy, frame_count in result.frame_counts.items():
                        logger.info(f"  {strategy}: {frame_count} frames")

                return PhaseResult(
                    phase=Phase.MERGE,
                    success=True,
                    reused=result.reused,
                    needs_work=False,
                    message=f"Merged {len(result.output_files)} final video(s)",
                    metadata={"output_files": list(result.output_files.values())}
                )
            else:
                return PhaseResult(
                    phase=Phase.MERGE,
                    success=False,
                    reused=False,
                    needs_work=False,
                    message="Merge failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Merge phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.MERGE,
                success=False,
                reused=False,
                needs_work=False,
                message="Merge failed with exception",
                error=str(e)
            )
