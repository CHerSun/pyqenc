"""ProbePhase — resolves crop parameters and source frame count after extraction.

This phase sits between ``ExtractionPhase`` and ``ChunkingPhase`` in the pipeline
and owns the two slow video-only operations:

1. **Crop detection** — samples the extracted video file to find black borders.
2. **Frame count probing** — runs a null-encode on the source file to count frames.

Both operations are skipped for audio-only runs because ``ProbePhase`` is not
inserted into the audio registry.  When no video was extracted, ``ProbePhase``
returns ``FAILED`` which cascades to all downstream video phases.

Results are persisted in ``probe.yaml`` so subsequent runs skip re-probing.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from pyqenc.constants import TEMP_SUFFIX, THICK_LINE
from pyqenc.metrics import MetricKey
from pyqenc.models import (
    CropParams,
    ExtendedVideoMetadata,
    PhaseOutcome,
    VideoMetadata,
)
from pyqenc.phase import (
    Artifact,
    Phase,
    PhaseBase,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.state import ProbeState

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector
    from pyqenc.phases.job import JobPhaseResult

logger = logging.getLogger(__name__)

_PROBE_YAML_NAME = "probe.yaml"


# ---------------------------------------------------------------------------
# ProbePhaseResult
# ---------------------------------------------------------------------------

@dataclass
class ProbePhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying probe-specific payload.

    Attributes:
        source: Source video with guaranteed frame count; ``None`` when no
                video was extracted and ``ProbePhase`` returned ``FAILED``.
        crop:   Resolved crop parameters; all-zero when no cropping is needed
                or when ``ProbePhase`` returned ``FAILED``.
    """

    source: ExtendedVideoMetadata | None = field(default=None)
    crop:   CropParams                   = field(default_factory=CropParams)


# ---------------------------------------------------------------------------
# ProbePhase
# ---------------------------------------------------------------------------

class ProbePhase(PhaseBase):
    """Phase object that resolves crop parameters and source frame count.

    Depends on ``JobPhase`` and ``ExtractionPhase``.  Returns ``FAILED`` when
    no video was extracted, which cascades to all downstream video phases via
    their ``_ensure_dependencies()`` mechanism.

    Results are written to ``probe.yaml`` after a successful run so subsequent
    runs can skip re-probing. ``probe.yaml`` is phase STATE, not an artifact:
    the result carries no artifacts and ``pending`` comes from the sidecar's
    currency (absent, or invalidated by a manual ``--crop`` override). The
    phase emits no banner — it logs a concise INFO line when the slow probe
    starts and a result line with the probed (or cached) details.

    Args:
        config:      Full validated application configuration.
        phases:      Phase registry; used to resolve typed dependency references.
        collector:   Metrics collector; recovery and probe work are timed under
                     ``probe`` / ``probe.crop_detect`` / ``probe.frame_count``.
        crop_params: Optional manual ``--crop`` override forwarded from the CLI.
                     When ``not None`` it invalidates the cached sidecar (cheap
                     rewrite reusing the cached frame count).
    """

    name:        str       = "probe"
    BANNER:      bool      = False
    _METRIC_KEY: MetricKey = MetricKey.PROBE

    def __init__(
        self,
        config:      AppConfig,
        phases:      dict[type[Phase], Phase] | None = None,
        *,
        collector:   MetricsCollector,
        crop_params: CropParams | None = None,
    ) -> None:
        from pyqenc.phases.extraction import ExtractionPhase as _ExtractionPhase
        from pyqenc.phases.job import JobPhase as _JobPhase

        super().__init__(config, phases, collector=collector)

        self._crop_params: CropParams | None       = crop_params
        self._job:         _JobPhase | None        = cast("_JobPhase",        phases.get(_JobPhase))        if phases else None
        self._extraction:  _ExtractionPhase | None = cast("_ExtractionPhase", phases.get(_ExtractionPhase)) if phases else None
        self.dependencies: list[Phase]             = [
            dep for dep in (self._job, self._extraction) if dep is not None
        ]

        # Recovery stash — the loaded probe.yaml state and the resolved payload
        # (source / crop) for result construction.
        self._probe_state:     ProbeState | None           = None
        self._resolved_source: ExtendedVideoMetadata | None = None
        self._resolved_crop:   CropParams                  = CropParams()

    # ------------------------------------------------------------------
    # PhaseBase hooks
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, *, dry_run: bool) -> ProbePhaseResult | None:
        """Presence-check the typed references, then defer to the shared walk.

        Args:
            dry_run: Propagated unchanged to each dependency's ``run()``.

        Returns:
            A ``FAILED`` result when a required phase is missing or a
            dependency failed, a ``PENDING`` result if any dependency is
            legitimately pending (dry-run only), or ``None`` when the phase
            may proceed.
        """
        missing = [
            label for label, dep in (("JobPhase", self._job), ("ExtractionPhase", self._extraction))
            if dep is None
        ]
        if missing:
            err = f"ProbePhase requires {', '.join(missing)} dependencies"
            logger.error(err)
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)
        return super()._ensure_dependencies(dry_run=dry_run)

    def _recover(self) -> Recovery:
        """Determine ``probe.yaml`` currency: absent, invalidated, or current.

        Steps:

        1. Fail fast when no video was extracted — a fatal invalidation for
           every downstream video phase.
        2. Remove a leftover ``probe.yaml.tmp`` from an interrupted write.
        3. Load ``probe.yaml``. Pending when absent (full probe needed) or
           when a manual ``--crop`` override invalidates the cached crop
           (cheap rewrite: the frame count stays cached). Current otherwise —
           the sidecar is written atomically, so presence implies complete
           data (no content peeking).

        Returns:
            ``Recovery(artifacts=[], pending=...)`` — probe.yaml is state,
            not an artifact; the loaded state is stashed on ``self._probe_state``.

        Raises:
            RecoveryError: When extraction produced no video track.
        """
        job_result        = self._job.result        # type: ignore[union-attr]
        extraction_result = self._extraction.result # type: ignore[union-attr]
        probe_yaml        = job_result.work_dir / _PROBE_YAML_NAME  # type: ignore[operator]

        # Step 1 — no video extracted: fatal for all downstream video phases.
        if extraction_result.video is None:  # type: ignore[union-attr]
            raise RecoveryError(
                "No video tracks extracted — video processing cannot continue"
            )

        # Step 2 — .tmp pre-clean (probe.yaml is written via .tmp-then-rename).
        tmp = probe_yaml.with_name(probe_yaml.name + TEMP_SUFFIX)
        if tmp.exists():
            try:
                tmp.unlink()
                logger.warning("Removed leftover temp file: %s", tmp.name)
            except OSError as exc:
                logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 3 — load + currency.
        self._probe_state = ProbeState.load(probe_yaml)
        if self._probe_state is None:
            return Recovery(pending=True)
        if self._crop_params is not None:
            # Manual --crop invalidates the cached crop: rewrite (cheap — the
            # frame count stays cached).
            return Recovery(pending=True)
        return Recovery(pending=False)

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> ProbePhaseResult:
        """Resolve crop + frame count and persist ``probe.yaml``.

        The slow operations are individually timed under the dotted keys
        ``probe.crop_detect`` and ``probe.frame_count`` (the top-level
        ``probe`` span belongs to the template). Cache hits skip the slow
        operation entirely. ``dry_run`` is never ``True`` here (probe is not a
        readonly-execute phase; the template previews instead).

        Args:
            wanted:  Always empty (probe.yaml is state, not artifacts).
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``ProbePhaseResult`` with outcome ``COMPLETED``.
        """
        from pyqenc.utils.crop import detect_crop_parameters

        job_result        = self._job.result         # type: ignore[union-attr]
        extraction_result = self._extraction.result  # type: ignore[union-attr]
        probe_yaml        = job_result.work_dir / _PROBE_YAML_NAME  # type: ignore[operator]
        probe_state       = self._probe_state
        extracted_vm      = extraction_result.video   # type: ignore[union-attr]

        needs_crop_detect = (
            self._crop_params is None
            and (probe_state is None or probe_state.crop is None)
        )
        needs_frame_probe = probe_state is None or probe_state.frame_count <= 0
        if needs_crop_detect or needs_frame_probe:
            logger.info(
                "Probe: starting long probe%s%s — may take a while",
                " + crop detect" if needs_crop_detect else "",
                " + frame count" if needs_frame_probe else "",
            )

        # Resolve crop: manual → cached → auto-detect (timed when slow).
        if self._crop_params is not None:
            crop = self._crop_params
            logger.info("Crop: %s (manual)", crop.display())
        elif probe_state is not None and probe_state.crop is not None:
            crop = probe_state.crop
            logger.info("Crop: %s (cached)", crop.display())
        else:
            logger.info("Detecting crop: %s", extracted_vm.path.name)
            with self._collector.time(MetricKey.PROBE, "crop_detect"):
                crop = detect_crop_parameters(extracted_vm)

        # Resolve frame count: cached → slow null-encode probe (timed).
        source_vm = self._get_source_vm(job_result)
        if probe_state is not None and probe_state.frame_count > 0:
            frame_count        = probe_state.frame_count
            extended_vm        = ExtendedVideoMetadata.from_base(
                source_vm, frame_count=frame_count
            )
            logger.debug("Frame count: %d (cached)", frame_count)
        else:
            with self._collector.time(MetricKey.PROBE, "frame_count"):
                extended_vm = source_vm.probe_extended()
            frame_count = extended_vm.frame_count

        # Persist and stash the payload.
        ProbeState(frame_count=frame_count, crop=crop).save(probe_yaml)
        self._resolved_source = extended_vm
        self._resolved_crop   = crop

        logger.info(
            "Probe: done — frame_count=%d, crop=%s",
            frame_count, crop.display(),
        )
        return self._make_result(PhaseOutcome.COMPLETED, [], "probe resolved")

    def _reused_result(self, wanted: list[Artifact], message: str) -> ProbePhaseResult:
        """Build the reused result from the cached ``probe.yaml`` state."""
        state = self._probe_state
        assert state is not None  # current currency implies a loaded state
        source_vm   = self._get_source_vm(self._job.result)  # type: ignore[union-attr]
        extended_vm = ExtendedVideoMetadata.from_base(
            source_vm, frame_count=state.frame_count
        )
        self._resolved_source = extended_vm
        self._resolved_crop   = state.crop if state.crop is not None else CropParams()
        logger.info("Probe: all values cached — reusing probe.yaml")
        logger.info(THICK_LINE)
        return self._make_result(PhaseOutcome.REUSED, [], "probe.yaml reused")

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact],
        message:   str,
        error:     str | None = None,
    ) -> ProbePhaseResult:
        """Assemble a ``ProbePhaseResult`` from the resolved payload stash.

        Args:
            outcome:   The phase outcome.
            artifacts: Always empty (probe.yaml is state, not artifacts).
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result (``source``/``crop`` default to ``None`` /
            all-zero on non-complete paths).
        """
        return ProbePhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = message,
            error     = error,
            source    = self._resolved_source,
            crop      = self._resolved_crop,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_source_vm(self, job_result: JobPhaseResult) -> VideoMetadata:

        """Return the source ``VideoMetadata`` from job state, or a bare instance.

        Args:
            job_result: Completed ``JobPhaseResult``.

        Returns:
            Source ``VideoMetadata`` with any cached fast-probe fields.
        """
        if job_result.job is not None:
            return job_result.job.source
        # Fallback: construct bare instance from the source path
        return VideoMetadata(path=job_result.source)  # type: ignore[arg-type]
