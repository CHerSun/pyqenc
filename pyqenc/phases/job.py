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
  file (path + size).
- Propagate ``force_wipe=True`` to downstream phases when ``--force`` is
  provided and a source mismatch is detected.
"""
# CHerSun 2026

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pyqenc.constants import TEMP_SUFFIX
from pyqenc.metrics import MetricKey, MetricsCollector
from pyqenc.models import (
    CleanupLevel,
    EncodingPlan,
    PhaseOutcome,
)
from pyqenc.phase import (
    Artifact,
    ArtifactState,
    FinalizeContext,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.stream_model import File, JobSidecar
from pyqenc.utils.fs import remove_stale_tmp_file, safe_stat_size
from pyqenc.utils.yaml_utils import load_model, write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JobPhaseResult — extends PhaseResult with job-specific payload
# ---------------------------------------------------------------------------

@dataclass
class JobPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying job-level data for downstream phases.

    Attributes:
        file:       The source-file artifact — the run's single
                    :class:`~pyqenc.stream_model.File` (path + size) wrapped
                    with its recovery facts; ``COMPLETE`` once established and
                    persisted.
        force_wipe: ``True`` when ``--force`` was provided and a source mismatch
                    was detected; downstream phases must delete their own output
                    directories and phase parameter YAMLs before proceeding.
        config:     Full validated application configuration.
        plan:       The run's resolved encoding plan — strategies and quality
                    targets as consumed by optimization, encoding, merge, and
                    extraction's strategy count. Derived once at the run
                    boundary (``AppConfig.resolve_encoding``) and threaded
                    here like every other volatile per-run parameter.
                    ``None`` only in the audio-only registry (no video phase
                    runs there — the audio pass must not depend on the
                    video config being resolvable).
        work_dir:   Working directory for all pipeline artifacts.
        source:     Resolved path to the source video file.
        cleanup:    Artifact retention policy applied after encoding.
        no_metrics: When ``True``, skip writing ``metrics.yaml`` files.
    """

    # Always-present payload (every phase-built result populates all of it —
    # required, never Optional) comes before the defaulted conveniences.
    # plan is the one nullable member: None only in the audio-only registry.
    file:       Artifact[File]
    config:     AppConfig
    plan:       EncodingPlan | None
    work_dir:   Path
    source:     Path
    force_wipe: bool                   = field(default=False)
    cleanup:    CleanupLevel           = field(default=CleanupLevel.NONE)
    no_metrics: bool                   = field(default=False)


# ---------------------------------------------------------------------------
# JobPhase
# ---------------------------------------------------------------------------

class JobPhase(Phase[JobPhaseResult]):
    """Phase that establishes the source identity and owns ``job.yaml``.

    This phase has no dependencies and is a declared dependency of every other
    phase. Its ledger is one ``Artifact[File]`` row — ``COMPLETE`` once the
    source identity is verified and persisted; ``job.yaml`` remains phase STATE
    (the sidecar), distinct from the artifact.

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
    SIDECAR_NAME = "job.yaml"
    DEPENDS_ON:        ClassVar[tuple[type[Phase], ...]] = ()
    BANNER:            bool      = False
    _METRIC_KEY:       MetricKey = MetricKey.JOB
    _DRY_RUN_READONLY: bool      = True

    def __init__(
        self,
        config:     AppConfig,
        phases:     PhaseRegistry,
        *,
        source:      Path,
        work_dir:    Path,
        force:       bool,
        cleanup:     CleanupLevel,
        no_metrics:  bool,
        collector:   MetricsCollector,
        plan:        EncodingPlan | None,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._source:      Path         = source
        self._work_dir:    Path             = work_dir
        self._force:       bool             = force
        self._cleanup:     CleanupLevel     = cleanup
        self._no_metrics:  bool             = no_metrics
        self._plan:        EncodingPlan | None = plan

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
           create). A ``job.yaml`` written by an older version does not parse
           as this schema — it is treated as absent and rebuilt (pre-alpha
           policy: no mid-work upgrades).
        3. Compare the persisted source identity (path + ``file_size_bytes``)
           against live values. On mismatch: with ``--force`` set
           ``force_wipe`` and go pending (rebuild for the new source);
           without ``--force`` this is a fatal invalidation.

        The ledger carries one ``Artifact[File]`` row — ``COMPLETE`` by
        construction once the source is verified (a missing source fails the
        phase before recovery); the pending paths leave it ``ABSENT`` with
        the freshly probed identity as the payload.

        Returns:
            The :class:`Recovery` single source of truth (one row).

        Raises:
            RecoveryError: On a source mismatch without ``--force``.
        """
        job_yaml = self._work_dir / JobPhase.SIDECAR_NAME

        # Step 1 — .tmp pre-clean (job.yaml is written via .tmp-then-rename).
        remove_stale_tmp_file(job_yaml.with_name(job_yaml.name + TEMP_SUFFIX))

        # Step 2 — load the File dump; absent/unparseable → must create.
        existing = self._load_job_sidecar(job_yaml)
        if existing is None:
            self._file = self._probe_file()
            return Recovery.from_artifacts([
                Artifact(payload=self._file, state=ArtifactState.ABSENT),
            ])
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
                self._file = self._probe_file()
                return Recovery.from_artifacts([
                    Artifact(payload=self._file, state=ArtifactState.ABSENT),
                ])
            raise RecoveryError(
                "Source file mismatch detected — stopping execution.  "
                "Re-run with --force to wipe existing artifacts and continue with the new source.  "
                f"Mismatch: {mismatch_desc}"
            )

        return Recovery.from_artifacts([
            Artifact(payload=self._file, state=ArtifactState.COMPLETE),
        ])

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
        job_yaml = self._work_dir / JobPhase.SIDECAR_NAME

        # Fresh identity (job.yaml absent, or force_wipe after a source
        # mismatch) — recovery already probed it eagerly for the ledger row.
        assert self._file is not None, "file guaranteed by the _recover pending branches"
        if not dry_run:
            write_yaml_atomic(
                job_yaml,
                JobSidecar(source=self._file).model_dump(exclude_none=True),
            )
            logger.info("Initialized job.yaml for new pipeline run")
        return self._make_result(
            PhaseOutcome.COMPLETED, [],
            "job.yaml initialised",
            file_state=ArtifactState.COMPLETE,
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> JobPhaseResult:
        """Build the reused result; rebuild the interim in-memory state.

        Args:
            wanted:  Always empty (job.yaml is state, not artifacts).
            message: Unused — the reused message is fixed.

        Returns:
            ``JobPhaseResult`` with outcome ``REUSED``.
        """
        assert self._file is not None, "file guaranteed by the _recover reuse path"
        return self._make_result(
            PhaseOutcome.REUSED, [], "job.yaml already up to date",
            file_state=ArtifactState.COMPLETE,
        )

    def _make_result(
        self,
        outcome:    PhaseOutcome,
        artifacts:  list[Artifact],
        message:    str,
        file_state: ArtifactState = ArtifactState.ABSENT,
    ) -> JobPhaseResult:
        """Assemble a ``JobPhaseResult`` from constructor + recovery state.

        Args:
            outcome:    The phase outcome.
            artifacts:  The wanted row list (transitional population).
            message:    Human-readable summary — on ``FAILED``, the error
                        description.
            file_state: The File row's state for this result (``COMPLETE``
                        once the identity is established on this path).

        Returns:
            The populated result.
        """
        assert self._file is not None, "file set on every phase-built result"
        return JobPhaseResult(
            outcome     = outcome,
            message     = message,
            file        = Artifact(payload=self._file, state=file_state),
            force_wipe  = self._force_wipe,
            config      = self._config,
            plan        = self._plan,
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
        return load_model(path, JobSidecar)

    def _probe_file(self) -> File:
        """Construct the run's single :class:`File`, eagerly from the filesystem.

        Returns:
            The source ``File`` (path + size; size is ``None`` when the stat
            fails).
        """
        file_size_bytes = safe_stat_size(self._source)
        if file_size_bytes is None:
            logger.warning("Could not stat source file: %s", self._source)
        return File(path=self._source, file_size_bytes=file_size_bytes)

    def _find_source_mismatches(
        self,
        persisted: File,
    ) -> list[tuple[str, object, object]]:
        """Compare the persisted source identity against the live file.

        Checks the persisted path and ``file_size_bytes`` only.

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

