"""JobPhase — establishes the source ``File`` identity and owns ``job.yaml``.

This is the first phase in the pipeline and has no dependencies.  Every other
phase declares ``JobPhase`` as a dependency so that job-level data (the source
:class:`~pyqenc.stream_model.File`, force-wipe flag, run parameters) is always
available before any phase does real work.

Responsibilities:
- Construct exactly one :class:`~pyqenc.stream_model.File` per run — eagerly
  from the filesystem — and expose it on ``JobPhaseResult``.
- Persist ``job.yaml`` as the :class:`File` dump only
  (``{source: {path, file_size_bytes?}}``) and validate it against the live
  file (path + size); the resolution re-probe comparison is gone.
- Propagate ``force_wipe=True`` to downstream phases when ``--force`` is
  provided and a source mismatch is detected.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import yaml

from pyqenc.constants import TEMP_SUFFIX
from pyqenc.metrics import MetricKey, MetricsCollector
from pyqenc.models import (
    CleanupLevel,
    PhaseOutcome,
)
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.stream_model import File, JobSidecar
from pyqenc.utils.long_path import LongPath
from pyqenc.utils.yaml_utils import write_yaml_atomic

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
        file:       The run's single :class:`~pyqenc.stream_model.File` — the
                    source identity (path + size) established eagerly by
                    JobPhase.
        job:        Interim in-memory fast-metadata state (until downstream
                    phases migrate to the stream model).
        force_wipe: ``True`` when ``--force`` was provided and a source mismatch
                    was detected; downstream phases must delete their own output
                    directories and phase parameter YAMLs before proceeding.
        config:     Full validated application configuration.
        work_dir:   Working directory for all pipeline artifacts.
        source:     Resolved path to the source video file.
        cleanup:     Artifact retention policy applied after encoding.
        no_metrics: When ``True``, skip writing ``metrics.yaml`` files.
    """

    file:       File | None       = field(default=None)
    force_wipe: bool              = field(default=False)
    config:     AppConfig | None = field(default=None)
    work_dir:   Path | None       = field(default=None)
    source:     Path | None       = field(default=None)
    cleanup:    CleanupLevel      = field(default=CleanupLevel.NONE)
    no_metrics: bool              = field(default=False)


# ---------------------------------------------------------------------------
# JobPhase
# ---------------------------------------------------------------------------

class JobPhase(Phase):
    """Phase that establishes the source identity and owns ``job.yaml``.

    This phase has no dependencies and is a declared dependency of every other
    phase. ``job.yaml`` is phase STATE, not an artifact: the result carries no
    artifacts and ``pending`` comes from the sidecar's currency (absent, or
    invalidated by a source mismatch).

    The phase is ``_DRY_RUN_READONLY``: a dry-run still establishes the
    :class:`~pyqenc.stream_model.File` and builds the interim in-memory state
    read-only — only the ``job.yaml`` write is skipped — so the dry-run chain
    proceeds and the first real work phase reports ``PENDING``.

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
        phases:     PhaseRegistry | None = None,
        *,
        source:      LongPath,
        work_dir:    Path,
        force:       bool,
        cleanup:     CleanupLevel,
        no_metrics:  bool,
        collector:   MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._source:      LongPath         = source
        self._work_dir:    Path             = work_dir
        self._force:       bool             = force
        self._cleanup:     CleanupLevel     = cleanup
        self._no_metrics:  bool             = no_metrics

        # Recovery stash — the run's File, resolved during _recover();
        # consumed by _execute()/_make_result().
        self._file:        File | None      = None
        self._force_wipe:  bool             = False

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _recover(self) -> Recovery:
        """Determine ``job.yaml`` currency: absent, stale, or current.

        Steps:

        1. Remove a leftover ``job.yaml.tmp`` from an interrupted write.
        2. Load the :class:`~pyqenc.stream_model.JobSidecar` (the ``File``
           dump); when absent or unparseable the phase is pending (must
           create). A pre-stream-model ``job.yaml`` does not parse as the
           shrunk schema — it is treated as absent and rebuilt (pre-alpha
           policy: no mid-work upgrades).
        3. Compare the persisted source identity (path + ``file_size_bytes``)
           against live values. On mismatch: with ``--force`` set
           ``force_wipe`` and go pending (rebuild for the new source);
           without ``--force`` this is a fatal invalidation.

        Returns:
            ``Recovery(artifacts=[], pending=...)`` — job.yaml is state, not
            an artifact; the loaded File is stashed on ``self._file``.

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

        # Step 2 — load the File dump; absent/unparseable → must create.
        existing = self._load_job_sidecar(job_yaml)
        if existing is None:
            return Recovery(pending=True)
        self._file = existing.source

        # Step 3 — source-mismatch invalidation (path + size vs live values).
        mismatches = self._find_source_mismatches(existing.source)
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
                self._file = None
                return Recovery(pending=True)
            raise RecoveryError(
                "Source file mismatch detected — stopping execution.  "
                "Re-run with --force to wipe existing artifacts and continue with the new source.  "
                f"Mismatch: {mismatch_desc}"
            )

        return Recovery(pending=False)

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> JobPhaseResult:
        """Establish the source identity and (unless dry-run) write ``job.yaml``.

        Runs when the sidecar was absent or was invalidated by ``--force``:
        constructs the eager :class:`~pyqenc.stream_model.File`, builds the
        interim in-memory fast-metadata state, and persists the shrunk
        ``job.yaml`` (the File dump only).

        Args:
            wanted:  Always empty (job.yaml is state, not artifacts).
            dry_run: When ``True``, skip the write; the returned data is
                     identical to what would be persisted.

        Returns:
            ``JobPhaseResult`` with outcome ``COMPLETED`` (work ran).
        """
        job_yaml = self._work_dir / _JOB_YAML_FILENAME

        # Fresh identity (job.yaml absent, or force_wipe after a source mismatch).
        self._file = self._probe_file()
        if not dry_run:
            write_yaml_atomic(
                job_yaml,
                JobSidecar(source=self._file).model_dump(exclude_none=True),
            )
            logger.info("Initialized job.yaml for new pipeline run")
        return self._make_result(PhaseOutcome.COMPLETED, [], "job.yaml initialised")

    def _reused_result(self, wanted: list[Artifact], message: str) -> JobPhaseResult:
        """Build the reused result; rebuild the interim in-memory state.

        Args:
            wanted:  Always empty (job.yaml is state, not artifacts).
            message: Unused — the reused message is fixed.

        Returns:
            ``JobPhaseResult`` with outcome ``REUSED``.
        """
        assert self._file is not None, "file guaranteed by the _recover reuse path"
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
            artifacts: Always empty (job.yaml is state, not an artifact).
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
            file        = self._file,
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _load_job_sidecar(path: Path) -> JobSidecar | None:
        """Load ``job.yaml`` as the :class:`JobSidecar` (the ``File`` dump).

        Args:
            path: The ``job.yaml`` path.

        Returns:
            The loaded sidecar, or ``None`` when absent or unparseable.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return JobSidecar.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — any parse/validation failure means "rebuild"
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def _probe_file(self) -> File:
        """Construct the run's single :class:`File`, eagerly from the filesystem.

        Returns:
            The source ``File`` (path + size; size is ``None`` when the stat
            fails).
        """
        try:
            file_size_bytes: int | None = self._source.stat().st_size
        except OSError as exc:
            file_size_bytes = None
            logger.warning("Could not stat source file %s: %s", self._source, exc)
        return File(path=self._source, file_size_bytes=file_size_bytes)

    def _find_source_mismatches(
        self,
        persisted: File,
    ) -> list[tuple[str, object, object]]:
        """Compare the persisted source identity against the live file.

        Checks the persisted path and ``file_size_bytes`` only (Req 1.5) —
        the resolution re-probe comparison is gone with the fast-video slice.

        Args:
            persisted: The sidecar's recorded :class:`File`.

        Returns:
            List of ``(field_name, persisted_value, current_value)`` tuples.
        """
        mismatches: list[tuple[str, object, object]] = []

        if persisted.path.resolve() != self._source.resolve():
            mismatches.append(("path", str(persisted.path), str(self._source)))
            return mismatches

        try:
            current_size = self._source.stat().st_size
        except OSError:
            current_size = None

        if (
            persisted.file_size_bytes is not None
            and current_size is not None
            and persisted.file_size_bytes != current_size
        ):
            mismatches.append(("file_size_bytes", persisted.file_size_bytes, current_size))

        return mismatches

