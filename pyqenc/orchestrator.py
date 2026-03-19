"""Pipeline orchestrator for coordinating phase execution."""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pyqenc.constants import SUCCESS_SYMBOL_MINOR, TEMP_SUFFIX, THICK_LINE, THIN_LINE
from pyqenc.models import (
    ChunkingMode,
    CropParams,
    PhaseOutcome,
    PipelineConfig,
)
from pyqenc.state import JobState, JobStateManager

logger = logging.getLogger(__name__)


def _load_chunks_from_sidecars(
    chunks_dir: Path,
    chunk_files: list[Path],
) -> list["ChunkMetadata"]:
    """Load ChunkMetadata from chunk sidecar YAMLs; fall back to stub if sidecar absent.

    Args:
        chunks_dir:  Directory containing chunk ``.mkv`` and ``.yaml`` sidecar files.
        chunk_files: Sorted list of chunk ``.mkv`` paths.

    Returns:
        List of ``ChunkMetadata`` instances, sorted by filename.
    """
    from pyqenc.models import ChunkMetadata
    from pyqenc.state import ChunkSidecar

    chunks: list[ChunkMetadata] = []
    for chunk_file in chunk_files:
        sidecar_path = chunk_file.with_suffix(".yaml")
        if sidecar_path.exists():
            try:
                import yaml as _yaml
                with sidecar_path.open("r", encoding="utf-8") as fh:
                    data = _yaml.safe_load(fh)
                sidecar = ChunkSidecar.from_yaml_dict(data, chunk_id=chunk_file.stem, path=chunk_file)
                chunks.append(sidecar.chunk)
                continue
            except Exception:
                pass
        # Fallback: stub with zero timestamps
        chunks.append(ChunkMetadata(
            path=chunk_file,
            chunk_id=chunk_file.stem,
            start_timestamp=0.0,
            end_timestamp=0.0,
        ))
    return chunks



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
    phase:    Phase
    outcome:  PhaseOutcome
    message:  str
    error:    str | None          = None
    metadata: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Backward-compatible derived properties
    # ------------------------------------------------------------------

    @property
    def success(self) -> bool:
        """True when the phase completed or reused existing artifacts."""
        return self.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED)

    @property
    def reused(self) -> bool:
        """True when existing artifacts were reused without new work."""
        return self.outcome == PhaseOutcome.REUSED

    @property
    def needs_work(self) -> bool:
        """True when the phase is in dry-run mode and work would be required."""
        return self.outcome == PhaseOutcome.DRY_RUN


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

    def __init__(self, config: PipelineConfig, state_manager: JobStateManager):
        """Initialize orchestrator with configuration and job state manager.

        Args:
            config:        Pipeline configuration
            state_manager: Job state manager for YAML-based state persistence
        """
        self.config        = config
        self.state_manager = state_manager
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

        # Source-file binding check via JobStateManager (Req 1.2)
        if not self.state_manager.validate(dry_run=dry_run):
            return PipelineResult(
                success=False,
                phases_executed=[],
                phases_reused=[],
                phases_needing_work=[],
                output_files=[],
                error="Source file mismatch — aborting. Use --force to override.",
            )

        # Ensure job.yaml is written with current source metadata (first run)
        # Also resolve crop parameters here at job level — before any phases.
        if not dry_run:
            from pyqenc.models import VideoMetadata
            existing_job = self.state_manager.load_job()
            if existing_job is None:
                job = JobState(source=VideoMetadata(path=self.config.source_video))
                sv = job.source
                _ = sv.file_size_bytes
                _ = sv.duration_seconds
                _ = sv.fps
                _ = sv.resolution
                _ = sv.frame_count
                self.state_manager.save_job(job)
                logger.info("Initialized job.yaml for new pipeline run")

            # Crop: manual → cached → detect (runs on source video, once per job)
            self._resolve_crop_params(VideoMetadata(path=self.config.source_video))

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
                outcome=PhaseOutcome.FAILED,
                message=f"Unknown phase: {phase}",
                error="Phase not implemented"
            )

    def _get_crop_params(self) -> CropParams | None:
        """Read crop parameters from ``job.yaml``.

        Returns ``None`` only when detection has not yet run (crop_params is None).
        Returns the ``CropParams`` instance (possibly all-zero) once extraction
        has completed.

        Returns:
            CropParams if source video has crop data, None if not yet detected.
        """
        job = self.state_manager.load_job()
        if job is None:
            return None
        return job.crop

    def _execute_extraction(self, dry_run: bool) -> PhaseResult:
        """Execute extraction phase."""
        from pyqenc.phases.extraction import extract_streams

        output_dir = self.config.work_dir / "extracted"

        try:
            result = extract_streams(
                source_video=self.config.source_video,
                output_dir=output_dir,
                include=self.config.include,
                exclude=self.config.exclude,
                force=False,
                dry_run=dry_run,
            )

            if result.outcome == PhaseOutcome.DRY_RUN:
                return PhaseResult(
                    phase=Phase.EXTRACTION,
                    outcome=PhaseOutcome.DRY_RUN,
                    message="Needs work: streams not extracted",
                    metadata={"result": result}
                )

            if result.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED):
                video_count = 1 if result.video else 0
                return PhaseResult(
                    phase=Phase.EXTRACTION,
                    outcome=result.outcome,
                    message=f"Extracted {video_count} video, {len(result.audio)} audio streams",
                    metadata={"video": result.video, "audio": result.audio},
                )
            else:
                return PhaseResult(
                    phase=Phase.EXTRACTION,
                    outcome=PhaseOutcome.FAILED,
                    message="Extraction failed",
                    error=result.error,
                )

        except Exception as e:
            logger.error("Extraction phase error: %s", e, exc_info=True)
            return PhaseResult(
                phase=Phase.EXTRACTION,
                outcome=PhaseOutcome.FAILED,
                message="Extraction failed with exception",
                error=str(e),
            )

    def _resolve_crop_params(self, video: "VideoMetadata | None" = None) -> "CropParams":
        """Resolve crop parameters at job level: manual → cached → detect → save.

        Runs on the source video directly, before any phases.  Called once
        during job initialisation in ``run()``.

        Priority:
        1. Manual crop from ``config.crop_params`` — always used as-is.
        2. Cached value in ``job.yaml`` — reused if present (detection already ran).
        3. Auto-detection via ffmpeg cropdetect on the source video.

        Returns:
            Resolved ``CropParams`` (all-zero if no borders found).
        """
        from pyqenc.models import CropParams, VideoMetadata
        from pyqenc.utils.crop import detect_crop_parameters

        # 1. Manual override
        if self.config.crop_params is not None:
            c = self.config.crop_params
            logger.info(
                "User-provided cropping: %d top, %d bottom, %d left, %d right",
                c.top, c.bottom, c.left, c.right,
            )
            return c

        # 2. Cached in job.yaml
        job = self.state_manager.load_job()
        if job is not None and job.crop is not None:
            c = job.crop
            logger.info(
                "Detected cropping: %d top, %d bottom, %d left, %d right (cached)",
                c.top, c.bottom, c.left, c.right,
            )
            return c

        # 3. Detect on source video and persist
        source = video or VideoMetadata(path=self.config.source_video)
        logger.info("Cropping: detecting black borders...")
        crop = detect_crop_parameters(source)
        if job is not None:
            job.crop = crop
            self.state_manager.save_job(job)
        return crop

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

        existing_job = self.state_manager.load_job()
        if existing_job is not None:
            job = existing_job
        else:
            from pyqenc.models import VideoMetadata
            job = JobState(source=VideoMetadata(path=video_file))

        try:
            result = chunk_video(
                video_file=video_file,
                output_dir=output_dir,
                state_manager=self.state_manager,
                job=job,
                chunking_mode=self.config.chunking_mode,
                dry_run=dry_run,
            )

            if dry_run and result.needs_work:
                return PhaseResult(
                    phase=Phase.CHUNKING,
                    outcome=PhaseOutcome.DRY_RUN,
                    message="Needs work: video not chunked",
                    metadata={"result": result}
                )

            if result.success:
                # Verify chunk frame total matches source
                job = self.state_manager.load_job()
                source_frame_count = job.source._frame_count if job else None
                if source_frame_count and result.total_frames != source_frame_count:
                    diff = result.total_frames - source_frame_count
                    sign = "+" if diff > 0 else ""
                    if self.config.chunking_mode == ChunkingMode.REMUX:
                        note = "This is expected with stream-copy I-frame snapping."
                    else:
                        note = "Frame count mismatch — check chunking output."
                    logger.warning(
                        "Chunk frame total (%d) differs from source (%d) by %s%d frames. %s",
                        result.total_frames, source_frame_count, sign, diff, note,
                    )
                elif source_frame_count:
                    logger.info(
                        "Frame count verified: %d chunk frames == %d source frames %s",
                        result.total_frames, source_frame_count, SUCCESS_SYMBOL_MINOR
                    )

                return PhaseResult(
                    phase=Phase.CHUNKING,
                    outcome=PhaseOutcome.REUSED if result.reused else PhaseOutcome.COMPLETED,
                    message=f"Created {len(result.chunks)} chunks ({result.total_frames} frames)",
                    metadata={"chunks": result.chunks}
                )
            else:
                return PhaseResult(
                    phase=Phase.CHUNKING,
                    outcome=PhaseOutcome.FAILED,
                    message="Chunking failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Chunking phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.CHUNKING,
                outcome=PhaseOutcome.FAILED,
                message="Chunking failed with exception",
                error=str(e)
            )

    def _execute_optimization(self, dry_run: bool) -> PhaseResult:
        """Execute optimization phase."""
        from pyqenc.config import ConfigManager
        from pyqenc.models import ChunkMetadata
        from pyqenc.phases.optimization import find_optimal_strategy

        chunks_dir = self.config.work_dir / "chunks"

        if not chunks_dir.exists():
            return PhaseResult(
                phase=Phase.OPTIMIZATION,
                outcome=PhaseOutcome.FAILED,
                message="Chunks directory not found",
                error="Chunking phase must complete first",
            )

        chunk_files = sorted(chunks_dir.glob("*.mkv"))
        if not chunk_files:
            return PhaseResult(
                phase=Phase.OPTIMIZATION,
                outcome=PhaseOutcome.FAILED,
                message="No chunks found in chunks directory",
                error="Chunking phase must complete first",
            )

        # Fast-path: reuse cached result from optimization.yaml
        opt_params = self.state_manager.load_optimization()
        if opt_params is not None and opt_params.optimal_strategy and not dry_run:
            optimal = opt_params.optimal_strategy
            logger.info("Optimization already completed — reusing result: %s", optimal)
            self._optimal_strategy = optimal
            return PhaseResult(
                phase=Phase.OPTIMIZATION,
                outcome=PhaseOutcome.REUSED,
                message=f"Optimal strategy (cached): {optimal}",
                metadata={"optimal_strategy": optimal},
            )

        config_manager = ConfigManager()

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

        # Load chunks from chunk sidecars; fall back to filesystem glob
        chunks = _load_chunks_from_sidecars(chunks_dir, chunk_files)

        # Persisted test chunk IDs from optimization.yaml
        persisted_chunk_ids = opt_params.test_chunks if opt_params is not None else []

        try:
            crop_params = self._get_crop_params()
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
                dry_run=dry_run,
                crop_params=crop_params,
                max_parallel=self.config.max_parallel,
                persisted_chunk_ids=persisted_chunk_ids,
                state_manager=self.state_manager,
                force=self.config.force,
            )

            if dry_run:
                return PhaseResult(
                    phase=Phase.OPTIMIZATION,
                    outcome=PhaseOutcome.DRY_RUN,
                    message="Would test strategies on representative chunks",
                )

            if result.success:
                logger.info("Strategy test results:")
                for strategy, test_result in result.test_results.items():
                    if test_result.all_passed:
                        logger.info(
                            "  %s: avg CRF %.2f, total size %.2f MB",
                            strategy,
                            test_result.avg_crf,
                            test_result.total_file_size / 1024 / 1024,
                        )
                    else:
                        logger.warning("  %s: failed to encode all test chunks", strategy)

                self._optimal_strategy = result.optimal_strategy
                logger.info("Optimal strategy: %s", result.optimal_strategy)
                return PhaseResult(
                    phase=Phase.OPTIMIZATION,
                    outcome=PhaseOutcome.COMPLETED,
                    message=f"Optimal strategy: {result.optimal_strategy}",
                    metadata={"optimal_strategy": result.optimal_strategy},
                )
            else:
                return PhaseResult(
                    phase=Phase.OPTIMIZATION,
                    outcome=PhaseOutcome.FAILED,
                    message="Optimization failed",
                    error=result.error,
                )

        except Exception as e:
            logger.error("Optimization phase error: %s", e, exc_info=True)
            return PhaseResult(
                phase=Phase.OPTIMIZATION,
                outcome=PhaseOutcome.FAILED,
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
        from pyqenc.models import ChunkMetadata
        from pyqenc.phases.encoding import encode_all_chunks

        chunks_dir = self.config.work_dir / "chunks"

        if not chunks_dir.exists():
            return PhaseResult(
                phase=Phase.ENCODING,
                outcome=PhaseOutcome.FAILED,
                message="Chunks directory not found",
                error="Chunking phase must complete first"
            )

        # Load chunks from chunk sidecars; fall back to filesystem glob
        chunk_files = sorted(chunks_dir.glob("*.mkv"))
        if not chunk_files:
            return PhaseResult(
                phase=Phase.ENCODING,
                outcome=PhaseOutcome.FAILED,
                message="No chunks found in chunks directory",
                error="Chunking phase must complete first"
            )
        chunks = _load_chunks_from_sidecars(chunks_dir, chunk_files)
        logger.info("Found %d chunks to encode", len(chunks))

        config_manager = ConfigManager()

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

        logger.info("Resolved %d strategies: %s", len(strategy_names), strategy_names)

        try:
            crop_params = self._get_crop_params()
            if crop_params:
                logger.info("Crop params loaded: %s", crop_params)
            else:
                logger.info("No crop params; encoding without crop")

            result = encode_all_chunks(
                chunks=chunks,
                reference_dir=chunks_dir,
                strategies=strategy_names,
                quality_targets=self.config.quality_targets,
                work_dir=self.config.work_dir,
                config_manager=config_manager,
                max_parallel=self.config.max_parallel,
                force=self.config.force,
                crop_params=crop_params,
                dry_run=dry_run,
                state_manager=self.state_manager,
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
                    outcome=PhaseOutcome.DRY_RUN,
                    message=f"Needs work: {chunks_needing_work} chunk+strategy combinations need encoding",
                    metadata={"result": result}
                )

            if result.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED):
                return PhaseResult(
                    phase=Phase.ENCODING,
                    outcome=PhaseOutcome.REUSED if result.reused_count > 0 and result.encoded_count == 0 else PhaseOutcome.COMPLETED,
                    message=f"Encoded {result.encoded_count} chunks, reused {result.reused_count}",
                    metadata={"encoded_chunks": result.encoded_chunks}
                )
            else:
                return PhaseResult(
                    phase=Phase.ENCODING,
                    outcome=PhaseOutcome.FAILED,
                    message=f"Encoding failed: {len(result.failed_chunks)} chunks failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Encoding phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.ENCODING,
                outcome=PhaseOutcome.FAILED,
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
                outcome=PhaseOutcome.FAILED,
                message="No extracted audio files found",
                error="Extraction phase must complete first"
            )

        output_dir = self.config.work_dir / "audio"

        try:
            result = process_audio_streams(
                audio_files=audio_files,
                output_dir=output_dir,
                audio_convert=self.config.audio_convert,
                audio_codec=self.config.audio_codec,
                audio_base_bitrate=self.config.audio_base_bitrate,
                dry_run=dry_run,
            )

            if dry_run and result.needs_work:
                return PhaseResult(
                    phase=Phase.AUDIO,
                    outcome=PhaseOutcome.DRY_RUN,
                    message="Needs work: audio not processed",
                    metadata={"result": result}
                )

            if result.success:
                return PhaseResult(
                    phase=Phase.AUDIO,
                    outcome=PhaseOutcome.REUSED if result.reused else PhaseOutcome.COMPLETED,
                    message=f"Processed {len(result.output_files)} audio delivery file(s)",
                    metadata={
                        "output_files": result.output_files,
                    }
                )
            else:
                return PhaseResult(
                    phase=Phase.AUDIO,
                    outcome=PhaseOutcome.FAILED,
                    message="Audio processing failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Audio phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.AUDIO,
                outcome=PhaseOutcome.FAILED,
                message="Audio processing failed with exception",
                error=str(e)
            )

    def _get_reference_size(self, job: "JobState") -> int | None:
        """Return the reference video size in bytes for merge summary savings calculation.

        Looks for the extracted video stream in ``work_dir/extracted/``.
        Falls back to the source file size if no extracted video is found.

        Args:
            job: Current job state (used for source size fallback).

        Returns:
            Size in bytes, or ``None`` if unavailable.
        """
        extracted_dir = self.config.work_dir / "extracted"
        if extracted_dir.exists():
            # The extracted video stream is a .mkv file that does NOT contain "(audio-"
            video_files = [
                f for f in extracted_dir.glob("*.mkv")
                if "(audio-" not in f.name
            ]
            if video_files:
                try:
                    return video_files[0].stat().st_size
                except OSError as exc:
                    logger.warning("Could not stat extracted video file: %s", exc)

        # Fallback: source file size
        try:
            return job.source.file_size_bytes
        except Exception as exc:
            logger.warning("Could not determine reference size: %s", exc)
            return None

    def _emit_merge_summary(
        self,
        result:  "MergeResult",
        job:     "JobState",
    ) -> None:
        """Emit a human-readable post-merge summary at ``info`` level.

        Determines the operating mode from ``self._optimal_strategy``, resolves
        the reference size, and delegates formatting to the appropriate
        ``fmt_merge_summary_*`` function.

        Args:
            result: Completed merge result.
            job:    Current job state (for reference size fallback).
        """
        from pyqenc.phases.merge import MergeResult
        from pyqenc.utils.log_format import fmt_merge_summary_all, fmt_merge_summary_optimal

        if not result.output_files:
            return

        reference_size_bytes = self._get_reference_size(job)
        quality_targets      = self.config.quality_targets or []

        # Collect output file sizes
        sizes_bytes: dict[str, int] = {}
        for strategy, output_file in result.output_files.items():
            try:
                sizes_bytes[strategy] = output_file.stat().st_size
            except OSError as exc:
                logger.warning("Could not stat output file %s: %s", output_file.name, exc)
                sizes_bytes[strategy] = 0

        if self._optimal_strategy and len(result.output_files) == 1:
            # Optimal-strategy mode: single-strategy key-value block
            strategy    = next(iter(result.output_files))
            output_file = result.output_files[strategy]
            size_bytes  = sizes_bytes.get(strategy, 0)
            metrics     = result.final_metrics.get(strategy, {})
            targets_met = result.targets_met.get(strategy, False)

            lines = fmt_merge_summary_optimal(
                output_file=output_file,
                size_bytes=size_bytes,
                reference_size_bytes=reference_size_bytes,
                quality_targets=quality_targets,
                metrics=metrics,
                targets_met=targets_met,
            )
        else:
            # All-strategies mode: table format
            lines = fmt_merge_summary_all(
                output_files=result.output_files,
                sizes_bytes=sizes_bytes,
                reference_size_bytes=reference_size_bytes,
                quality_targets=quality_targets,
                final_metrics=result.final_metrics,
                targets_met=result.targets_met,
            )

        for line in lines:
            logger.info(line)

    def _execute_merge(self, dry_run: bool) -> PhaseResult:
        """Execute merge phase.

        Args:
            dry_run: If True, only report status

        Returns:
            PhaseResult with execution details
        """
        # Import here to avoid circular dependencies
        from pyqenc.constants import ENCODED_ATTEMPT_NAME_PATTERN
        from pyqenc.phases.merge import merge_final_video
        from pyqenc.utils.ffmpeg_runner import get_frame_count

        encoded_dir = self.config.work_dir / "encoded"
        output_dir  = self.config.work_dir / "final"
        chunks_dir  = self.config.work_dir / "chunks"

        if not encoded_dir.exists():
            return PhaseResult(
                phase=Phase.MERGE,
                outcome=PhaseOutcome.FAILED,
                message="Encoded directory not found",
                error="Encoding phase must complete first"
            )

        # Get source frame count for verification from job.yaml
        source_frame_count: int | None = None
        job = self.state_manager.load_job()
        if job and job.source._frame_count is not None:
            source_frame_count = job.source._frame_count
            logger.debug("Source frame count from job.yaml: %d", source_frame_count)
        else:
            try:
                chunk_files = sorted(chunks_dir.glob("*.mkv"))
                if chunk_files:
                    source_frame_count = sum(get_frame_count(cf) for cf in chunk_files)
                    logger.debug("Source frame count from chunks: %d", source_frame_count)
            except Exception as e:
                logger.warning("Could not determine source frame count: %s", e)

        # Collect encoded chunks organised by chunk_id → strategy → path.
        # Use ENCODED_ATTEMPT_NAME_PATTERN to parse CRF-only filenames;
        # one file per (chunk_id, strategy) — presence on disk is proof of completion.
        encoded_chunks: dict[str, dict[str, Path]] = {}

        for strategy_dir in encoded_dir.iterdir():
            if not strategy_dir.is_dir():
                continue
            strategy_name = strategy_dir.name

            for encoded_file in sorted(strategy_dir.glob("*.mkv")):
                if TEMP_SUFFIX in encoded_file.name:
                    continue  # skip in-progress temp files
                m = ENCODED_ATTEMPT_NAME_PATTERN.match(encoded_file.name)
                if not m:
                    # Fallback: treat everything before the first '.' as chunk_id
                    chunk_id = encoded_file.stem.split(".")[0]
                else:
                    chunk_id = m.group("chunk_id")

                # One file per (chunk_id, strategy) — last one wins on duplicates
                encoded_chunks.setdefault(chunk_id, {})[strategy_name] = encoded_file

        if not encoded_chunks:
            return PhaseResult(
                phase=Phase.MERGE,
                outcome=PhaseOutcome.FAILED,
                message="No encoded chunks found",
                error="Encoding phase must complete first"
            )

        strategy_count = len({s for cs in encoded_chunks.values() for s in cs})
        logger.debug(
            "Found %d chunks across %d strategies",
            len(encoded_chunks), strategy_count,
        )

        if job is None:
            logger.critical("Job state is None — cannot determine source stem for merge")
            return PhaseResult(
                phase=Phase.MERGE,
                outcome=PhaseOutcome.FAILED,
                message="Merge failed: job state unavailable",
                error="job is None",
            )

        try:
            result = merge_final_video(
                encoded_chunks=encoded_chunks,
                output_dir=output_dir,
                source_stem=job.source.path.stem,
                source_video=job.source if job else None,
                ref_crop=job.crop if job else None,
                quality_targets=self.config.quality_targets or None,
                source_frame_count=source_frame_count,
                optimal_strategy=self._optimal_strategy,
                metrics_sampling=self.config.metrics_sampling,
                verify_frames=True,
                force=False,
                dry_run=dry_run,
            )

            if dry_run and result.needs_work:
                return PhaseResult(
                    phase=Phase.MERGE,
                    outcome=PhaseOutcome.DRY_RUN,
                    message="Needs work: final videos not merged",
                    metadata={"result": result}
                )

            if result.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED):
                # Log frame counts if available
                if result.frame_counts:
                    for strategy, frame_count in result.frame_counts.items():
                        logger.info("  %s: %d frames", strategy, frame_count)

                # Emit post-merge summary
                self._emit_merge_summary(result, job)

                return PhaseResult(
                    phase=Phase.MERGE,
                    outcome=result.outcome,
                    message=f"Merged {len(result.output_files)} final video(s)",
                    metadata={"output_files": list(result.output_files.values())}
                )
            else:
                return PhaseResult(
                    phase=Phase.MERGE,
                    outcome=PhaseOutcome.FAILED,
                    message="Merge failed",
                    error=result.error
                )

        except Exception as e:
            logger.error(f"Merge phase error: {e}", exc_info=True)
            return PhaseResult(
                phase=Phase.MERGE,
                outcome=PhaseOutcome.FAILED,
                message="Merge failed with exception",
                error=str(e)
            )
