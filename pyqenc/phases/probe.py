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

from pyqenc.constants import THICK_LINE
from pyqenc.models import (
    CropParams,
    ExtendedVideoMetadata,
    PhaseOutcome,
    VideoMetadata,
)
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.state import ArtifactState, ProbeState

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

class ProbePhase:
    """Phase object that resolves crop parameters and source frame count.

    Depends on ``JobPhase`` and ``ExtractionPhase``.  Returns ``FAILED`` when
    no video was extracted, which cascades to all downstream video phases via
    their ``_ensure_dependencies()`` mechanism.

    Results are written to ``probe.yaml`` after a successful run so subsequent
    runs can skip re-probing.

    Args:
        config:      Full validated application configuration.
        phases:      Phase registry; used to resolve typed dependency references.
        collector:   Metrics collector (accepted for API uniformity, not used here).
        crop_params: Optional manual ``--crop`` override forwarded from the CLI.
                     When not ``None``, crop detection is always skipped and the
                     value is persisted to ``probe.yaml`` on each run.
    """

    name: str = "probe"

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

        self._config:      AppConfig           = config
        self._collector:   MetricsCollector    = collector
        self._crop_params: CropParams | None     = crop_params
        self._job:         _JobPhase | None    = cast("_JobPhase",        phases.get(_JobPhase))        if phases else None
        self._extraction:  _ExtractionPhase | None = cast("_ExtractionPhase", phases.get(_ExtractionPhase)) if phases else None

        self.result:       ProbePhaseResult | None = None
        self.dependencies: list[Phase] = [
            dep for dep in (self._job, self._extraction) if dep is not None
        ]

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> ProbePhaseResult:
        """Classify current probe state without running any slow operations.

        Loads ``probe.yaml`` if present and builds a ``COMPLETE`` result from
        the cached values.  Returns ``ABSENT`` when the file is missing.

        Returns:
            ``ProbePhaseResult`` — ``COMPLETE`` when ``probe.yaml`` exists,
            ``ABSENT`` (as ``DRY_RUN`` outcome) otherwise.  Never runs ffmpeg
            or crop detection.
        """
        if self.result is not None:
            return self.result

        dep_result = self._ensure_dependencies(execute=False)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result       = self._job.result         # type: ignore[union-attr]
        probe_yaml_path  = job_result.work_dir / _PROBE_YAML_NAME  # type: ignore[operator]
        probe_state      = ProbeState.load(probe_yaml_path)

        probe_artifact = Artifact(path=probe_yaml_path, state=ArtifactState.ABSENT)

        if probe_state is None:
            self.result = ProbePhaseResult(
                outcome   = PhaseOutcome.DRY_RUN,
                artifacts = [probe_artifact],
                message   = "probe.yaml not found",
                source    = None,
                crop      = CropParams(),
            )
            return self.result

        # probe.yaml exists — build ExtendedVideoMetadata from cached values
        probe_artifact.state = ArtifactState.COMPLETE
        source_vm    = self._get_source_vm(job_result)
        extended_vm  = ExtendedVideoMetadata.from_base(source_vm, frame_count=probe_state.frame_count)
        crop         = probe_state.crop or CropParams()

        self.result = ProbePhaseResult(
            outcome   = PhaseOutcome.REUSED,
            artifacts = [probe_artifact],
            message   = "probe.yaml loaded",
            source    = extended_vm,
            crop      = crop,
        )
        return self.result

    def run(self, dry_run: bool = False) -> ProbePhaseResult:
        """Resolve crop and frame count, persist ``probe.yaml``, return result.

        Sequence:
        1. Emit phase banner.
        2. Ensure dependencies have results (scan if needed).
        3. If ``extraction_result.video is None`` → return ``FAILED``.
        4. If ``probe.yaml`` is fully cached and no manual ``--crop`` → return ``REUSED``.
        5. Resolve crop: manual ``--crop`` → cached from ``probe.yaml`` →
           ``detect_crop_parameters()`` on extracted video.
        6. Resolve frame count: cached from ``probe.yaml`` →
           ``job_result.job.source.probe_extended()``.
        7. Write ``probe.yaml`` via ``.tmp``-then-rename (``ProbeState.save()``).
        8. Return ``COMPLETED``.

        Args:
            dry_run: When ``True``, report what would be done without writing files.

        Returns:
            ``ProbePhaseResult`` on success; ``FAILED`` when no video was extracted.
        """
        from pyqenc.utils.log_format import emit_phase_banner
        emit_phase_banner("PROBE", logger)

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result        = self._job.result        # type: ignore[union-attr]
        extraction_result = self._extraction.result # type: ignore[union-attr]
        probe_yaml_path   = job_result.work_dir / _PROBE_YAML_NAME  # type: ignore[operator]

        # Step 3: fail fast when no video was extracted
        if extraction_result.video is None:  # type: ignore[union-attr]
            err = "No video tracks extracted — video processing cannot continue"
            logger.error(err)
            logger.info(THICK_LINE)
            self.result = ProbePhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = [Artifact(path=probe_yaml_path, state=ArtifactState.ABSENT)],
                message   = err,
                error     = err,
                source    = None,
                crop      = CropParams(),
            )
            return self.result

        # Load existing probe.yaml (may be None)
        probe_state = ProbeState.load(probe_yaml_path)

        # Step 4: return REUSED when fully cached and no manual crop override
        if (
            probe_state is not None
            and probe_state.frame_count > 0
            and probe_state.crop is not None
            and self._crop_params is None
        ):
            source_vm   = self._get_source_vm(job_result)
            extended_vm = ExtendedVideoMetadata.from_base(
                source_vm, frame_count=probe_state.frame_count
            )
            logger.info("Probe: all values cached — reusing probe.yaml")
            logger.info(THICK_LINE)
            self.result = ProbePhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = [Artifact(path=probe_yaml_path, state=ArtifactState.COMPLETE)],
                message   = "probe.yaml reused",
                source    = extended_vm,
                crop      = probe_state.crop,
            )
            return self.result

        # Dry-run: report what would be done
        if dry_run:
            self.result = ProbePhaseResult(
                outcome   = PhaseOutcome.DRY_RUN,
                artifacts = [Artifact(path=probe_yaml_path, state=ArtifactState.ABSENT)],
                message   = "dry-run: probe not yet complete",
                source    = None,
                crop      = CropParams(),
            )
            return self.result

        # Step 5: resolve crop
        crop = self._resolve_crop(probe_state, extraction_result.video)  # type: ignore[union-attr]

        # Step 6: resolve frame count
        frame_count, extended_vm = self._resolve_frame_count(probe_state, job_result)

        # Step 7: persist probe.yaml via .tmp-then-rename (handled by ProbeState.save)
        new_probe_state = ProbeState(
            frame_count = frame_count,
            crop        = crop,
        )
        new_probe_state.save(probe_yaml_path)

        logger.info(
            "Probe: frame_count=%d  crop=%s",
            frame_count,
            crop.display(),
        )
        logger.info(THICK_LINE)

        # Step 8: return COMPLETED
        self.result = ProbePhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = [Artifact(path=probe_yaml_path, state=ArtifactState.COMPLETE)],
            message   = f"probed frame_count={frame_count}, crop={crop}",
            source    = extended_vm,
            crop      = crop,
        )
        return self.result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, execute: bool) -> ProbePhaseResult | None:
        """Scan or run dependencies; fail fast when they are incomplete.

        Args:
            execute: When ``True``, call ``dep.run()`` on un-run dependencies;
                     when ``False``, call ``dep.scan()``.

        Returns:
            A ``FAILED`` ``ProbePhaseResult`` if any dependency is incomplete;
            ``None`` otherwise.
        """
        if self._job is None or self._extraction is None:
            err = "ProbePhase requires JobPhase and ExtractionPhase dependencies"
            return ProbePhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = [],
                message   = err,
                error     = err,
                source    = None,
                crop      = CropParams(),
            )

        for dep in (self._job, self._extraction):
            if dep.result is None:
                if execute:
                    dep.run()
                else:
                    dep.scan()

        if not self._job.result.is_complete:  # type: ignore[union-attr]
            err = "JobPhase did not complete successfully"
            logger.warning("ProbePhase skipping: %s", err)
            return ProbePhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = [],
                message   = err,
                error     = err,
                source    = None,
                crop      = CropParams(),
            )

        if not self._extraction.result.is_complete:  # type: ignore[union-attr]
            err = "ExtractionPhase did not complete successfully"
            logger.warning("ProbePhase skipping: %s", err)
            return ProbePhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = [],
                message   = err,
                error     = err,
                source    = None,
                crop      = CropParams(),
            )

        return None

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

    def _resolve_crop(
        self,
        probe_state:   ProbeState | None,
        extracted_vm:  VideoMetadata,
    ) -> CropParams:
        """Resolve crop using the priority order: manual → cached → auto-detect.

        Args:
            probe_state:  Existing ``probe.yaml`` state (may be ``None``).
            extracted_vm: ``VideoMetadata`` for the *extracted* video file
                          (not the source) — used for crop detection.

        Returns:
            Resolved ``CropParams``.
        """
        from pyqenc.utils.crop import detect_crop_parameters

        # 1. Manual --crop override
        if self._crop_params is not None:
            c = self._crop_params
            logger.info("Crop: %s (manual)", c.display())
            return c

        # 2. Cached in probe.yaml
        if probe_state is not None and probe_state.crop is not None:
            c = probe_state.crop
            logger.info("Crop: %s (cached)", c.display())
            return c

        # 3. Auto-detect on extracted video
        logger.info("Detecting crop: %s", extracted_vm.path.name)
        crop = detect_crop_parameters(extracted_vm)
        return crop

    def _resolve_frame_count(
        self,
        probe_state: ProbeState | None,
        job_result:  JobPhaseResult,
    ) -> tuple[int, ExtendedVideoMetadata]:
        """Resolve frame count: cached from ``probe.yaml`` → ``probe_extended()``.

        Args:
            probe_state: Existing ``probe.yaml`` state (may be ``None``).
            job_result:  Completed ``JobPhaseResult``.

        Returns:
            ``(frame_count, extended_vm)`` tuple.
        """
        source_vm = self._get_source_vm(job_result)

        # 1. Cached in probe.yaml
        if probe_state is not None and probe_state.frame_count > 0:
            logger.debug("Frame count: %d (cached)", probe_state.frame_count)
            return probe_state.frame_count, ExtendedVideoMetadata.from_base(
                source_vm, frame_count=probe_state.frame_count
            )

        # 2. Slow null-encode probe
        extended_vm = source_vm.probe_extended()
        return extended_vm.frame_count, extended_vm
