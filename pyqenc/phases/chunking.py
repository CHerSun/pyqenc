"""ChunkingPhase — scene boundaries → timestamp-window chunks.

Two-mode statistical chunking (spec ``2026-11-21 chunking-rework``): a
mode-dependent scoring provider produces per-frame feature traces (the
content score always first; slow mode appends the probe encode's codec
traces), one shared splitter (:mod:`pyqenc.chunking_split` — the
causal-Hampel descent) selects boundaries from them, and the phase
persists the result. Direct-from-source model (spec
``2026-09-25 file-stream-model``): chunks are
:class:`~pyqenc.stream_model.VideoStreamChunk` windows over the source's
extended video stream — no files are produced, no per-chunk sidecars are
written. ``chunking.yaml`` persists the boundary records
(``{timestamp_seconds, frame}``); chunk windows derive from the boundaries
plus the stream duration at load time, and each chunk's frame count derives
from the boundary frames (the last chunk closes against the source total) —
Σ chunk counts telescope to the source count by construction.
"""
# CHerSun 2026

import asyncio
import logging
import math
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel
from scenedetect import ContentDetector, SceneManager, StatsManager, open_video

from pyqenc.app_config import ChunkingMode
from pyqenc.app_metrics import AppMetricKey
from pyqenc.chunking_split import split_boundaries
from pyqenc.constants import CHUNKING_WORKSPACE_DIR
from pyqenc.models import (
    ArtifactState,
    CleanupLevel,
    CropParams,
    PhaseOutcome,
    SceneBoundary,
    SizedFingerprintYaml,
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
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.probe import ProbePhase
from pyqenc.quality import run_metrics
from pyqenc.quality_metrics import MetricType
from pyqenc.stream_model import (
    ExtendedVideoStream,
    VideoStreamChunk,
)
from pyqenc.utils.alive import AdvanceState, ProgressBar, alive_bar
from pyqenc.utils.ffmpeg_runner import (
    FFmpegInput,
    FFmpegRequest,
    run_ffmpeg,
)
from pyqenc.utils.log_format import fmt_key_value_table
from pyqenc.utils.timestamps import parse_frame_timestamps
from pyqenc.utils.yaml_utils import load_model, save_model

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.app_metrics import AppMetricsCollector


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sidecar models
# ---------------------------------------------------------------------------

class SceneRecord(BaseModel):
    """One persisted scene boundary.

    Attributes:
        timestamp_seconds: The boundary's timestamp on the source timeline —
                           the frame's real PTS from the extraction index,
                           never an idealized grid time.
        frame:             The boundary's frame index. Consecutive index
                           differences ARE the chunk frame counts (telescoping
                           to the source total by construction) —
                           load-bearing on the reload path, not informational.
    """

    timestamp_seconds: float
    frame:             int


class ChunkingSidecar(BaseModel):
    """The ``chunking.yaml`` slice: scene boundaries + the detection keys.

    Chunk windows are derived from these boundaries plus the stream duration
    at load time — no per-chunk records, no per-chunk sidecars.

    Keys (spec 2026-10-07, Req 26/31; reworked 2026-11-21): the source
    identity (fingerprint only) and the three detection parameters — a
    change in any re-detects (identity: catastrophic per Req 33; params:
    automatic re-detect, Req 45). An absent key (legacy sidecar) is unknown,
    never a mismatch (Req 32).

    Attributes:
        source:                The source identity key (fingerprint only) —
                               required; a sidecar missing it fails
                               validation → absent → re-detection (Req 32
                               amended 2026-10-10).
        mode:             The scoring regime the boundaries were detected
                          under.
        min_scene_length: The hard minimum segment length (frames) in
                          force at detection.
        scenes:           Scene boundaries in order; the first is the
                          stream start.
    """

    source:           SizedFingerprintYaml
    mode:             ChunkingMode
    min_scene_length: int
    scenes:           list[SceneRecord]


# ---------------------------------------------------------------------------
# Scoring providers — the mode-dependent seam
# ---------------------------------------------------------------------------

SCORER_ONLY_THRESHOLD: float = 1e9
"""The decode-side detector threshold: impossibly high, so the detector's
own cut logic never fires — the library is used purely as a scorer via its
stats manager. No detector threshold exists anywhere in the pipeline."""

CONTENT_SCORE_METRIC: str = "content_val"
"""PySceneDetect's stats-manager key for the per-frame content score."""

PROBE_CODEC: str = "libx265"
"""The slow mode's probe codec."""
PROBE_PRESET: str = "ultrafast"
"""The slow mode's probe preset — a difficulty trace, not a deliverable."""
PROBE_QUALITY: str = "23"
"""The slow mode's fixed probe CRF (internal calibration, not a user
surface)."""
PROBE_PIXEL_FORMAT: str = "yuv420p10le"
"""The slow mode's probe pixel format (matches the project encoding norm)."""
PROBE_X265_PARAMS: str = "log-level=error"
"""The slow mode's probe x265 verbosity — the probe runs inside a progress
bar; encoder spam is noise."""
PROBE_FILENAME: str = "probe.mkv"
"""The slow mode's probe encode file name (inside the chunking workspace)."""
PROBE_METRIC_PREFIX: str = "probe."
"""The output prefix for the probe's PSNR/SSIM stats files (the workspace
is chunking-owned; a fixed prefix keeps the intermediates debuggable)."""
PSNR_LOSSLESS_CAP: float = 100.0
"""PSNR cap for lossless frames (ffmpeg logs ``inf``): the 0–100 normalized
scale's lossless end, as the probe measurement's per-frame values feed the
splitter's float arithmetic."""

PROBE_ENCODE_ARGS: tuple[str | os.PathLike, ...] = (
    "-c:v",        PROBE_CODEC,
    "-preset",     PROBE_PRESET,
    "-crf",        PROBE_QUALITY,
    "-pix_fmt",    PROBE_PIXEL_FORMAT,
    "-x265-params", PROBE_X265_PARAMS,
)
"""The slow mode's probe encode stage (full res, no crop — the probe
measures difficulty regimes, not delivery pixels)."""


@dataclass(frozen=True)
class ScoringResult:
    """One scoring provider's output for the splitter.

    Attributes:
        traces: Per-feature per-frame values, aligned by frame index over
                the source; the FIRST trace is always the content score
                (the splitter's primary feature — offer order and raw
                guards are defined in its units). The provider owns the
                rest of feature construction (the slow mode's bits trace
                is ``log1p``-transformed packet byte sizes).
    """

    traces: list[list[float]]


def _content_decode(source: Path, frame_count: int) -> list[float]:
    """Decode once via PySceneDetect and score the pipeline's frames.

    The frame axis is the pipeline's own: ``frame_count`` is the source's
    frame count as the extraction PTS index records it, and the decoder
    must deliver a score for every one of those frames — boundaries are
    indices into that index. The decoder's own frame count is deliberately
    not consulted: the OpenCV backend estimates it from the container
    duration (the longest stream's, which can outlast the video), so on
    some sources it differs from the frames actually decoded.

    The detector runs purely as a scorer (its threshold is impossibly
    high, its cut logic never fires); the per-frame content score from the
    stats manager is the trace. Frame 0 has no predecessor and scores 0.

    Args:
        source:     The source video path.
        frame_count: The source's frame count (the PTS index's frame lines).

    Returns:
        The per-frame content-score trace — one entry per pipeline frame.

    Raises:
        RecoveryError: When the decoder has no score for one of the frames
                       or scored past them: its frames and the pipeline's
                       cannot be matched up, so boundaries cannot be
                       anchored.
        Exception: Whatever PySceneDetect raises on decode failure — the
                   phase converts it to a typed FAILED result.
    """
    assert frame_count >= 1, "the PTS index carries at least the frame the phase requires"
    logger.info("Content scoring: analyzing %s", source.name)
    stats   = StatsManager()
    manager = SceneManager(stats_manager=stats)
    manager.add_detector(ContentDetector(threshold=SCORER_ONLY_THRESHOLD))
    with alive_bar(title="Content scoring", monitor=False, stats=False) as bar:
        # PySceneDetect invokes ffmpeg internally; redirect its stderr to
        # suppress noise like "[matroska,webm @ ...] Unsupported encoding type".
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        old_stderr_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)
        try:
            video = open_video(str(source))
            manager.detect_scenes(video, show_progress=False)
        finally:
            os.dup2(old_stderr_fd, 2)
            os.close(old_stderr_fd)
        bar()

    keys  = [CONTENT_SCORE_METRIC]
    trace = [0.0]
    for frame_num in range(1, frame_count):
        if not stats.metrics_exist(frame_num, keys):
            raise RecoveryError(
                f"The content decoder scored no frame {frame_num} of the "
                f"{frame_count} frames in the PTS index — its frames and the "
                "pipeline's cannot be matched up, so boundaries cannot be "
                "anchored."
            )
        trace.append(float(stats.get_metrics(frame_num, keys)[0]))
    if stats.metrics_exist(frame_count, keys):
        raise RecoveryError(
            f"The content decoder scored frame {frame_count} and beyond — "
            f"more frames than the {frame_count} in the PTS index, so "
            "boundaries cannot be anchored."
        )
    logger.info("Content scoring: %d frame(s) scored", len(trace))
    return trace


def fast_scoring(
    stream:     ExtendedVideoStream,
    frame_count: int,
) -> ScoringResult:
    """The fast scoring provider: one decode → the content-score trace.

    The content score is an EVENT signal (a spike at each cut); the
    descent's Hampel significance consumes it directly. The detector's own
    cut logic and threshold play no role.

    Args:
        stream:      The source's extended video stream (reads the file).
        frame_count: The source's frame count (the extraction PTS index's
                     frame lines); the trace carries one entry per frame.

    Returns:
        One feature trace (the content score per frame).

    Raises:
        RecoveryError: When the decoder does not score exactly the
                       pipeline's frames.
    """
    return ScoringResult(traces=[_content_decode(stream.stream.file.path, frame_count)])


def slow_scoring(
    stream:           ExtendedVideoStream,
    work_dir:         Path,
    duration_seconds: float,
    average_fps:      float,
    frame_count:      int,
) -> ScoringResult:
    """The slow scoring provider: content trace + probe measurement traces.

    The same method as fast mode, extended with the actually-encoded
    evidence: a probe encode (h265 ultrafast, fixed CRF, full res, no
    crop — chunking's own work; the probe phase stays metadata-only)
    yields per-frame PSNR/SSIM vs the source and per-packet byte sizes as
    extra features. The content trace leads (the splitter's primary
    feature).

    Args:
        stream:           The source's extended video stream.
        work_dir:         The run's work directory (hosts the chunking
                          workspace with the probe and its traces).
        duration_seconds: Source duration in seconds (progress bars).
        average_fps:      The source's true average frame rate (the
                          index-based re-timing of the measurement pass).
        frame_count:      The source's frame count (the extraction PTS
                          index's frame lines); the content trace carries
                          one entry per frame.

    Returns:
        Four feature traces: [content, psnr, ssim, log-bits].

    Raises:
        RecoveryError: When the content decoder does not score exactly the
                       pipeline's frames.
        RuntimeError:         When the probe encode or the measurement pass
                             fails.
        subprocess.CalledProcessError: When the packet scan fails.
    """
    source    = stream.stream.file.path
    probe_dir = work_dir / CHUNKING_WORKSPACE_DIR
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_path = probe_dir / PROBE_FILENAME

    content = _content_decode(source, frame_count)

    if probe_path.exists():
        logger.debug("Probe encode reused: %s", probe_path.name)
    else:
        logger.info(
            "Probe encoding: %s (h265 ultrafast, CRF %s, full res)",
            source.name, PROBE_QUALITY,
        )
        with ProgressBar(duration_seconds, title="Chunking probe") as advance:
            result = run_ffmpeg(
                FFmpegRequest(
                    inputs      = [stream.stream.as_input()],
                    output_args = PROBE_ENCODE_ARGS,
                    output      = probe_path,
                ),
                progress_callback=lambda _, out_time: advance(out_time, AdvanceState.PROGRESS),
            )
            advance(0, AdvanceState.COMPLETE)
        if not result.success:
            raise RuntimeError(
                f"chunking probe encode failed (exit {result.returncode})"
            )

    logger.info("Probe measurement: per-frame PSNR/SSIM vs %s", source.name)
    with ProgressBar(duration_seconds, title="Chunking measurement") as advance:
        metrics_result = asyncio.run(run_metrics(
            metrics         = {MetricType.PSNR, MetricType.SSIM},
            distorted       = FFmpegInput(path=probe_path),
            reference       = stream.stream.as_input(),
            crop_distorted  = CropParams(),
            crop_reference  = CropParams(),
            scale_distorted = None,
            scale_reference = None,
            use_gpu         = False,
            sampling        = 1,
            output_prefix   = PROBE_METRIC_PREFIX,
            cwd             = probe_dir,
            progress_callback=lambda _, out_time: advance(out_time, AdvanceState.PROGRESS),
            fps             = average_fps,
        ))
        advance(0, AdvanceState.COMPLETE)
    if not metrics_result.success:
        raise RuntimeError(
            f"chunking probe measurement failed (exit {metrics_result.returncode})"
        )

    psnr = [
        PSNR_LOSSLESS_CAP if math.isinf(value) else value
        for value in MetricType.PSNR.info.parse_log(probe_dir / f"{PROBE_METRIC_PREFIX}psnr.log")
    ]
    ssim = MetricType.SSIM.info.parse_log(probe_dir / f"{PROBE_METRIC_PREFIX}ssim.log")
    sizes = _packet_byte_sizes(probe_path)
    return ScoringResult(
        traces = [content, psnr, ssim, [math.log1p(size) for size in sizes]],
    )


def _packet_byte_sizes(probe: Path) -> list[float]:
    """Per-frame video byte sizes of the probe encode, via ffprobe.

    One packet per encoded frame, ordered by presentation timestamp — the
    difficulty axis at fixed quality: expensive frames cost bits, cheap
    ones do not. Presentation order is required, not stored order: with
    reordered frames (B-frames) the two differ, and the measurement
    traces follow presentation order.

    Args:
        probe: The probe encode path.

    Returns:
        The packet byte sizes in presentation order.

    Raises:
        subprocess.CalledProcessError: When ffprobe fails.
        ValueError: When the output carries no packets or an unparseable
                    one.
    """
    cmd: list[str | os.PathLike] = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,size",
        "-of", "csv=p=0",
        probe,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    packets = sorted(
        (float(pts_time), float(size))
        for pts_time, size in (
            line.strip().split(",") for line in result.stdout.splitlines()
            if line.strip()
        )
    )
    if not packets:
        raise ValueError(f"ffprobe returned no packets for the probe encode: {probe.name}")
    return [size for _, size in packets]


# ---------------------------------------------------------------------------
# Boundaries → chunk windows
# ---------------------------------------------------------------------------

def build_chunks(
    boundaries: list[SceneBoundary],
    stream:     ExtendedVideoStream,
) -> list[VideoStreamChunk]:
    """Expand scene boundaries into chunk windows with detector-derived counts.

    Every boundary opens a ``[start, end)`` window; each window's frame count
    is the difference of consecutive boundary frames, the last closing
    against the source total. The counts telescope, so
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

    Owns the scoring providers, the splitter wiring, and the
    ``chunking.yaml`` boundary sidecar. The ledger emits one
    ``Artifact[VideoStreamChunk]`` row per chunk window — a virtual entity
    with no per-chunk on-disk presence, so all rows share one state
    (set-flip): ``COMPLETE`` once the persisted boundaries are current, and
    while boundaries are absent the chunk set itself is unknown — the ledger
    is empty and detection must run. The uniform run footprint is inherited
    from :class:`Phase`.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "chunking"
    SIDECAR_NAME = "chunking.yaml"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (
        JobPhase, ExtractionPhase, ProbePhase,
    )
    _METRIC_KEY: AppMetricKey = AppMetricKey.CHUNKING

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry,
        *,
        collector: AppMetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        # Recovery stash — the boundaries loaded from chunking.yaml.
        self._recovered_scenes: list[SceneBoundary] = []

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the chunking phase key input params."""
        fmt_key_value_table({
            "Chunking mode:":       self._config.chunking.mode,
            "Min scene length:":    f"{self._config.chunking.min_scene_length} frames",
        })

    def _invalidate(self) -> None:
        """Key-triggered effects over ``chunking.yaml`` (disk effects only).

        Identity key (Req 33, C-2): a persisted identity contradicting the
        live source is catastrophic — fatal without the ``--force``
        permission; with it the sidecar is wiped (boundaries of the old
        source's timeline are never current). Detection params (Req 26/45,
        C-1): a ``mode`` / ``min_scene_length`` change discards the
        persisted boundaries — automatic re-detect (user-initiated config
        edit, non-destructive: chunking owns no artifacts; the sidecar is
        overwritten in place). Absent keys are unknown, never a mismatch
        (Req 32).

        Raises:
            RecoveryError: On an identity mismatch without permission.
        """
        job_result = self._deps[JobPhase]
        yaml_path  = job_result.work_dir / ChunkingPhase.SIDECAR_NAME

        sidecar = load_model(yaml_path, ChunkingSidecar)
        if sidecar is not None and sidecar.source != job_result.file.payload.fingerprint:
            if not job_result.force:
                raise RecoveryError(
                    "Source content identity mismatch (chunking.yaml) — the "
                    "persisted boundaries belong to a different source's timeline.  "
                    "Re-run with --force to grant permission to discard them "
                    "and re-detect on the new source."
                )
            logger.warning(
                "Source identity mismatch (--force granted — discarding "
                "chunking.yaml and re-detecting)"
            )
            yaml_path.unlink(missing_ok=True)
            sidecar = None

        if sidecar is not None and self._params_changed(sidecar):
            logger.info(
                "Chunking parameters changed (mode %s→%s, min scene length "
                "%s→%s frames) — re-detecting boundaries",
                sidecar.mode, self._config.chunking.mode,
                sidecar.min_scene_length, self._config.chunking.min_scene_length,
            )
            yaml_path.unlink(missing_ok=True)

    def _recover(self) -> Recovery:
        """Classify the persisted boundaries (the set-flip ledger).

        Runs after :meth:`_invalidate` settled the sidecar on disk. With
        current boundaries the chunk windows are fully derivable and every
        row is ``COMPLETE`` (the set flips together — chunks derive wholly
        from the sidecar); with absent boundaries the chunk set is
        unknowable, so the ledger is empty.

        Returns:
            The :class:`Recovery` single source of truth.
        """
        work_dir = self._deps[JobPhase].work_dir
        yaml_path = work_dir / ChunkingPhase.SIDECAR_NAME

        sidecar = load_model(yaml_path, ChunkingSidecar)
        if sidecar is not None and sidecar.scenes:
            self._recovered_scenes = [
                SceneBoundary(frame=record.frame, timestamp_seconds=record.timestamp_seconds)
                for record in sidecar.scenes
            ]
            stream = self._deps[ProbePhase].stream
            assert stream is not None, "probe guaranteed complete by the dependency walk"
            # RecoveryError (unknown duration) propagates — the template
            # converts it to the typed FAILED result.
            chunks = build_chunks(self._recovered_scenes, stream.payload)
            rows = [Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks]
            return Recovery.from_artifacts(rows)

        logger.debug("Chunking recovery: chunking.yaml absent or empty — scoring needed")
        return Recovery(pending=True)

    def _params_changed(self, sidecar: ChunkingSidecar) -> bool:
        """Whether a detection parameter differs from the persisted key.

        The detection parameters are required on the loaded sidecar (a
        missing key never loaded — the sidecar is absent and re-detected).
        """
        return (
            sidecar.mode != self._config.chunking.mode
            or sidecar.min_scene_length != self._config.chunking.min_scene_length
        )

    def _execute(
        self,
        wanted:  list,
        dry_run: bool,
    ) -> ChunkingPhaseResult:
        """Score and split (when no boundaries are cached) and emit chunk rows.

        ``dry_run`` is never ``True`` here (chunking is not a readonly-execute
        phase; the template previews instead).

        Args:
            wanted:  The wanted row list (empty until boundaries exist).
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``ChunkingPhaseResult`` with the chunk-window rows.
        """
        job_result = self._deps[JobPhase]
        work_dir   = job_result.work_dir
        stream     = self._deps[ProbePhase].stream
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
                with self._collector.time(AppMetricKey.CHUNKING, "score_and_split"):
                    boundaries = self._detect_boundaries(extended, work_dir)
            except Exception as exc:
                logger.exception("Scene detection failed")
                return self._make_result(PhaseOutcome.FAILED, [], str(exc))
            sidecar_path = work_dir / ChunkingPhase.SIDECAR_NAME
            sidecar = ChunkingSidecar(
                scenes = [
                    SceneRecord(timestamp_seconds=b.timestamp_seconds, frame=b.frame)
                    for b in boundaries
                ],
                source           = job_result.file.payload.fingerprint,
                mode             = self._config.chunking.mode,
                min_scene_length = self._config.chunking.min_scene_length,
            )
            save_model(sidecar_path, sidecar)
            logger.debug("Wrote scene boundaries: %s", sidecar_path.name)
            self._sweep_workspace(job_result.cleanup)

        try:
            chunks = build_chunks(boundaries, extended)
        except RecoveryError as exc:
            return self._make_result(PhaseOutcome.FAILED, [], str(exc))

        rows = [Artifact(payload=c, state=ArtifactState.COMPLETE) for c in chunks]
        return self._make_result(
            PhaseOutcome.COMPLETED, [],
            f"{len(chunks)} window(s)",
            chunks=rows,
        )

    def _detect_boundaries(
        self,
        extended: ExtendedVideoStream,
        work_dir: Path,
    ) -> list[SceneBoundary]:
        """Run the scoring provider and the splitter for this run's mode.

        The stream-start boundary (frame 0) is prepended to the splitter's
        interior boundaries — the first chunk window opens at the source
        start regardless of what the traces say. With no interior
        boundaries the whole video is one chunk.

        Args:
            extended: The source's extended video stream.
            work_dir: The run's work directory.

        Returns:
            The full boundary list, the first at the stream start.

        Raises:
            RecoveryError: When the source duration is unknown (progress
                           bars and the last window need it) or the PTS
                           index is missing.
        """
        duration = extended.stream.info.duration_seconds
        if duration is None:
            raise RecoveryError(
                "Source video duration is unknown — cannot derive chunk windows. "
                "Re-run extraction to re-enumerate the stream inventory."
            )
        pts_seconds = self._load_pts_seconds()
        frame_count = len(pts_seconds)
        frame_period = pts_seconds[-1] / (frame_count - 1)

        if self._config.chunking.mode == "fast":
            scoring = fast_scoring(extended, frame_count)
        else:
            scoring = slow_scoring(
                extended, work_dir, duration, 1.0 / frame_period, frame_count,
            )
        traces = _checked_traces(scoring, pts_seconds)
        interior = split_boundaries(
            traces[0], pts_seconds,
            self._config.chunking.min_scene_length,
            extra_traces = traces[1:],
        )
        logger.info(
            "Statistical chunking: %d boundary(ies) from %d feature trace(s) "
            "(min scene %d frames)",
            len(interior), len(traces),
            self._config.chunking.min_scene_length,
        )
        if not interior:
            logger.warning(
                "Scene detection found 0 boundaries in '%s' -- treating entire "
                "video as one chunk.",
                extended.stream.file.path.name,
            )
        return [SceneBoundary(frame=0, timestamp_seconds=0.0), *interior]

    def _load_pts_seconds(self) -> list[float]:
        """Load the source's per-frame PTS axis from the extraction index.

        The anchoring authority: every boundary timestamp comes from this
        index exactly (the frame-preservation invariant's basis), and its
        frame lines are the pipeline's frame count — the same lines
        :func:`pyqenc.utils.timestamps.count_frames` counts for the probe.

        Returns:
            The per-frame presentation timestamps in seconds (the index's
            frame lines; its end-of-stream entry is not a frame).

        Raises:
            RecoveryError: When the index is absent — extraction owns it
                           unconditionally, so its absence is a broken run
                           state, not a chunking decision.
        """
        timestamps_path = self._deps[ExtractionPhase].timestamps_path
        if timestamps_path is None or not timestamps_path.exists():
            raise RecoveryError(
                "The per-frame PTS index (extracted/timestamps.txt) is missing "
                "— boundaries cannot be anchored. Re-run extraction."
            )
        values = parse_frame_timestamps(timestamps_path)
        if len(values) < 2:
            raise RecoveryError(
                "The per-frame PTS index (extracted/timestamps.txt) carries "
                "fewer than 2 frame entries — boundaries cannot be derived."
            )
        return [value / 1000.0 for value in values]

    def _sweep_workspace(self, cleanup: CleanupLevel) -> None:
        """Delete the chunking workspace when the run sweeps intermediates.

        The probe encode and its traces are consumed the moment the
        boundaries are computed and persisted.

        Args:
            cleanup: The run's cleanup level.
        """
        if cleanup < CleanupLevel.INTERMEDIATE:
            return
        target = self._deps[JobPhase].work_dir / CHUNKING_WORKSPACE_DIR
        if target.exists():
            try:
                shutil.rmtree(target)
                logger.debug("Intermediate cleanup: deleted %s", target)
            except OSError as exc:
                logger.warning("Could not delete %s: %s", target, exc)

    def _reused_result(self, wanted: list, message: str) -> ChunkingPhaseResult:
        """Build the reused result from the cached boundaries."""
        stream = self._deps[ProbePhase].stream
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

        On deep cleanup, deletes the chunking workspace (the probe encode
        and its traces — reproducible consumables the boundary sidecar
        summarizes). ``chunking.yaml`` is a recovery sidecar and is left in
        place; a cleanup failure never fails the run.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        if not ctx.deep_cleanup:
            return
        target = self._deps[JobPhase].work_dir / CHUNKING_WORKSPACE_DIR
        if target.exists():
            try:
                shutil.rmtree(target)
                logger.debug("deep cleanup: deleted %s", target)
            except OSError as exc:
                logger.warning("deep cleanup: could not delete %s: %s", target, exc)


def _checked_traces(
    scoring:     ScoringResult,
    pts_seconds: list[float],
) -> list[list[float]]:
    """The scoring traces, verified to describe exactly the PTS index's frames.

    Boundaries are frame indices into the PTS index, so every trace and
    the index must agree on the frame count. A difference means the frames
    cannot be matched up — a decoder that stopped early or skipped frames,
    or a source that misreports its length — and any boundary would be
    anchored to the wrong timestamp. Nothing is truncated or padded: the
    phase fails instead.

    Args:
        scoring:     The scoring provider's output.
        pts_seconds: The per-frame PTS axis.

    Returns:
        The traces, unchanged.

    Raises:
        RecoveryError: When any trace's length differs from the index's.
    """
    lengths = [len(trace) for trace in scoring.traces]
    if any(length != len(pts_seconds) for length in lengths):
        raise RecoveryError(
            f"The scoring traces do not match the PTS index ({len(pts_seconds)} "
            f"frames): trace lengths are {lengths}. Boundaries cannot be "
            "anchored to frames — a decoder stopped early, or the source "
            "misreports its length."
        )
    return scoring.traces
