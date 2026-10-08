"""JobPhase — establishes the source ``File`` identity and owns ``job.yaml``.

This is the first phase in the pipeline and has no dependencies.  Every other
phase declares ``JobPhase`` as a dependency so that job-level data (the source
:class:`~pyqenc.stream_model.File`, the ``--force`` permission, run
parameters) is always available before any phase does real work.

Responsibilities:
- Construct exactly one :class:`~pyqenc.stream_model.File` per run — eagerly
  from the filesystem, including the sampled-content fingerprint — and expose
  it on ``JobPhaseResult``.
- Persist ``job.yaml`` as the one human-facing header — the source locator
  beside its content identity (``{source: {path, fingerprint}}``) — and
  validate it against the live file.
- Carry the raw ``--force`` permission on the result: it grants destructive
  invalidation effects for the run and has no other effect (Req 35a); each
  phase detects its own conditions from its own persisted keys.
"""
# CHerSun 2026

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from pyqenc.constants import TEMP_SUFFIX
from pyqenc.metrics import MetricKey, MetricsCollector
from pyqenc.models import (
    CleanupLevel,
    Fingerprint,
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
from pyqenc.stream_model import File, LongPathYaml
from pyqenc.utils.fs import remove_stale_tmp_file, safe_stat_size
from pyqenc.utils.yaml_utils import load_model, write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig

# ---------------------------------------------------------------------------
# job.yaml — the human-facing identity record (re-homed sidecar, Req 21)
# ---------------------------------------------------------------------------

class JobSourceRecord(BaseModel):
    """The ``job.yaml`` identity record: the locator beside the content.

    Path is the RUNTIME LOCATOR (a move rewrites ``job.yaml`` and nothing
    else — no fatal, no invalidation, Req 34); the fingerprint is the
    identity every phase key compares against (Req 31). The file size lives
    inside the fingerprint as its belt — it is not duplicated as a field.
    """

    model_config = ConfigDict(frozen=True)

    path:       LongPathYaml
    fingerprint: Fingerprint


class JobSidecar(BaseModel):
    """The ``job.yaml`` slice — the one human-facing sidecar.

    ``source`` names what this workdir works with (the locator + content
    identity); it is also the standalone ``measure`` command's source
    discovery (the sanctioned cross-sidecar read).
    """

    source: JobSourceRecord


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JobPhaseResult — extends PhaseResult with job-specific payload
# ---------------------------------------------------------------------------

@dataclass
class JobPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying job-level data for downstream phases.

    Attributes:
        file:       The source-file artifact — the run's single
                    :class:`~pyqenc.stream_model.File` (path + size +
                    sampled fingerprint) wrapped with its recovery facts;
                    ``COMPLETE`` once established and persisted.
        config:     Full validated application configuration.
        work_dir:   Working directory for all pipeline artifacts.
        source:     Resolved path to the source video file.
        force:      The raw ``--force`` CLI flag — PERMISSION for
                    destructive invalidation effects this run and nothing
                    else (Req 35a): it never causes a wipe by itself; each
                    phase's own fatal-band condition consumes it.
        cleanup:    Artifact retention policy applied after encoding.
        no_metrics: When ``True``, skip writing ``metrics.yaml`` files.
    """

    # Always-present payload (every phase-built result populates all of it —
    # required, never Optional) comes before the defaulted conveniences.
    file:       Artifact[File]
    config:     AppConfig
    work_dir:   Path
    source:     Path
    force:      bool                  = field(default=False)
    cleanup:    CleanupLevel          = field(default=CleanupLevel.NONE)
    no_metrics: bool                  = field(default=False)

    @property
    def source_fingerprint(self) -> Fingerprint:
        """The live source content identity — computed once per run at Job.

        The single accessor every phase's identity-key comparison reads; the
        contract assert replaces per-consumer Optional handling (the source
        ``File`` always carries its fingerprint).
        """
        fingerprint = self.file.payload.fingerprint
        assert fingerprint is not None, "the source File carries its fingerprint (Req 30)"
        return fingerprint


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
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._source:      Path         = source
        self._work_dir:    Path             = work_dir
        self._force:       bool             = force
        self._cleanup:     CleanupLevel     = cleanup
        self._no_metrics:  bool             = no_metrics

        # Recovery stash — the run's File, resolved during _recover();
        # consumed by _execute()/_make_result().
        self._file:   File | None = None
        self._stale:  bool       = False
        """Whether the persisted job.yaml record is stale (identity wipe or
        locator move) and must be rewritten by ``_execute``."""

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _invalidate(self) -> None:
        """Key-triggered effects over the persisted ``job.yaml`` record.

        Disk effects only (Req 54): the .tmp pre-clean; the identity
        comparison (fatal without ``--force``); and the two rewrite-own-
        sidecar effects — the permission-granted identity rewrite (Job
        rewrites its own record; each downstream phase's identity key
        re-detects the change and wipes its own artifacts — no propagated
        wipe order exists) and the locator update (a path-only difference
        with matching content rewrites the record with no fatal and no
        invalidation, Req 34).

        Raises:
            RecoveryError: On a content-identity mismatch without ``--force``.
        """
        job_yaml = self._work_dir / JobPhase.SIDECAR_NAME

        # .tmp pre-clean (job.yaml is written via .tmp-then-rename).
        remove_stale_tmp_file(job_yaml.with_name(job_yaml.name + TEMP_SUFFIX))

        # The live identity, probed eagerly every run (the identity owner;
        # downstream phases compare against this, never re-hash).
        self._file = self._probe_file()
        live_fingerprint = self._file.fingerprint
        assert live_fingerprint is not None, "the probed File carries its fingerprint"

        existing = self._load_job_sidecar(job_yaml)
        if existing is None:
            return

        if not existing.source.fingerprint.matches(live_fingerprint):
            if not self._force:
                raise RecoveryError(
                    "Source content identity mismatch — this workdir belongs to a "
                    "different source file.  Re-run with --force to grant permission "
                    "for each phase to wipe its own artifacts and re-derive from the "
                    "new source."
                )
            logger.warning(
                "Source content identity mismatch (--force granted — each phase "
                "wipes its own artifacts via its own identity key)"
            )
            self._rewrite_record(job_yaml)
            return

        if existing.source.path.resolve() != self._source.resolve():
            # Locator update (Req 34): the content is unchanged — only the
            # runtime location moved. Rewrite the record; nothing else fires.
            logger.info(
                "Source moved (%s → %s) — updating the recorded locator "
                "(content identity unchanged)",
                existing.source.path, self._source,
            )
            self._rewrite_record(job_yaml)

    def _rewrite_record(self, job_yaml: Path) -> None:
        """Rewrite ``job.yaml`` with the live identity (a disk effect)."""
        assert self._file is not None, "the invalidation probe stashes the File"
        fingerprint = self._file.fingerprint
        assert fingerprint is not None, "the probed File carries its fingerprint"
        write_yaml_atomic(
            job_yaml,
            JobSidecar(source=JobSourceRecord(
                path       = self._source,
                fingerprint = fingerprint,
            )).model_dump(exclude_none=True),
        )

    def _recover(self) -> Recovery:
        """Classify the ``job.yaml`` record from disk truth (one row).

        Runs after :meth:`_invalidate` settled the record on disk — the
        classification re-reads it: a record matching the live identity and
        locator is COMPLETE; anything else (absent, or a rewrite the
        invalidation could not apply — e.g. a dry-run skips writes) leaves
        the row ABSENT with the freshly probed identity as the payload.

        Returns:
            The :class:`Recovery` single source of truth (one row).
        """
        job_yaml = self._work_dir / JobPhase.SIDECAR_NAME

        # The live identity (probed by _invalidate; re-derive defensively —
        # classification re-reads truth, it never trusts in-memory handoff).
        if self._file is None:
            self._file = self._probe_file()
        live_fingerprint = self._file.fingerprint
        assert live_fingerprint is not None, "the probed File carries its fingerprint"

        existing = self._load_job_sidecar(job_yaml)
        current = (
            existing is not None
            and existing.source.fingerprint.matches(live_fingerprint)
            and existing.source.path.resolve() == self._source.resolve()
        )
        self._stale = not current
        return Recovery.from_artifacts([
            Artifact(
                payload = self._file,
                state   = ArtifactState.COMPLETE if current else ArtifactState.ABSENT,
            ),
        ])

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> JobPhaseResult:
        """Establish the source identity and (unless dry-run) write ``job.yaml``.

        Runs when the record was absent, stale by identity (permission
        granted), or moved (locator update): persists the current
        :class:`~pyqenc.stream_model.JobSidecar`.

        Args:
            wanted:  Always empty (job.yaml is state, not artifacts).
            dry_run: When ``True``, skip the write; the returned data is
                     identical to what would be persisted.

        Returns:
            ``JobPhaseResult`` with outcome ``COMPLETED`` (work ran).
        """
        job_yaml = self._work_dir / JobPhase.SIDECAR_NAME

        # The invalidation (or its dry-run skip) left the classification
        # truth on disk; recovery stashed the probed File.
        assert self._file is not None, "file guaranteed by the _recover pending branches"
        fingerprint = self._file.fingerprint
        assert fingerprint is not None, "the probed File carries its fingerprint"
        if not dry_run:
            write_yaml_atomic(
                job_yaml,
                JobSidecar(source=JobSourceRecord(
                    path       = self._source,
                    fingerprint = fingerprint,
                )).model_dump(exclude_none=True),
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
            force       = self._force,
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
        """Load ``job.yaml`` as the :class:`JobSidecar`.

        Args:
            path: The ``job.yaml`` path.

        Returns:
            The loaded sidecar, or ``None`` when absent or unparseable.
        """
        return load_model(path, JobSidecar)

    def _probe_file(self) -> File:
        """Construct the run's single :class:`File`, eagerly from the filesystem.

        The source's sampled-content fingerprint is derived here (Req 30) —
        computed once per run and carried by the live ``File``, so downstream
        phases compare their persisted identity key against it without ever
        re-hashing the source.

        Returns:
            The source ``File`` (path + size + fingerprint; size is ``None``
            when the stat fails).

        Raises:
            OSError: When the source cannot be read for the fingerprint —
                     a fatal at this computing site, never a carried ``None``
                     (Req 10).
        """
        file_size_bytes = safe_stat_size(self._source)
        if file_size_bytes is None:
            logger.warning("Could not stat source file: %s", self._source)
        return File(
            path            = self._source,
            file_size_bytes = file_size_bytes,
            fingerprint     = File.sampled_fingerprint(self._source),
        )

