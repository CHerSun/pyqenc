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

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from pyqenc.constants import TEMP_SUFFIX, THICK_LINE
from pyqenc.metrics import MetricKey
from pyqenc.models import (
    CropParams,
    PhaseOutcome,
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
from pyqenc.state import ProbeState
from pyqenc.stream_model import ExtendedVideoStream, VideoStream
from pyqenc.utils.crop import detect_crop_parameters
from pyqenc.utils.ffmpeg_runner import FrameCountError, get_frame_count
from pyqenc.utils.fs import remove_stale_tmp_file
from pyqenc.utils.timestamps import count_frames

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# ProbePhaseResult
# ---------------------------------------------------------------------------

@dataclass
class ProbePhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying probe-specific payload.

    Attributes:
        stream: The extended-video-stream artifact — the slow facet (frame
                count + crop) over the base video stream; ``None`` when no
                video stream exists and ``ProbePhase`` returned ``FAILED``.
    """

    stream: Artifact[ExtendedVideoStream] | None = field(default=None)

    @property
    def crop(self) -> CropParams:
        """Resolved crop — derived from the stream artifact; empty when absent."""
        if self.stream is None:
            return CropParams()
        return self.stream.payload.crop


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
    (the sidecar), distinct from the artifact. The phase emits no banner — it
    logs a concise INFO line when the slow probe starts and a result line with
    the probed (or cached) details.

    Args:
        config:      Full validated application configuration.
        phases:      Phase registry; used to resolve typed dependency references.
        collector:   Metrics collector for timing instrumentation; recovery and
                     probe work are timed under ``probe`` /
                     ``probe.crop_detect`` / ``probe.frame_count``.
        crop_params: Optional manual ``--crop`` override forwarded from the CLI.
                     When ``not None`` it invalidates the cached sidecar (cheap
                     rewrite reusing the cached frame count).
    """

    name:        str       = "probe"
    SIDECAR_NAME = "probe.yaml"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ExtractionPhase)
    BANNER:      bool      = False
    _METRIC_KEY: MetricKey = MetricKey.PROBE

    def __init__(
        self,
        config:      AppConfig,
        phases:      PhaseRegistry,
        *,
        collector:   MetricsCollector,
        crop_params: CropParams | None = None,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self._crop_params: CropParams | None = crop_params

        # Recovery stash — the loaded probe.yaml state, the extraction stream
        # and the resolved payload for result construction.
        self._probe_state:     ProbeState | None          = None
        self._video_stream:    VideoStream | None          = None
        self._resolved:        ExtendedVideoStream | None  = None

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _recover(self) -> Recovery:
        """Determine ``probe.yaml`` currency: absent, invalidated, or current.

        Steps:

        1. Fail fast when the source has no video stream — a fatal
           invalidation for every downstream video phase.
        2. Remove a leftover ``probe.yaml.tmp`` from an interrupted write.
        3. Load ``probe.yaml``. Pending when absent (full probe needed) or
           when a manual ``--crop`` override invalidates the cached crop
           (cheap rewrite: the frame count stays cached). Current otherwise —
           the sidecar is written atomically, so presence implies complete
           data (no content peeking).

        Returns:
            The :class:`Recovery` single source of truth — one
            ``Artifact[ExtendedVideoStream]`` row (``ABSENT`` with the
            unknown-sentinel composition while pending; ``COMPLETE`` with the
            resolved slow facet when current, stashed on ``self._resolved``).

        Raises:
            RecoveryError: When the source has no video stream.
        """
        job_result        = self._dep_result(JobPhase)
        extraction_result = self._dep_result(ExtractionPhase)
        probe_yaml        = job_result.work_dir / ProbePhase.SIDECAR_NAME

        # Step 1 — no video stream: fatal for all downstream video phases.
        video_artifact = extraction_result.video_stream
        if video_artifact is None:
            raise RecoveryError(
                "No video stream in the source — video processing cannot continue"
            )
        self._video_stream = video_artifact.payload

        # Step 2 — .tmp pre-clean (probe.yaml is written via .tmp-then-rename).
        remove_stale_tmp_file(probe_yaml.with_name(probe_yaml.name + TEMP_SUFFIX))

        # Step 3 — load + currency.
        self._probe_state = ProbeState.load(probe_yaml)
        if self._probe_state is None or self._crop_params is not None:
            # Absent (full probe needed), or a manual --crop override
            # invalidates the cached crop (cheap rewrite: the frame count
            # stays cached). The row is ABSENT with the unknown-sentinel
            # composition — the slow facet is unresolved until probed.
            placeholder = ExtendedVideoStream(
                stream      = self._video_stream,
                frame_count = 0,
                crop        = CropParams(),
            )
            return Recovery.from_artifacts([
                Artifact(payload=placeholder, state=ArtifactState.ABSENT),
            ])

        self._resolved = ExtendedVideoStream(
            stream      = self._video_stream,
            frame_count = self._probe_state.frame_count,
            crop        = self._probe_state.crop,
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
        probe_yaml = self._dep_result(JobPhase).work_dir / ProbePhase.SIDECAR_NAME
        probe_state = self._probe_state
        video       = self._video_stream
        assert video is not None  # recovery guarantees a video stream

        needs_crop_detect = self._crop_params is None and probe_state is None
        needs_frame_probe = probe_state is None or probe_state.frame_count <= 0
        if needs_crop_detect or needs_frame_probe:
            logger.info(
                "Probe: starting long probe%s%s — may take a while",
                " + crop detect" if needs_crop_detect else "",
                " + frame count" if needs_frame_probe else "",
            )

        if self._crop_params is not None:
            crop = self._crop_params
            logger.info("Crop: %s (manual)", crop.display())
        elif probe_state is not None:
            crop = probe_state.crop
            logger.info("Crop: %s (cached)", crop.display())
        else:
            logger.info("Detecting crop: %s", video.file.path.name)
            with self._collector.time(MetricKey.PROBE, "crop_detect"):
                crop = detect_crop_parameters(video)

        # Resolve frame count: cached → timestamps.txt count → null-count pass.
        if probe_state is not None and probe_state.frame_count > 0:
            frame_count = probe_state.frame_count
            logger.debug("Frame count: %d (cached)", frame_count)
        else:
            frame_count = self._count_source_frames()

        # Persist and stash the payload.
        ProbeState(frame_count=frame_count, crop=crop).save(probe_yaml)
        self._resolved = ExtendedVideoStream(
            stream      = video,
            frame_count = frame_count,
            crop        = crop,
        )

        logger.info(
            "Probe: done — frame_count=%d, crop=%s",
            frame_count, crop.display(),
        )
        return self._make_result(PhaseOutcome.COMPLETED, [], "probe resolved")

    def _reused_result(self, wanted: list[Artifact], message: str) -> ProbePhaseResult:
        """Build the reused result from the cached ``probe.yaml`` state."""
        assert self._probe_state is not None  # current currency implies a loaded state
        assert self._video_stream is not None  # recovery guarantees a video stream
        assert self._resolved is not None  # stashed by the current-currency path
        logger.info("Probe: all values cached — reusing probe.yaml")
        logger.info(THICK_LINE)
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
            stream    = (
                Artifact(payload=self._resolved, state=ArtifactState.COMPLETE)
                if self._resolved is not None else None
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _count_source_frames(self) -> int:
        """The source frame count: timestamps.txt count, null-count fallback.

        The timestamps total is exact and free; when the file is unavailable
        the null-count pass runs (timed under ``probe.frame_count``). Zero —
        the unknown sentinel — only when both paths fail.
        """
        video = self._video_stream
        assert video is not None
        timestamps_path = self._dep_result(ExtractionPhase).timestamps_path

        if timestamps_path is not None:
            counted = count_frames(timestamps_path)
            if counted is not None and counted > 0:
                logger.debug("Frame count: %d (timestamps.txt)", counted)
                return counted
            logger.warning(
                "Frame count not derivable from %s — falling back to a null-count pass",
                timestamps_path,
            )

        with self._collector.time(MetricKey.PROBE, "frame_count"):
            try:
                counted = get_frame_count(video.file.path)
            except FrameCountError as exc:
                logger.error("Could not determine source frame count: %s", exc)
                return 0
        logger.debug("Frame count: %d (null-count pass)", counted)
        return counted
