"""Pipeline orchestrator — thin driver that builds the phase registry and iterates it.

The orchestrator's only responsibilities are:

1. Build the ``dict[type[Phase], Phase]`` registry via ``_build_registry``.
2. Iterate the registry values in execution order, calling ``phase.run(dry_run)``.
3. Log pipeline-level concerns: start/stop, per-phase boundary markers, overall outcome.
4. Stop on ``FAILED`` or ``DRY_RUN`` outcomes and return a ``PipelineResult``.

All phase-specific logic (artifact paths, sidecar formats, crop handling, strategy
resolution, cleanup) lives inside the individual phase objects.
"""
# CHerSun 2026

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from pyqenc.constants import (
    CHUNKS_DIR,
    ENCODED_OUTPUT_DIR,
    ENCODING_WORKSPACE_DIR,
    EXTRACTED_DIR,
    THICK_LINE,
)
from pyqenc.models import CleanupLevel, PhaseOutcome, PipelineConfig
from pyqenc.phase import Phase, PhaseResult, _build_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PipelineResult — public result type consumed by api.py and tests
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Result of a complete pipeline execution.

    Attributes:
        success:              ``True`` when all phases completed or were reused.
        phases_executed:      Names of phases that performed real work this run.
        phases_reused:        Names of phases that reused existing artifacts.
        phases_needing_work:  Names of phases that need work (dry-run mode only).
        output_files:         Final output file paths (populated by ``MergePhase``).
        error:                Error description when ``success`` is ``False``.
    """

    success:             bool
    phases_executed:     list[str]      = field(default_factory=list)
    phases_reused:       list[str]      = field(default_factory=list)
    phases_needing_work: list[str]      = field(default_factory=list)
    output_files:        list[Path]     = field(default_factory=list)
    error:               str | None     = None


# ---------------------------------------------------------------------------
# PipelineOrchestrator
# ---------------------------------------------------------------------------

class PipelineOrchestrator:
    """Thin driver that constructs and runs phase objects in execution order.

    Args:
        config: Full pipeline configuration shared by all phases.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def run(self, dry_run: bool = True) -> PipelineResult:
        """Execute the pipeline by iterating phase objects in registry order.

        Builds the phase registry, then calls ``phase.run(dry_run)`` on each
        phase in insertion order.  Stops on the first ``FAILED`` or (in dry-run
        mode) ``DRY_RUN`` outcome.

        Args:
            dry_run: When ``True``, phases report what work would be done
                     without executing it (default: ``True``).

        Returns:
            ``PipelineResult`` summarising the run.
        """
        registry = _build_registry(self.config)

        phases_executed:     list[str] = []
        phases_reused:       list[str] = []
        phases_needing_work: list[str] = []
        output_files:        list[Path] = []

        phases = list(registry.values())

        for phase in phases:
            result: PhaseResult = phase.run(dry_run=dry_run)

            if result.outcome == PhaseOutcome.COMPLETED:
                phases_executed.append(phase.name)
                # Collect output files from MergePhase result if available
                _collect_output_files(result, output_files)

            elif result.outcome == PhaseOutcome.REUSED:
                phases_reused.append(phase.name)
                _collect_output_files(result, output_files)

            elif result.outcome == PhaseOutcome.DRY_RUN:
                phases_needing_work.append(phase.name)
                if dry_run:
                    logger.info("")
                    logger.info("[DRY-RUN] Stopping at first incomplete phase.")
                    logger.info("[DRY-RUN] Run with -y to execute.")
                    break

            elif result.outcome == PhaseOutcome.FAILED:
                logger.critical(
                    "Phase %s failed: %s",
                    phase.name,
                    result.error or result.message,
                )
                return PipelineResult(
                    success             = False,
                    phases_executed     = phases_executed,
                    phases_reused       = phases_reused,
                    phases_needing_work = phases_needing_work,
                    output_files        = output_files,
                    error               = f"Phase {phase.name} failed: {result.error or result.message}",
                )

        # Overall summary
        logger.info(THICK_LINE)
        if dry_run:
            logger.info("[DRY-RUN] Pipeline preview completed")
            logger.info("Phases complete:      %d", len(phases_reused) + len(phases_executed))
            logger.info("Phases needing work:  %d", len(phases_needing_work))
        else:
            logger.info("Pipeline execution completed")
            logger.info("Phases executed: %d", len(phases_executed))
            logger.info("Phases reused:   %d", len(phases_reused))
            if output_files:
                logger.info("Output files: %d", len(output_files))
                for f in output_files:
                    logger.info("  - %s", f)
        logger.info(THICK_LINE)

        # Post-pipeline full cleanup (Req 12.4, 12.5)
        if not dry_run and self.config.cleanup >= CleanupLevel.ALL:
            _run_post_pipeline_cleanup(self.config.work_dir, registry)

        return PipelineResult(
            success             = True,
            phases_executed     = phases_executed,
            phases_reused       = phases_reused,
            phases_needing_work = phases_needing_work,
            output_files        = output_files,
            error               = None,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _collect_output_files(result: PhaseResult, output_files: list[Path]) -> None:
    """Append any final output paths from a phase result to ``output_files``.

    ``MergePhase`` stores its ``MergedArtifact`` list in ``result.artifacts``;
    each artifact whose state is ``COMPLETE`` and whose path is inside a
    ``final/`` directory is considered a pipeline output file.
    """
    for artifact in result.complete:
        # Only collect artifacts that live in the final output directory.
        if "final" in artifact.path.parts:
            output_files.append(artifact.path)


def _run_post_pipeline_cleanup(work_dir: Path, registry: dict[type[Phase], Phase]) -> None:
    """Delete remaining intermediate files/directories after full pipeline success.

    Called when ``config.cleanup >= CleanupLevel.ALL`` and the pipeline
    completed successfully.  Deletes ``encoding/``, ``encoded/``, and
    ``chunks/`` entirely.  For ``extracted/``, only the video and audio
    artifact files are deleted — subtitles, chapters, and attachments are
    retained (Req 12.4, 12.5).

    Args:
        work_dir: Pipeline working directory.
        registry: Phase registry; used to resolve extraction artifact paths.
    """
    # Delete whole intermediate directories
    for directory in (
        work_dir / ENCODING_WORKSPACE_DIR,
        work_dir / ENCODED_OUTPUT_DIR,
        work_dir / CHUNKS_DIR,
    ):
        if directory.exists():
            try:
                shutil.rmtree(directory)
                logger.info("Full cleanup: removed %s", directory.name)
            except OSError as exc:
                logger.warning("Full cleanup: could not remove %s: %s", directory.name, exc)

    # Selectively delete only video and audio files from extracted/,
    # preserving subtitles, chapters, and attachments.
    _cleanup_extracted(work_dir, registry)


def _cleanup_extracted(work_dir: Path, registry: dict[type[Phase], Phase]) -> None:
    """Delete only video and audio files from ``extracted/``, keeping everything else.

    Subtitles, chapters, and attachments in ``extracted/`` are preserved because
    they are not reproduced by any downstream phase and are needed for remuxing
    or manual use.

    Uses the ``ExtractionPhase`` result artifacts to identify exactly which files
    to delete.  Falls back to deleting the whole directory if the extraction
    result is unavailable.

    Args:
        work_dir: Pipeline working directory.
        registry: Phase registry; used to resolve ``ExtractionPhase`` artifacts.
    """
    from pyqenc.phases.extraction import AudioArtifact, ExtractionPhase, VideoArtifact

    extracted_dir = work_dir / EXTRACTED_DIR
    if not extracted_dir.exists():
        return

    extraction_phase = registry.get(ExtractionPhase)
    result = extraction_phase.result if extraction_phase is not None else None

    if result is None:
        # No result available — fall back to deleting the whole directory
        logger.warning(
            "Full cleanup: extraction result unavailable; removing entire %s",
            extracted_dir.name,
        )
        try:
            shutil.rmtree(extracted_dir)
        except OSError as exc:
            logger.warning("Full cleanup: could not remove %s: %s", extracted_dir.name, exc)
        return

    deleted = 0
    for artifact in result.artifacts:
        if not isinstance(artifact, (VideoArtifact, AudioArtifact)):
            continue
        if artifact.path.exists():
            try:
                artifact.path.unlink()
                deleted += 1
            except OSError as exc:
                logger.warning("Full cleanup: could not delete %s: %s", artifact.path.name, exc)

    logger.info("Full cleanup: removed %d video/audio file(s) from %s", deleted, extracted_dir.name)
