"""JobPhase — initialises ``job.yaml`` and resolves crop parameters.

This is the first phase in the pipeline and has no dependencies.  Every other
phase declares ``JobPhase`` as a dependency so that job-level data (source
metadata, crop params, force-wipe flag) is always available before any phase
does real work.

Responsibilities:
- Validate the source video against any existing ``job.yaml``.
- Create or update ``job.yaml`` with current source metadata.
- Resolve crop parameters (manual → cached → auto-detect).
- Propagate ``force_wipe=True`` to downstream phases when ``--force`` is
  provided and a source mismatch is detected.
- Check available disk space before starting work.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pyqenc.models import (
    CropParams,
    PhaseOutcome,
    PipelineConfig,
    VideoMetadata,
)
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.state import ArtifactState, JobState
from pyqenc.utils.disk_space import log_disk_space_info

logger = logging.getLogger(__name__)

_JOB_YAML_FILENAME = "job.yaml"


# ---------------------------------------------------------------------------
# JobPhaseResult — extends PhaseResult with job-specific payload
# ---------------------------------------------------------------------------

@dataclass
class JobPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying job-level data for downstream phases.

    Attributes:
        job:        Loaded/created ``JobState`` (source metadata + crop).
        crop:       Resolved crop parameters; ``None`` only when crop detection
                    has not yet run (scan mode without an existing ``job.yaml``).
        force_wipe: ``True`` when ``--force`` was provided and a source mismatch
                    was detected; downstream phases must delete their own output
                    directories and phase parameter YAMLs before proceeding.
    """

    job:        JobState | None = field(default=None)
    crop:       CropParams | None = field(default=None)
    force_wipe: bool = field(default=False)


# ---------------------------------------------------------------------------
# JobPhase
# ---------------------------------------------------------------------------

class JobPhase:
    """Phase object that initialises ``job.yaml`` and resolves crop parameters.

    This phase has no dependencies and is a declared dependency of every other
    phase.  It is the only phase that performs disk-space checking and pipeline
    intro logging.

    Args:
        config:  Full pipeline configuration.
        phases:  Phase registry (unused — ``JobPhase`` has no dependencies).
    """

    name: str = "job"
    dependencies: list[Phase] = []

    def __init__(
        self,
        config: PipelineConfig,
        phases: dict[type[Phase], Phase] | None = None,
    ) -> None:
        self._config = config
        self.result: JobPhaseResult | None = None
        # JobPhase has no dependencies; phases registry is accepted but unused.

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> JobPhaseResult:
        """Load existing ``job.yaml`` without running crop detection or writing files.

        Returns:
            ``JobPhaseResult`` with ``force_wipe=False``; ``is_complete`` is
            ``True`` when ``job.yaml`` exists and the source path matches.
        """
        if self.result is not None:
            return self.result

        job = JobState.load(self._config.work_dir / _JOB_YAML_FILENAME)

        if job is None:
            result = JobPhaseResult(
                outcome   = PhaseOutcome.DRY_RUN,
                artifacts = [Artifact(
                    path  = self._config.work_dir / _JOB_YAML_FILENAME,
                    state = ArtifactState.ABSENT,
                )],
                message    = "job.yaml not found",
                job        = None,
                crop       = None,
                force_wipe = False,
            )
        else:
            result = JobPhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = [Artifact(
                    path  = self._config.work_dir / _JOB_YAML_FILENAME,
                    state = ArtifactState.COMPLETE,
                )],
                message    = "job.yaml loaded",
                job        = job,
                crop       = job.crop,
                force_wipe = False,
            )

        self.result = result
        return result

    def run(self, dry_run: bool = False) -> JobPhaseResult:
        """Validate source, create/update ``job.yaml``, resolve crop parameters.

        Sequence:
        1. Emit phase banner.
        2. Check disk space (execute mode only).
        3. Detect source mismatch against existing ``job.yaml``.
           - No mismatch or no existing file → continue.
           - Mismatch without ``--force`` → return ``FAILED``.
           - Mismatch with ``--force`` → set ``force_wipe=True``, delete
             ``job.yaml`` and all phase parameter YAMLs, continue.
        4. In dry-run mode: return ``DRY_RUN`` if ``job.yaml`` is absent.
        5. Create/update ``job.yaml`` with current source metadata.
        6. Resolve crop (manual → cached → detect).
        7. Cache and return result.

        Args:
            dry_run: When ``True``, report what would be done without writing
                     any files.

        Returns:
            ``JobPhaseResult`` with ``job``, ``crop``, and ``force_wipe`` set.
        """
        # Disk space check (execute mode only)
        if not dry_run:
            log_disk_space_info(
                source_video         = self._config.source_video,
                work_dir             = self._config.work_dir,
                min_num_strategies   = 1,
                max_num_strategies   = len(self._config.strategies),
                include_optimization = self._config.optimize,
                chunking_mode        = self._config.chunking_mode,
            )
            # Insufficient space. Previously we stopped here, but now I don't want to block, just notify the user.
            #if not sufficient:
            #    result = JobPhaseResult(
            #        outcome   = PhaseOutcome.FAILED,
            #        artifacts = [],
            #        message   = "Insufficient disk space",
            #        error     = "Free up space or use a different work directory.",
            #        job        = None,
            #        crop       = None,
            #        force_wipe = False,
            #    )
            #    self.result = result
            #    return result

        # Source mismatch detection
        force_wipe, failed = self._check_source_mismatch(dry_run)
        if failed:
            result = JobPhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = [],
                message   = "Source file mismatch — aborting",
                error     = "Re-run with --force to wipe existing artifacts and continue.",
                job        = None,
                crop       = None,
                force_wipe = False,
            )
            self.result = result
            return result

        # Dry-run: if job.yaml absent, report DRY_RUN
        if dry_run:
            existing = JobState.load(self._config.work_dir / _JOB_YAML_FILENAME)
            if existing is None:
                result = JobPhaseResult(
                    outcome   = PhaseOutcome.DRY_RUN,
                    artifacts = [Artifact(
                        path  = self._config.work_dir / _JOB_YAML_FILENAME,
                        state = ArtifactState.ABSENT,
                    )],
                    message    = "Would create job.yaml",
                    job        = None,
                    crop       = None,
                    force_wipe = False,
                )
                self.result = result
                return result
            # job.yaml exists and source matches — reused
            result = JobPhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = [Artifact(
                    path  = self._config.work_dir / _JOB_YAML_FILENAME,
                    state = ArtifactState.COMPLETE,
                )],
                message    = "job.yaml already up to date",
                job        = existing,
                crop       = existing.crop,
                force_wipe = False,
            )
            self.result = result
            return result

        # Check whether job.yaml existed before we create/update it
        did_work = (JobState.load(self._config.work_dir / _JOB_YAML_FILENAME) is None) or force_wipe

        # Execute mode: create/update job.yaml (force fresh write on force_wipe)
        job = self._create_or_update_job(force=force_wipe)

        # Resolve crop parameters
        crop = self._resolve_crop(job)

        # Persist updated job state (with crop)
        job.crop = crop
        job.save(self._config.work_dir / _JOB_YAML_FILENAME)

        if did_work:
            logger.debug("Job initialised: %s", self._config.work_dir / _JOB_YAML_FILENAME)
        else:
            logger.debug("Job: reusing existing job.yaml")

        result = JobPhaseResult(
            outcome   = PhaseOutcome.COMPLETED if did_work else PhaseOutcome.REUSED,
            artifacts = [Artifact(
                path  = self._config.work_dir / _JOB_YAML_FILENAME,
                state = ArtifactState.COMPLETE,
            )],
            message    = "job.yaml initialised" if did_work else "job.yaml already up to date",
            job        = job,
            crop       = crop,
            force_wipe = force_wipe,
        )
        self.result = result
        return result

    def _check_source_mismatch(self, dry_run: bool) -> tuple[bool, bool]:
        """Check for source mismatch against existing ``job.yaml``.

        On ``--force`` + mismatch, sets ``force_wipe=True`` and logs a warning.
        Downstream phases are responsible for wiping their own artifacts when
        they see ``force_wipe=True`` on ``JobPhase.result``.

        Returns:
            ``(force_wipe, failed)`` tuple.
            - ``force_wipe=True`` when ``--force`` was provided and a mismatch
              was detected; downstream phases must wipe their own artifacts.
            - ``failed=True`` when a mismatch was detected without ``--force``
              in execute mode; caller should return ``FAILED``.
        """
        existing = JobState.load(self._config.work_dir / _JOB_YAML_FILENAME)
        if existing is None:
            return False, False

        mismatches = self._find_source_mismatches(existing)
        if not mismatches:
            return False, False

        mismatch_desc = "; ".join(
            f"{field}: persisted={old!r}, current={new!r}"
            for field, old, new in mismatches
        )

        if dry_run:
            logger.warning(
                "Source file mismatch detected (dry-run — no action taken): %s",
                mismatch_desc,
            )
            return False, False

        if self._config.force:
            logger.warning(
                "Source file mismatch detected (--force — downstream phases will wipe their own artifacts): %s",
                mismatch_desc,
            )
            return True, False

        logger.critical(
            "Source file mismatch detected — stopping execution.  "
            "Re-run with --force to wipe existing artifacts and continue with the new source.  "
            "Mismatch: %s",
            mismatch_desc,
        )
        return False, True

    def _find_source_mismatches(
        self,
        existing: JobState,
    ) -> list[tuple[str, object, object]]:
        """Compare persisted source metadata against the current source file.

        Checks path, file size, and resolution.

        Returns:
            List of ``(field_name, persisted_value, current_value)`` tuples.
        """
        mismatches: list[tuple[str, object, object]] = []
        persisted = existing.source

        if persisted.path.resolve() != self._config.source_video.resolve():
            mismatches.append(("path", str(persisted.path), str(self._config.source_video)))
            return mismatches

        try:
            current_size = self._config.source_video.stat().st_size
        except OSError:
            current_size = None

        if persisted._file_size_bytes is not None and current_size is not None:
            if persisted._file_size_bytes != current_size:
                mismatches.append(("file_size_bytes", persisted._file_size_bytes, current_size))

        if not mismatches and persisted._resolution is not None:
            live_meta = VideoMetadata(path=self._config.source_video)
            current_res = live_meta.resolution
            if current_res is not None and current_res != persisted._resolution:
                mismatches.append(("resolution", persisted._resolution, current_res))

        return mismatches

    def _create_or_update_job(self, force: bool = False) -> JobState:
        """Create or load ``JobState`` with current source metadata.

        Probes the source video for all metadata fields and returns a
        ``JobState`` ready to be persisted.  Crop is NOT set here — that is
        done by ``_resolve_crop()`` after this call.

        Args:
            force: When ``True``, ignore any existing ``job.yaml`` and build a
                   fresh ``JobState`` from the current source file.  Used when
                   ``--force`` detected a source mismatch — the old metadata is
                   stale and must be replaced.
        """
        if not force:
            existing = JobState.load(self._config.work_dir / _JOB_YAML_FILENAME)
            if existing is not None:
                logger.debug("Loaded existing job.yaml")
                return existing

        source = VideoMetadata(path=self._config.source_video)
        # Eagerly probe all fields so they are persisted in job.yaml
        _ = source.file_size_bytes
        _ = source.duration_seconds
        _ = source.fps
        _ = source.resolution
        _ = source.frame_count

        job = JobState(source=source)
        logger.info("Initialized job.yaml for new pipeline run")
        return job

    def _resolve_crop(self, job: JobState) -> CropParams:
        """Resolve crop parameters: manual → cached → auto-detect.

        Args:
            job: Current ``JobState`` (may already have ``crop`` set from cache).

        Returns:
            Resolved ``CropParams`` (all-zero if no borders found).
        """
        from pyqenc.utils.crop import detect_crop_parameters

        # 1. Manual override from config
        if self._config.crop_params is not None:
            c = self._config.crop_params
            logger.info(f"Cropping: {c.display()} (manual)")
            return c

        # 2. Cached in job.yaml
        if job.crop is not None:
            c = job.crop
            logger.info(f"Cropping: {c.display()} (cached)")
            return c

        # 3. Auto-detect
        logger.info("Cropping: detecting black borders...")
        source = VideoMetadata(path=self._config.source_video)
        crop = detect_crop_parameters(source)
        return crop
