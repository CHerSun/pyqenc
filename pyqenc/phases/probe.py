"""ProbePhase — resolves the slow video facet (frame count, crop) after extraction.

This phase sits between ``ExtractionPhase`` and ``ChunkingPhase`` in the
pipeline and owns the two slow video-only operations:

1. **Frame count** — read primarily from the total line count of the
   extracted ``timestamps.txt`` (exact per-frame PTS list, free); falls back
   to a null-count ffmpeg pass when the file is absent/unreadable.
2. **Crop detection** — samples the source (through the video stream's own
   selector) to find black borders; failure falls back to an empty crop with
   a warning.

Both operations are skipped for audio-only runs because ``ProbePhase`` is not
inserted into the audio registry.  The phase is the sole producer/owner of
:class:`~pyqenc.stream_model.ExtendedVideoStream` — the type every
downstream video phase consumes.  The facet persists in ``probe.yaml`` so
subsequent runs skip re-probing.
"""
# CHerSun 2026

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from pyqenc.app_metrics import AppMetricKey
from pyqenc.constants import CROP_SOURCE_DETECTED, CROP_SOURCE_MANUAL, TEMP_SUFFIX
from pyqenc.models import (
    CropParams,
    CropYaml,
    EncodingPlan,
    Fingerprint,
    PhaseOutcome,
    ResolvedScale,
    ScaleMode,
    ScaleYaml,
    SizedFingerprintYaml,
    fingerprinter,
    resolve_scale,
)
from pyqenc.phase import (
    Artifact,
    ArtifactState,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.stream_model import ExtendedVideoStream, VideoStream
from pyqenc.utils.crop import detect_crop_parameters
from pyqenc.utils.ffmpeg_runner import FrameCountError, get_frame_count
from pyqenc.utils.fs import remove_stale_tmp_file
from pyqenc.utils.timestamps import count_frames
from pyqenc.utils.yaml_utils import load_model, save_model

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.app_metrics import AppMetricsCollector

# ---------------------------------------------------------------------------
# probe.yaml — the slow-facet sidecar (re-homed, Req 21)
# ---------------------------------------------------------------------------

class ProbeSidecar(BaseModel):
    """Sidecar model for ``probe.yaml`` (the slow facet).

    Written by ``ProbePhase`` after resolving frame count, crop, and the
    output scale.  Contains only the delta over the extraction inventory:
    ``frame_count``, ``crop``, the crop's provenance, the resolved scale,
    and the source identity key.

    ``frame_count=0`` is the sentinel for "could not be determined" — no valid
    video has zero frames.  ``crop`` is non-optional: an empty
    :class:`CropParams` means "no crop" and persists as ``crop: 0,0,0,0``.
    Loading always materializes a concrete crop — ``None`` ("auto") never
    appears past config. The persisted crop is the model's own storage string
    (``crop: 22,22,0,0`` — one line; the legacy nested-dict form still
    loads). Unset optional fields are omitted by the ``save_model``
    ``exclude_none`` convention (never silently dropped when set).

    ``scale`` is the probe-resolved output scale (non-optional, ``off`` by
    default) — a run-level pixel-shaping key like the crop: a changed
    resolved value invalidates the cached sidecar via the same cheap
    rewrite as a differing ``--crop`` override. The persisted form is the
    one-line storage string (``scale: contain 1920x800 lanczos``).

    ``crop_source`` is HUMAN-FACING provenance (was the committed crop a
    manual override or a detection?) — it never participates in any
    comparison (Req 23); downstream probe keys compare the frame count,
    the crop values, and the resolved scale only. ``None`` for sidecars
    written before the field.

    ``source`` is the identity key (the fingerprint pair, no path — Req 31) —
    REQUIRED: every write path carries it; a sidecar missing it fails
    validation → absent → the facet re-probes (Req 32 amended 2026-10-10).

    ``resulting_resolution`` is the HUMAN-FACING output dimensions
    (``"<w>x<h>"``), derived at write time as
    ``Resolution(source) - crop``, then the resolved scale's output
    dimensions when scaling is applied — the encode's only
    dimension-changing filters are the crop and the scale, so the value
    holds by construction. A display fact only: never an invalidation key,
    never a completeness input; ``None`` when the source resolution is
    unknown or for sidecars written before the field.
    """

    source:      SizedFingerprintYaml
    frame_count: int
    crop:        CropYaml = CropParams()
    scale:       ScaleYaml = ResolvedScale()
    crop_source: str | None         = None
    resulting_resolution: str | None = None


logger = logging.getLogger(__name__)


def _derive_resulting_resolution(
    video: VideoStream,
    crop: CropParams,
    scale: ResolvedScale,
) -> str | None:
    """The output dimensions (``source - crop``, then the scale).

    Human-facing display fact (the "Probe: done" line, ``probe.yaml``) —
    nothing keys on it. ``None`` when the source resolution is unknown;
    a malformed source string raises (it is an ffprobe-built fact —
    loud is correct there).
    """
    dims = video.info.resolution_dims()
    if dims is None:
        return None
    if scale.mode != ScaleMode.OFF:
        return f"{scale.width}x{scale.height}"
    return str(dims - crop)



# ---------------------------------------------------------------------------
# ProbePhaseResult
# ---------------------------------------------------------------------------

@dataclass
class ProbePhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying probe-specific payload.

    Attributes:
        plan:   The run's video encoding plan. Required here by construction:
                Probe exists only in the video registry (the audio registry
                omits it), so the head of the video chain can carry the plan
                non-optionally — the audio-shared phases (Job, Extraction)
                never see it.
        stream: The extended-video-stream artifact — the slow facet (frame
                count + crop) over the base video stream; ``None`` when no
                video stream exists and ``ProbePhase`` returned ``FAILED``.
    """

    plan:   EncodingPlan
    stream: Artifact[ExtendedVideoStream] | None = field(default=None)

    @property
    def crop(self) -> CropParams:
        """Resolved crop — derived from the stream artifact; empty when absent."""
        if self.stream is None:
            return CropParams()
        return self.stream.payload.crop

    @property
    def scale(self) -> ResolvedScale:
        """Resolved output scale — derived from the stream artifact; ``OFF`` when absent."""
        if self.stream is None:
            return ResolvedScale()
        return self.stream.payload.scale

    def probe_fingerprint(self) -> Fingerprint:
        """The opaque probe-facts key: frame count + crop + the applied scale filter.

        THE downstream invalidation key over the probe's pixel-shaping facts
        (replacing the structured field-wise facet comparison) — persisted by
        the optimization and merge sidecars and compared with ``==``, like
        every strategy fingerprint. The scale enters as its APPLIED filter
        (``to_ffmpeg_filter()``), not the resolved model: the mode label is
        not pixel-shaping — contain and cover reaching the same dimensions
        apply the identical filter and must key identically (only ``exact``
        differs at equal dimensions, via its ``setsar=1``). The frame count
        comes from the live upstream result (unknown stays unknown on both
        sides). Opaque by design: the change SITE (the probe rewrite) logs
        what changed.
        """
        return fingerprinter.of_text(
            f"{self.stream.payload.frame_count if self.stream is not None else 0}"
            f"|{self.crop}|{self.scale.to_ffmpeg_filter()}"
        )


# ---------------------------------------------------------------------------
# ProbePhase
# ---------------------------------------------------------------------------

class ProbePhase(Phase[ProbePhaseResult]):
    """Phase object that resolves crop parameters and the source frame count.

    Depends on ``JobPhase`` and ``ExtractionPhase``.  Returns ``FAILED`` when
    the source has no video stream, which cascades to all downstream video
    phases via their ``_ensure_dependencies()`` mechanism.

    The facet is written to ``probe.yaml`` after a successful run so
    subsequent runs skip re-probing. The ledger carries one
    ``Artifact[ExtendedVideoStream]`` row — ``COMPLETE`` iff ``probe.yaml`` is
    current for the live inputs (absent or invalidated by a manual ``--crop``
    override → ``ABSENT``, with the unknown-sentinel composition as the
    payload: frame count 0, empty crop). ``probe.yaml`` remains phase STATE
    (the sidecar), distinct from the artifact. The phase logs a concise INFO
    line when the slow probe starts; the resolved details surface through
    the uniform phase closer.

    Args:
        config:      Full validated application configuration.
        phases:      Phase registry; used to resolve typed dependency references.
        collector:   Metrics collector for timing instrumentation; recovery and
                     probe work are timed under ``probe`` /
                     ``probe.crop_detect`` / ``probe.frame_count``.
        crop_params: Optional manual ``--crop`` override forwarded from the CLI.
                     When ``not None`` it invalidates the cached sidecar (cheap
                     rewrite reusing the cached frame count).
        plan:        The run's video encoding plan — stored on the result as
                     the video chain's entry context (Probe is constructed
                     only in the video registry).
    """

    name:        str       = "probe"
    SIDECAR_NAME = "probe.yaml"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ExtractionPhase)
    _METRIC_KEY: AppMetricKey = AppMetricKey.PROBE

    def __init__(
        self,
        config:      AppConfig,
        phases:      PhaseRegistry,
        *,
        collector:   AppMetricsCollector,
        crop_params: CropParams | None = None,
        plan:        EncodingPlan,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._crop_params: CropParams | None = crop_params
        self._plan:        EncodingPlan      = plan

        # Recovery stash — the loaded probe.yaml state, the extraction stream
        # and the resolved payload for result construction.
        self._probe_sidecar:   ProbeSidecar | None      = None
        self._video_stream:    VideoStream | None          = None
        self._resolved:        ExtendedVideoStream | None  = None

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _invalidate(self) -> None:
        """Key-triggered effects over ``probe.yaml`` (disk effects only).

        The .tmp pre-clean; then the identity key (Req 33, P-1): a persisted
        identity contradicting the live source is catastrophic — fatal
        without the ``--force`` permission; with it, ``probe.yaml`` is wiped
        and the facet re-probed. An absent key (legacy sidecar) is unknown,
        never a mismatch (Req 32).

        Raises:
            RecoveryError: On an identity mismatch without permission.
        """
        job_result = self._deps[JobPhase]
        probe_yaml = job_result.work_dir / ProbePhase.SIDECAR_NAME

        # .tmp pre-clean (probe.yaml is written via .tmp-then-rename).
        remove_stale_tmp_file(probe_yaml.with_name(probe_yaml.name + TEMP_SUFFIX))

        sidecar = load_model(probe_yaml, ProbeSidecar)
        if sidecar is not None and sidecar.source != job_result.file.payload.fingerprint:
            if not job_result.force:
                raise RecoveryError(
                    "Source content identity mismatch (probe.yaml) — the committed "
                    "facet belongs to a different source.  Re-run with --force to "
                    "grant permission to wipe it and re-probe the new source."
                )
            logger.warning(
                "Source identity mismatch (--force granted — wiping probe.yaml "
                "and re-probing)"
            )
            probe_yaml.unlink(missing_ok=True)

    def _recover(self) -> Recovery:
        """Classify ``probe.yaml`` currency: absent, invalidated, or current.

        Runs after :meth:`_invalidate`. The manual ``--crop`` override is
        classification input (Req 43, P-2): an override EQUAL to the
        committed crop is a no-op; a DIFFERING one leaves the row pending
        (a cheap crop-only rewrite at execute — the frame count stays
        cached). The investment consequence of a real facet change is
        handled downstream by the probe-facet keys, not here.

        Raises:
            RecoveryError: When the source has no video stream.
        """
        job_result        = self._deps[JobPhase]
        extraction_result = self._deps[ExtractionPhase]
        probe_yaml        = job_result.work_dir / ProbePhase.SIDECAR_NAME

        # No video stream: fatal for all downstream video phases.
        video_artifact = extraction_result.video_stream
        if video_artifact is None:
            raise RecoveryError(
                "No video stream in the source — video processing cannot continue"
            )
        self._video_stream = video_artifact.payload

        self._probe_sidecar = load_model(probe_yaml, ProbeSidecar)
        crop_override_active = (
            self._crop_params is not None
            and (
                self._probe_sidecar is None
                or self._crop_params != self._probe_sidecar.crop
            )
        )
        # The resolved scale keys like the crop: a stale persisted value (the
        # plan's request resolved differently against the cached crop) leaves
        # the row pending — a cheap scale-only rewrite at execute, the frame
        # count and detected crop stay cached.
        scale_stale = False
        if self._probe_sidecar is not None and not crop_override_active:
            try:
                scale_stale = (
                    self._probe_sidecar.scale
                    != self._resolve_output_scale(self._probe_sidecar.crop)
                )
            except ValueError as exc:
                raise RecoveryError(f"Infeasible scale request: {exc}") from exc

        if self._probe_sidecar is None or crop_override_active or scale_stale:
            # Absent/wiped (full probe needed), a differing manual --crop
            # override, or a stale resolved scale invalidates the cached
            # facet (cheap rewrite: the frame count and detected crop stay
            # cached). The row is ABSENT with the unknown-sentinel
            # composition — the slow facet is unresolved until probed.
            placeholder = ExtendedVideoStream(
                stream      = self._video_stream,
                frame_count = 0,
                crop        = CropParams(),
                scale       = ResolvedScale(),
            )
            return Recovery.from_artifacts([
                Artifact(payload=placeholder, state=ArtifactState.ABSENT),
            ])

        self._resolved = ExtendedVideoStream(
            stream      = self._video_stream,
            frame_count = self._probe_sidecar.frame_count,
            crop        = self._probe_sidecar.crop,
            scale       = self._probe_sidecar.scale,
        )
        return Recovery.from_artifacts([
            Artifact(payload=self._resolved, state=ArtifactState.COMPLETE),
        ])

    def _execute(self, wanted: list[Artifact], dry_run: bool) -> ProbePhaseResult:
        """Resolve crop + frame count and persist ``probe.yaml``.

        The slow operations are individually timed under the dotted keys
        ``probe.crop_detect`` and ``probe.frame_count`` (the top-level
        ``probe`` span belongs to the template). Cache hits skip the slow
        operation entirely. ``dry_run`` is never ``True`` here (probe is not
        a readonly-execute phase; the template previews instead).

        Args:
            wanted:  Always empty (probe.yaml is state, not artifacts).
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``ProbePhaseResult`` with outcome ``COMPLETED``.
        """
        probe_yaml = self._deps[JobPhase].work_dir / ProbePhase.SIDECAR_NAME
        probe_sidecar = self._probe_sidecar
        video       = self._video_stream
        assert video is not None  # recovery guarantees a video stream

        needs_crop_detect = self._crop_params is None and probe_sidecar is None
        needs_frame_probe = probe_sidecar is None or probe_sidecar.frame_count <= 0
        if needs_crop_detect or needs_frame_probe:
            logger.info(
                "Probe: starting long probe%s%s — may take a while",
                " + crop detect" if needs_crop_detect else "",
                " + frame count" if needs_frame_probe else "",
            )

        if self._crop_params is not None:
            crop        = self._crop_params
            crop_source = CROP_SOURCE_MANUAL
            logger.info("Crop: %s (manual)", crop.display())
        elif probe_sidecar is not None:
            crop        = probe_sidecar.crop
            crop_source = probe_sidecar.crop_source or CROP_SOURCE_DETECTED
            logger.info("Crop: %s (cached)", crop.display())
        else:
            logger.info("Detecting crop: %s", video.file.path.name)
            with self._collector.time(AppMetricKey.PROBE, "crop_detect"):
                crop = detect_crop_parameters(video)
            crop_source = CROP_SOURCE_DETECTED

        # Resolve frame count: cached → timestamps.txt count → null-count pass.
        if probe_sidecar is not None and probe_sidecar.frame_count > 0:
            frame_count = probe_sidecar.frame_count
            logger.debug("Frame count: %d (cached)", frame_count)
        else:
            frame_count = self._count_source_frames()

        # Resolve the output scale against the resolved crop; log the applied
        # shape (the no-op and infeasible cases resolve/log themselves).
        try:
            scale = self._resolve_output_scale(crop)
        except ValueError as exc:
            raise RecoveryError(f"Infeasible scale request: {exc}") from exc
        if scale.mode != ScaleMode.OFF:
            logger.info("Scale: %s", scale)
            request = self._plan.scale
            assert request is not None  # an applied scale implies a request
            if (
                scale.mode == ScaleMode.EXACT
                and (scale.width, scale.height) != (request.width, request.height)
            ):
                logger.warning(
                    "Exact scale target %dx%d moved to %dx%d by the codec "
                    "granularity grid",
                    request.width, request.height, scale.width, scale.height,
                )

        # Persist and stash the payload. crop_source and
        # resulting_resolution are human-facing facts only — never keys
        # (Req 23); source is the identity key.
        job_result = self._deps[JobPhase]
        resulting_resolution = _derive_resulting_resolution(video, crop, scale)
        save_model(probe_yaml, ProbeSidecar(
            frame_count          = frame_count,
            crop                 = crop,
            scale                = scale,
            source               = job_result.file.payload.fingerprint,
            crop_source          = crop_source,
            resulting_resolution = resulting_resolution,
        ))
        self._resolved = ExtendedVideoStream(
            stream      = video,
            frame_count = frame_count,
            crop        = crop,
            scale       = scale,
        )

        return self._make_result(
            PhaseOutcome.COMPLETED, [],
            f"frame_count={frame_count}, crop={crop.display()}, "
            f"resolution={resulting_resolution or 'unknown'}",
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> ProbePhaseResult:
        """Build the reused result from the cached ``probe.yaml`` state."""
        assert self._probe_sidecar is not None  # current currency implies a loaded sidecar
        assert self._video_stream is not None  # recovery guarantees a video stream
        assert self._resolved is not None  # stashed by the current-currency path
        return self._make_result(PhaseOutcome.REUSED, [], "probe.yaml reused")

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact],
        message:   str,
    ) -> ProbePhaseResult:
        """Assemble a ``ProbePhaseResult`` from the resolved payload stash.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted row list (transitional population).
            message:   Human-readable summary — on ``FAILED``, the error
                       description.

        Returns:
            The populated result (``stream`` defaults to ``None`` on
            non-complete paths).
        """
        return ProbePhaseResult(
            outcome   = outcome,
            message   = message,
            plan      = self._plan,
            stream    = (
                Artifact(payload=self._resolved, state=ArtifactState.COMPLETE)
                if self._resolved is not None else None
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_output_scale(self, crop: CropParams | None) -> ResolvedScale:
        """Resolve the plan's scale request against the live source stream.

        The granularity is the run's strategies' minimum — one output
        resolution for the whole run, valid for every codec in it.

        Args:
            crop: The crop the scale applies on top of (the override or the
                  cached/detected value at the call site).

        Returns:
            The :class:`~pyqenc.models.ResolvedScale`.

        Raises:
            ValueError: If a scale request exists but the source resolution
                is unknown, or the request is infeasible (below granularity,
                unreachable box).
        """
        video = self._video_stream
        assert video is not None  # recovery guarantees a video stream
        if self._plan.scale is None:
            return ResolvedScale()
        dims = video.info.resolution_dims()
        if dims is None:
            raise ValueError(
                "cannot resolve --scale: the source resolution is unknown"
            )
        return resolve_scale(
            dims,
            video.info.sample_aspect_ratio,
            crop or CropParams(),
            self._plan.scale,
            min(s.codec.resolution_granularity for s in self._plan.strategies),
        )

    def _count_source_frames(self) -> int:
        """The source frame count: timestamps.txt count, null-count fallback.

        The timestamps total is exact and free; when the file is unavailable
        the null-count pass runs (timed under ``probe.frame_count``). Zero —
        the unknown sentinel — only when both paths fail.
        """
        video = self._video_stream
        assert video is not None
        timestamps_path = self._deps[ExtractionPhase].timestamps_path

        if timestamps_path is not None:
            counted = count_frames(timestamps_path)
            if counted is not None and counted > 0:
                logger.debug("Frame count: %d (timestamps.txt)", counted)
                return counted
            logger.warning(
                "Frame count not derivable from %s — falling back to a null-count pass",
                timestamps_path,
            )

        with self._collector.time(AppMetricKey.PROBE, "frame_count"):
            try:
                counted = get_frame_count(video.file.path)
            except FrameCountError as exc:
                logger.error("Could not determine source frame count: %s", exc)
                return 0
        logger.debug("Frame count: %d (null-count pass)", counted)
        return counted
