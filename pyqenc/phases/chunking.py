"""ChunkingPhase — scene boundaries → timestamp-window chunks.

Direct-from-source model (spec ``2026-09-25 file-stream-model``): chunks are
:class:`~pyqenc.stream_model.VideoStreamChunk` windows over the source's
extended video stream — no files are produced, no per-chunk sidecars are
written. ``chunking.yaml`` persists the detector's scene boundaries
(``{timestamp_seconds, frame?}``); chunk windows derive from the boundaries
plus the stream duration at load time, and each chunk's frame count derives
from the detector-reported boundary frames (the last chunk closes against
the source total) — Σ chunk counts telescope to the source count by
construction.
"""
# CHerSun 2026

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from scenedetect import ContentDetector, detect

from pyqenc.metrics import MetricKey
from pyqenc.models import PhaseOutcome, SceneBoundary
from pyqenc.phase import (
    Artifact,
    FinalizeContext,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.probe import ProbePhase
from pyqenc.state import ArtifactState
from pyqenc.stream_model import (
    ChunkingSidecar,
    ExtendedVideoStream,
    SceneRecord,
    VideoStreamChunk,
)
from pyqenc.utils.alive import alive_bar
from pyqenc.utils.yaml_utils import load_model, write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

logger = logging.getLogger(__name__)

_CHUNKING_YAML = "chunking.yaml"


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------

def detect_scenes(
    stream:           ExtendedVideoStream,
    scene_threshold:  float,
    min_scene_length: int,
) -> list[SceneBoundary]:
    """Detect scene boundaries in the source using PySceneDetect.

    Pure computation — does not persist anything.  The caller is responsible
    for persisting the boundaries to ``chunking.yaml``.

    If zero scenes are detected the entire video is treated as a single scene
    (one boundary at frame 0 / t=0.0) and a warning is logged.

    Args:
        stream:           The source's extended video stream (reads the file).
        scene_threshold:  PySceneDetect content threshold — the mean per-pixel
                          distance between adjacent frames (0-255 float; see
                          ``chunking.scene_threshold`` in default_config.yaml,
                          the single source of truth).
        min_scene_length: Minimum frames per scene.

    Returns:
        List of ``SceneBoundary`` objects.
    """
    source_path = stream.stream.file.path
    logger.info("Scene detection: analyzing %s", source_path.name)

    with alive_bar(title="Scene detection", monitor=False, stats=False) as bar:
        # PySceneDetect invokes ffmpeg internally; redirect its stderr to suppress
        # noise like "[matroska,webm @ ...] Unsupported encoding type".
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        old_stderr_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)
        try:
            scene_list = detect(
                str(source_path),
                ContentDetector(threshold=scene_threshold, min_scene_len=min_scene_length),
            )
        finally:
            os.dup2(old_stderr_fd, 2)
            os.close(old_stderr_fd)
        bar()

    if not scene_list:
        logger.warning(
            "Scene detection found 0 scenes in '%s' -- treating entire video as one chunk.",
            source_path.name,
        )
        return [SceneBoundary(frame=0, timestamp_seconds=0.0)]

    boundaries = [
        SceneBoundary(frame=int(scene[0].get_frames()), timestamp_seconds=float(scene[0].get_seconds()))
        for scene in scene_list
    ]
    logger.info("Scene detection: %d scene(s)", len(boundaries))
    return boundaries


# ---------------------------------------------------------------------------
# Boundaries → chunk windows
# ---------------------------------------------------------------------------

def build_chunks(
    boundaries: list[SceneBoundary],
    stream:     ExtendedVideoStream,
) -> list[VideoStreamChunk]:
    """Expand scene boundaries into chunk windows with detector-derived counts.

    Every boundary opens a ``[start, end)`` window; each window's frame count
    is the difference of consecutive detector-reported boundary frames, the
    last closing against the source total. The counts telescope, so
    ``Σ chunk.frame_count == stream.frame_count`` by construction.

    Args:
        boundaries: Scene boundaries in order (the first at the stream start).
        stream:     The source's extended video stream.

    Returns:
        The chunk windows in scene order.

    Raises:
        RecoveryError: When the stream duration is unknown — the last window
                       cannot be closed.
    """
    duration = stream.stream.info.duration_seconds
    if duration is None:
        raise RecoveryError(
            "Source video duration is unknown — cannot close the last chunk window. "
            "Re-run extraction to re-enumerate the stream inventory."
        )

    source_total = stream.frame_count
    chunks: list[VideoStreamChunk] = []
    for i, boundary in enumerate(boundaries):
        start = boundary.timestamp_seconds
        if i + 1 < len(boundaries):
            end        = boundaries[i + 1].timestamp_seconds
            frame_count = boundaries[i + 1].frame - boundary.frame
        else:
            end         = duration
            frame_count = source_total - boundary.frame if source_total > 0 else 0
        chunks.append(VideoStreamChunk(
            stream          = stream,
            start_timestamp = start,
            end_timestamp   = end,
            frame_count     = max(frame_count, 0),
        ))
    return chunks


# ---------------------------------------------------------------------------
# ChunkingPhaseResult
# ---------------------------------------------------------------------------

@dataclass
class ChunkingPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying chunking-specific payload.

    Attributes:
        chunks: The chunk-window artifacts in scene order (virtual windows —
                ``COMPLETE`` together once the persisted boundaries are
                current; empty while detection is still pending).
    """

    chunks: list[Artifact[VideoStreamChunk]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ChunkingPhase
# ---------------------------------------------------------------------------

class ChunkingPhase(Phase[ChunkingPhaseResult]):
    """Phase object turning scene boundaries into timestamp-window chunks.

    Owns scene detection and the ``chunking.yaml`` boundary sidecar. The
    ledger emits one ``Artifact[VideoStreamChunk]`` row per chunk window — a
    virtual entity with no per-chunk on-disk presence, so all rows share one
    state (set-flip): ``COMPLETE`` once the persisted boundaries are current,
    and while boundaries are absent the chunk set itself is unknown — the
    ledger is empty and detection must run. The uniform run footprint is
    inherited from :class:`Phase`.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "chunking"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ExtractionPhase, ProbePhase)
    _METRIC_KEY: MetricKey = MetricKey.CHUNKING

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry | None = None,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        # Recovery stash — the boundaries loaded from chunking.yaml.
        self._recovered_scenes: list[SceneBoundary] = []

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the scene-detection tuning (the phase's key parameters)."""
        logger.info("Scene threshold:       %s", self._config.chunking.scene_threshold)
        logger.info("Min scene length:      %s frames", self._config.chunking.min_scene_length)

    def _recover(self) -> Recovery:
        """Determine ``chunking.yaml`` currency: absent boundaries or current.

        Steps:
        1. If ``force_wipe``: delete ``chunking.yaml``.
        2. Load scene boundaries from ``chunking.yaml``; pending when absent
           or empty (detection must run), current otherwise.

        The set-flip ledger: with current boundaries the chunk windows are
        fully derivable and every row is ``COMPLETE`` (the set flips
        together — chunks derive wholly from the sidecar); with absent
        boundaries the chunk set is unknowable, so the ledger is empty.

        Returns:
            The :class:`Recovery` single source of truth.
        """
        job_result = self._dep_result(JobPhase)
        work_dir   = job_result.work_dir
        yaml_path  = work_dir / _CHUNKING_YAML
        force_wipe = job_result.force_wipe

        # Step 1: force-wipe.
        if force_wipe:
            yaml_path.unlink(missing_ok=True)

        # Step 2: load boundaries.
        sidecar = self._load_sidecar(yaml_path)
        if sidecar is not None and sidecar.scenes:
            self._recovered_scenes = [
                SceneBoundary(frame=record.frame or 0, timestamp_seconds=record.timestamp_seconds)
                for record in sidecar.scenes
            ]
            logger.info("Scenes:  %d (from chunking.yaml)", len(self._recovered_scenes))
            stream = self._dep_result(ProbePhase).stream
            assert stream is not None, "probe guaranteed complete by the dependency walk"
            # RecoveryError (unknown duration) propagates — the template
            # converts it to the typed FAILED result.
            chunks = build_chunks(self._recovered_scenes, stream.payload)
            rows = [Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks]
            return Recovery.from_artifacts(rows)

        logger.debug("Chunking recovery: chunking.yaml absent or empty — scene detection needed")
        return Recovery(pending=True)

    def _execute(
        self,
        wanted:  list,
        dry_run: bool,
    ) -> ChunkingPhaseResult:
        """Detect scenes (when no boundaries are cached) and emit chunk rows.

        ``dry_run`` is never ``True`` here (chunking is not a readonly-execute
        phase; the template previews instead).

        Args:
            wanted:  The wanted row list (empty until boundaries exist).
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``ChunkingPhaseResult`` with the chunk-window rows.
        """
        work_dir = self._dep_result(JobPhase).work_dir
        stream   = self._dep_result(ProbePhase).stream
        assert stream is not None, "probe guaranteed complete by the dependency walk"
        extended = stream.payload

        boundaries = self._recovered_scenes
        if boundaries:
            logger.info(
                "Scene boundaries already in chunking.yaml (%d) — skipping detection.",
                len(boundaries),
            )
        else:
            try:
                with self._collector.time(MetricKey.CHUNKING, "scene_detect"):
                    boundaries = detect_scenes(
                        stream           = extended,
                        scene_threshold  = self._config.chunking.scene_threshold,
                        min_scene_length = self._config.chunking.min_scene_length,
                    )
            except Exception as exc:
                logger.exception("Scene detection failed")
                return self._make_result(PhaseOutcome.FAILED, [], str(exc))
            sidecar_path = work_dir / _CHUNKING_YAML
            sidecar = ChunkingSidecar(scenes=[
                SceneRecord(timestamp_seconds=b.timestamp_seconds, frame=b.frame)
                for b in boundaries
            ])
            write_yaml_atomic(sidecar_path, sidecar.model_dump(exclude_none=True))
            logger.debug("Wrote scene boundaries: %s", sidecar_path.name)

        try:
            chunks = build_chunks(boundaries, extended)
        except RecoveryError as exc:
            return self._make_result(PhaseOutcome.FAILED, [], str(exc))

        logger.info("%d chunk window(s) — no files produced (direct-from-source)", len(chunks))
        rows = [Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks]
        return self._make_result(
            PhaseOutcome.COMPLETED, [],
            f"chunked into {len(chunks)} window(s)",
            chunks=rows,
        )

    def _reused_result(self, wanted: list, message: str) -> ChunkingPhaseResult:
        """Build the reused result from the cached boundaries."""
        stream = self._dep_result(ProbePhase).stream
        assert stream is not None, "probe guaranteed complete by the dependency walk"
        try:
            chunks = build_chunks(self._recovered_scenes, stream.payload)
        except RecoveryError as exc:
            return self._make_result(PhaseOutcome.FAILED, [], str(exc))
        return self._make_result(
            PhaseOutcome.REUSED, [],
            "chunking.yaml reused",
            chunks=[Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks],
        )

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list,
        message:   str,
        chunks:    list[Artifact[VideoStreamChunk]] | None = None,
    ) -> ChunkingPhaseResult:
        """Assemble a ``ChunkingPhaseResult``.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted row list (transitional population).
            message:   Human-readable summary — on ``FAILED``, the error
                       description.
            chunks:    The chunk-window rows (empty on failure paths).

        Returns:
            The populated result.
        """
        return ChunkingPhaseResult(
            outcome   = outcome,
            message   = message,
            chunks    = chunks or [],
        )

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for the chunking phase.

        The phase owns no output artifacts — windows live in memory and the
        boundaries in ``chunking.yaml`` (a recovery sidecar, kept).

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        return

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _load_sidecar(path) -> ChunkingSidecar | None:
        """Load ``chunking.yaml``; ``None`` when absent or unparseable."""
        return load_model(path, ChunkingSidecar)
