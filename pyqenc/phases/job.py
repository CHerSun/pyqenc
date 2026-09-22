"""JobPhase — initialises ``job.yaml`` and probes source metadata.

This is the first phase in the pipeline and has no dependencies.  Every other
phase declares ``JobPhase`` as a dependency so that job-level data (source
metadata, force-wipe flag) is always available before any phase does real work.

Responsibilities:
- Validate the source video against any existing ``job.yaml``.
- Create or update ``job.yaml`` with current source metadata.
- Self-heal stale ``job.yaml`` entries (missing/invalid fps).
- Propagate ``force_wipe=True`` to downstream phases when ``--force`` is
  provided and a source mismatch is detected.
- Check available disk space before starting work.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pyqenc.constants import TEMP_SUFFIX
from pyqenc.metrics import MetricKey, MetricsCollector
from pyqenc.models import (
    CleanupLevel,
    PhaseOutcome,
    VideoMetadata,
)
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseBase,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.state import JobState
from pyqenc.utils.disk_space import log_disk_space_info

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig

logger = logging.getLogger(__name__)

_JOB_YAML_FILENAME = "job.yaml"


# ---------------------------------------------------------------------------
# JobPhaseResult — extends PhaseResult with job-specific payload
# ---------------------------------------------------------------------------

@dataclass
class JobPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying job-level data for downstream phases.

    Attributes:
        job:        Loaded/created ``JobState`` (source metadata).
        force_wipe: ``True`` when ``--force`` was provided and a source mismatch
                    was detected; downstream phases must delete their own output
                    directories and phase parameter YAMLs before proceeding.
        config:     Full validated application configuration.
        work_dir:   Working directory for all pipeline artifacts.
        source:     Resolved path to the source video file.
        cleanup:    Artifact retention policy applied after encoding.
        no_metrics: When ``True``, skip writing ``metrics.yaml`` files.
    """

    job:        JobState | None  = field(default=None)
    force_wipe: bool              = field(default=False)
    config:     AppConfig | None = field(default=None)
    work_dir:   Path | None       = field(default=None)
    source:     Path | None       = field(default=None)
    cleanup:    CleanupLevel      = field(default=CleanupLevel.NONE)
    no_metrics: bool              = field(default=False)


# ---------------------------------------------------------------------------
# JobPhase
# ---------------------------------------------------------------------------

class JobPhase(PhaseBase):
    """Phase object that initialises ``job.yaml`` and probes source metadata.

    This phase has no dependencies and is a declared dependency of every other
    phase.  It is the only phase that performs disk-space checking and pipeline
    intro logging. ``job.yaml`` is phase STATE, not an artifact: the result
    carries no artifacts and ``pending`` comes from the sidecar's currency
    (absent, self-heal-needed, or invalidated by a source mismatch).

    The phase is ``_DRY_RUN_READONLY``: a dry-run still probes the source and
    builds the full ``JobState`` read-only — only the ``job.yaml`` write is
    skipped — so the dry-run chain proceeds and the first real work phase
    reports ``PENDING``.

    Args:
        config:      Full validated application configuration.
        phases:      Phase registry (unused — ``JobPhase`` has no dependencies).
        source:      Resolved path to the source video file.
        work_dir:    Working directory for all pipeline artifacts.
        force:       When ``True``, wipe existing artifacts on source mismatch.
        cleanup:     Artifact retention policy applied after encoding.
        no_metrics:  When ``True``, skip writing ``metrics.yaml`` files.
        collector:   Metrics collector for timing instrumentation.
    """

    name:              str       = "job"
    DEPENDS_ON:        ClassVar[tuple[type[Phase], ...]] = ()
    BANNER:            bool      = False
    _METRIC_KEY:       MetricKey = MetricKey.JOB
    _DRY_RUN_READONLY: bool      = True

    def __init__(
        self,
        config:     AppConfig,
        phases:     dict[type[Phase], Phase] | None = None,
        *,
        source:      Path,
        work_dir:    Path,
        force:       bool,
        cleanup:     CleanupLevel,
        no_metrics:  bool,
        collector:   MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._source:      Path          = source
        self._work_dir:    Path          = work_dir
        self._force:       bool          = force
        self._cleanup:     CleanupLevel  = cleanup
        self._no_metrics:  bool          = no_metrics

        # Recovery stash — the loaded/current JobState and the force-wipe flag
        # resolved during _recover(); consumed by _execute()/_make_result().
        self._loaded_job:  JobState | None = None
        self._force_wipe: bool            = False

    # ------------------------------------------------------------------
    # PhaseBase hooks
    # ------------------------------------------------------------------

    def _recover(self) -> Recovery:
        """Determine ``job.yaml`` currency: absent, stale, self-heal, or current.

        Steps:

        1. Remove a leftover ``job.yaml.tmp`` from an interrupted write.
        2. Load ``job.yaml``; when absent the phase is pending (must create).
        3. Compare the persisted source metadata against the current source
           file (path, size, resolution). On mismatch: with ``--force`` set
           ``force_wipe`` and go pending (rebuild from the new source);
           without ``--force`` this is a fatal invalidation.
        4. When the persisted fast fields are incomplete (missing/invalid
           fps), the state needs a self-heal re-probe — pending.

        Returns:
            ``Recovery(artifacts=[], pending=...)`` — job.yaml is state, not
            an artifact; the loaded state is stashed on ``self._loaded_job``.

        Raises:
            RecoveryError: On a source mismatch without ``--force``.
        """
        job_yaml = self._work_dir / _JOB_YAML_FILENAME

        # Step 1 — .tmp pre-clean (job.yaml is written via .tmp-then-rename).
        tmp = job_yaml.with_name(job_yaml.name + TEMP_SUFFIX)
        if tmp.exists():
            try:
                tmp.unlink()
                logger.warning("Removed leftover temp file: %s", tmp.name)
            except OSError as exc:
                logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 2 — load; absent → must create.
        existing = JobState.load(job_yaml)
        if existing is None:
            return Recovery(pending=True)
        self._loaded_job = existing

        # Step 3 — source-mismatch invalidation.
        mismatches = self._find_source_mismatches(existing)
        if mismatches:
            mismatch_desc = "; ".join(
                f"{field}: persisted={old!r}, current={new!r}"
                for field, old, new in mismatches
            )
            if self._force:
                logger.warning(
                    "Source file mismatch detected (--force — downstream phases will wipe their own artifacts): %s",
                    mismatch_desc,
                )
                self._force_wipe = True
                return Recovery(pending=True)
            raise RecoveryError(
                "Source file mismatch detected — stopping execution.  "
                "Re-run with --force to wipe existing artifacts and continue with the new source.  "
                f"Mismatch: {mismatch_desc}"
            )

        # Step 4 — self-heal currency: missing/invalid fast fields.
        if existing.source._fps is None or existing.source._fps <= 0:
            return Recovery(pending=True)

        return Recovery(pending=False)

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> JobPhaseResult:
        """Probe the source and (unless dry-run) write ``job.yaml``.

        Two paths, both real work: the self-heal re-probe of a loaded state
        with incomplete fast fields, and the fresh probe after ``job.yaml``
        was absent or invalidated by ``--force``. The only write is the
        ``job.yaml`` save, skipped entirely when ``dry_run`` is ``True``.

        Args:
            wanted:  Always empty (job.yaml is state, not artifacts).
            dry_run: When ``True``, skip the write; the returned ``JobState``
                     is identical to what would have been persisted.

        Returns:
            ``JobPhaseResult`` with outcome ``COMPLETED`` (work ran).
        """
        job_yaml = self._work_dir / _JOB_YAML_FILENAME

        # Disk-space notification (execute mode only).
        if not dry_run:
            video = (
                self._loaded_job.source
                if self._loaded_job is not None
                else VideoMetadata(path=self._source)
            )
            n_strategies = len(self._config.encoding.resolved_strategies)
            log_disk_space_info(
                video          = video,
                work_dir       = self._work_dir,
                min_strategies = 1 if (self._config.encoding.optimize or n_strategies == 0) else n_strategies,
                max_strategies = max(1, n_strategies),
                chunking_mode  = self._config.chunking.mode,
            )

        if self._loaded_job is not None and not self._force_wipe:
            # Self-heal: re-probe the loaded state's missing fast fields.
            logger.warning("job.yaml has missing/invalid fps — re-probing source metadata")
            with self._collector.time(MetricKey.JOB, "probe"):
                self._probe_metadata(self._loaded_job.source)
            if not dry_run:
                self._loaded_job.save(job_yaml)
            return self._make_result(PhaseOutcome.COMPLETED, [], "job.yaml self-healed")

        # Fresh probe (job.yaml absent, or force_wipe after a source mismatch).
        source = VideoMetadata(path=self._source)
        logger.info("Probing source metadata: %s", source.path.name)
        with self._collector.time(MetricKey.JOB, "probe"):
            self._probe_metadata(source)
        job = JobState(source=source)
        self._loaded_job = job
        if not dry_run:
            job.save(job_yaml)
            logger.info("Initialized job.yaml for new pipeline run")
        return self._make_result(PhaseOutcome.COMPLETED, [], "job.yaml initialised")

    def _reused_result(self, wanted: list[Artifact], message: str) -> JobPhaseResult:
        """Build the reused result from the loaded, current ``job.yaml`` state."""
        return self._make_result(PhaseOutcome.REUSED, [], "job.yaml already up to date")

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact],
        message:   str,
        error:     str | None = None,
    ) -> JobPhaseResult:
        """Assemble a ``JobPhaseResult`` from constructor + recovery state.

        Args:
            outcome:   The phase outcome.
            artifacts: Always empty (job.yaml is state, not artifacts).
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result.
        """
        return JobPhaseResult(
            outcome     = outcome,
            artifacts   = artifacts,
            message     = message,
            error       = error,
            job         = self._loaded_job,
            force_wipe  = self._force_wipe,
            config      = self._config,
            work_dir    = self._work_dir,
            source      = self._source,
            cleanup     = self._cleanup,
            no_metrics  = self._no_metrics,
        )

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for the job phase.

        ``JobPhase`` owns only ``job.yaml`` — a recovery/parameter sidecar that
        must survive for reruns — so it has no deep artifacts to remove. This is
        a safe no-op regardless of ``ctx.deep_cleanup``.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        return

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

        if persisted.path.resolve() != self._source.resolve():
            mismatches.append(("path", str(persisted.path), str(self._source)))
            return mismatches

        try:
            current_size = self._source.stat().st_size
        except OSError:
            current_size = None

        if persisted._file_size_bytes is not None and current_size is not None:
            if persisted._file_size_bytes != current_size:
                mismatches.append(("file_size_bytes", persisted._file_size_bytes, current_size))

        if not mismatches and persisted._resolution is not None:
            live_meta = VideoMetadata(path=self._source)
            current_res = live_meta.resolution
            if current_res is not None and current_res != persisted._resolution:
                mismatches.append(("resolution", persisted._resolution, current_res))

        return mismatches

    def _probe_metadata(self, source: VideoMetadata) -> None:
        """Eagerly touch all fast-probe fields on ``source`` to populate them.

        Args:
            source: ``VideoMetadata`` instance to probe in-place.
        """
        _ = source.file_size_bytes
        _ = source.duration_seconds
        _ = source.fps
        _ = source.resolution
