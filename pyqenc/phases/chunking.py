"""
Chunking phase for the quality-based encoding pipeline.

This module handles splitting video into scene-based chunks using PySceneDetect.
The phase is split into two independently resumable sub-phases:

1. ``detect_scenes`` -- runs scene detection and persists boundaries to ``chunking.yaml``.
2. ``split_chunks`` -- splits the video at persisted boundaries, writing chunk sidecars.

The ``chunk_video`` entry point orchestrates both sub-phases, calling
``recover_chunking`` first to skip any work already on disk.

Two chunking modes are supported (see ``ChunkingMode``):

* **LOSSLESS** (default): each chunk is re-encoded to FFV1 all-intra (``-g 1``)
  so every frame is an I-frame and splits are frame-perfect.
* **REMUX**: stream-copy (``-c copy``); faster and smaller chunks but boundaries
  snap to the nearest I-frame before the scene timestamp.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from alive_progress import alive_bar, config_handler
from scenedetect import ContentDetector, detect
from scenedetect.video_splitter import is_ffmpeg_available

from pyqenc.constants import (
    CHUNK_NAME_PATTERN,
    RANGE_SEPARATOR,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.utils.alive import ProgressBar
from pyqenc.models import (
    ChunkMetadata,
    ChunkingMode,
    PhaseOutcome,
    SceneBoundary,
    VideoMetadata,
)
from pyqenc.state import (
    ArtifactState,
    ChunkSidecar,
    ChunkingParams,
    JobState,
    JobStateManager,
)
from pyqenc.utils.ffmpeg_runner import run_ffmpeg
from pyqenc.utils.yaml_utils import write_yaml_atomic
from pyqenc.phases.recovery import ChunkingRecovery, discover_inputs, recover_chunking

config_handler.set_global(enrich_print=False)  # type: ignore
logger = logging.getLogger(__name__)


# FFV1 all-intra flags used in lossless chunking mode.
# -g 1 makes every frame an I-frame for frame-perfect splits.
FFV1_VIDEO_ARGS: list[str] = [
    "-c:v",     "ffv1",
    "-g",       "1",
    "-level",   "3",
    "-coder",   "1",
    "-context", "1",
    "-slices",  "24",
]


def _chunk_name_duration(start_ts: float, end_ts: float) -> str:
    """Return the canonical chunk file stem for a timestamp range."""
    start_str = TIME_SEPARATOR_SAFE.join([
        f"{int(start_ts // 3600):02d}",
        f"{int((start_ts % 3600) // 60):02d}",
        f"{start_ts % 60:06.3f}".replace(".", TIME_SEPARATOR_MS),
    ])
    end_str = TIME_SEPARATOR_SAFE.join([
        f"{int(end_ts // 3600):02d}",
        f"{int((end_ts % 3600) // 60):02d}",
        f"{end_ts % 60:06.3f}".replace(".", TIME_SEPARATOR_MS),
    ])
    return f"{start_str}{RANGE_SEPARATOR}{end_str}"


@dataclass
class ChunkingResult:
    chunks:       list[ChunkMetadata]
    total_frames: int
    outcome:      PhaseOutcome
    error:        str | None = None

    @property
    def success(self) -> bool:
        """True when chunking completed or reused existing chunks."""
        return self.outcome in (PhaseOutcome.COMPLETED, PhaseOutcome.REUSED)

    @property
    def reused(self) -> bool:
        """True when existing chunks were reused without new work."""
        return self.outcome == PhaseOutcome.REUSED

    @property
    def needs_work(self) -> bool:
        """True when in dry-run mode and work would be required."""
        return self.outcome == PhaseOutcome.DRY_RUN


def detect_scenes(
    video_meta:       VideoMetadata,
    state_manager:    JobStateManager,
    scene_threshold:  float = 27.0,
    min_scene_length: int   = 15,
) -> list[SceneBoundary]:
    """Detect scene boundaries and persist them to ``chunking.yaml``.

    Runs PySceneDetect ContentDetector on ``video_meta.path`` and stores
    the resulting boundary list via ``state_manager.save_chunking``.
    If zero scenes are detected the entire video is treated as a single scene
    (one boundary at frame 0 / t=0.0) and a warning is logged.

    Args:
        video_meta:       Metadata for the source video file.
        state_manager:    State manager used to persist scene boundaries.
        scene_threshold:  PySceneDetect content-change threshold (default 27.0).
        min_scene_length: Minimum frames per scene (default 15).

    Returns:
        List of ``SceneBoundary`` objects.
    """
    logger.info("Scene detection: analyzing %s", video_meta.path.name)

    with alive_bar(title="Scene detection", monitor=False, stats=False) as bar:
        import os
        # PySceneDetect invokes ffmpeg internally; redirect its stderr to suppress
        # noise like "[matroska,webm @ ...] Unsupported encoding type".
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        old_stderr_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)
        try:
            scene_list = detect(
                str(video_meta.path),
                ContentDetector(threshold=scene_threshold, min_scene_len=min_scene_length),
            )
        finally:
            os.dup2(old_stderr_fd, 2)
            os.close(old_stderr_fd)
        bar()

    if not scene_list:
        logger.warning(
            "Scene detection found 0 scenes in '%s' -- treating entire video as one chunk.",
            video_meta.path.name,
        )
        boundaries: list[SceneBoundary] = [SceneBoundary(frame=0, timestamp_seconds=0.0)]
    else:
        boundaries = [
            SceneBoundary(
                frame=scene_start.get_frames(),
                timestamp_seconds=scene_start.get_seconds(),
            )
            for scene_start, _ in scene_list
        ]
        logger.info("Scene detection complete: %d scene(s) detected.", len(boundaries))

    # Persist scene boundaries to chunking.yaml (Req 2.2)
    state_manager.save_chunking(ChunkingParams(scenes=boundaries))
    logger.debug("Saved %d scene boundary(ies) to chunking.yaml", len(boundaries))

    return boundaries


def split_chunks(
    video_meta:    VideoMetadata,
    output_dir:    Path,
    boundaries:    list[SceneBoundary],
    state_manager: JobStateManager,
    recovery:      ChunkingRecovery,
    chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS,
) -> list[ChunkMetadata]:
    """Split the source video into chunks using scene boundaries.

    Skips chunks already present on disk (as determined by *recovery*).
    Writes a ``<chunk_stem>.yaml`` sidecar after each successful split (Req 5.5).

    Args:
        video_meta:    Metadata for the source video file.
        output_dir:    Directory where chunk files will be written.
        boundaries:    Scene boundaries to split at.
        state_manager: State manager (used for context; sidecars written directly).
        recovery:      Recovery result from ``recover_chunking``; chunks already
                       ``COMPLETE`` are skipped.
        chunking_mode: LOSSLESS (FFV1 all-intra, default) or REMUX (stream-copy).

    Returns:
        List of ``ChunkMetadata`` for every chunk that was successfully split or reused.
    """
    if not boundaries:
        raise RuntimeError(
            "No scene boundaries provided to split_chunks. "
            "Run detect_scenes first."
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve video args once before the loop.
    if chunking_mode == ChunkingMode.LOSSLESS:
        pix_fmt = video_meta.pix_fmt
        if pix_fmt is None:
            logger.warning(
                "Could not determine pixel format for %s; falling back to yuv420p.",
                video_meta.path.name,
            )
            pix_fmt = "yuv420p"
        video_args: list[str] = [*FFV1_VIDEO_ARGS, "-pix_fmt", pix_fmt]
        logger.info(
            "Chunking mode: lossless FFV1 (pix_fmt=%s) — frame-perfect splits.", pix_fmt
        )
    else:
        video_args = ["-c", "copy"]
        logger.info("Chunking mode: remux (stream-copy) — I-frame-snapped splits.")

    result_chunks: list[ChunkMetadata] = []

    # Collect already-complete chunks from recovery first
    complete_from_recovery: dict[str, ChunkMetadata] = {
        chunk_id: rec.metadata
        for chunk_id, rec in recovery.chunks.items()
        if rec.state == ArtifactState.COMPLETE and rec.metadata is not None
    }

    total_seconds = video_meta.duration_seconds or 0.0
    with ProgressBar(total_seconds, title="Chunking") as advance:
        for idx, start_boundary in enumerate(boundaries):
            end_ts = (
                boundaries[idx + 1].timestamp_seconds
                if idx + 1 < len(boundaries)
                else video_meta.duration_seconds or 0.0
            )
            start_ts = start_boundary.timestamp_seconds
            stem     = _chunk_name_duration(start_ts, end_ts)
            chunk_file = output_dir / f"{stem}.mkv"

            # Skip chunks already COMPLETE from recovery (Req 5.2)
            if stem in complete_from_recovery:
                result_chunks.append(complete_from_recovery[stem])
                logger.debug("Skipping already-complete chunk: %s", stem)
                advance(end_ts - start_ts)
                continue

            duration = end_ts - start_ts
            cmd: list[str | Path] = [
                "ffmpeg", "-y",
                "-ss", str(start_ts),
                "-i", video_meta.path,
                "-t", str(duration),
                *video_args,
                "-an", chunk_file,
            ]
            logger.debug("Splitting chunk %s (%.3fs)", stem, duration)

            chunk_meta = ChunkMetadata(
                path=chunk_file,
                chunk_id=stem,
                start_timestamp=start_ts,
                end_timestamp=end_ts,
            )
            split_result = run_ffmpeg(cmd, output_file=chunk_file, video_meta=chunk_meta)

            if not split_result.success:
                logger.critical(
                    "ffmpeg split failed for chunk %s (exit %d)",
                    stem, split_result.returncode,
                )
                advance(end_ts - start_ts)
                continue

            if not chunk_file.exists() or chunk_file.stat().st_size == 0:
                logger.critical("Chunk file missing or empty after split: %s", chunk_file.name)
                advance(end_ts - start_ts)
                continue

            # Set duration from the known timestamp range
            chunk_meta._duration_seconds = end_ts - start_ts

            if chunk_meta._frame_count is None:
                logger.warning("Frame count not found in ffmpeg output for chunk %s", stem)

            # Write chunk sidecar (Req 5.5)
            _write_chunk_sidecar(chunk_file, chunk_meta)

            result_chunks.append(chunk_meta)
            logger.debug("Chunk %s split successfully", stem)
            advance(end_ts - start_ts)

    return result_chunks


def _write_chunk_sidecar(chunk_file: Path, chunk_meta: ChunkMetadata) -> None:
    """Write a ``<chunk_stem>.yaml`` sidecar alongside *chunk_file*.

    Args:
        chunk_file:  Path to the chunk ``.mkv`` file.
        chunk_meta:  Metadata to persist in the sidecar.
    """
    sidecar_path = chunk_file.with_suffix(".yaml")
    sidecar = ChunkSidecar(chunk=chunk_meta)
    try:
        write_yaml_atomic(sidecar_path, sidecar.to_yaml_dict())
        logger.debug("Wrote chunk sidecar: %s", sidecar_path.name)
    except Exception as exc:
        logger.warning("Could not write chunk sidecar for %s: %s", chunk_file.name, exc)


def chunk_video(
    video_file:       Path,
    output_dir:       Path,
    state_manager:    JobStateManager,
    job:              JobState,
    chunking_mode:    ChunkingMode = ChunkingMode.LOSSLESS,
    scene_threshold:  float = 27.0,
    min_scene_length: int   = 15,
    dry_run:          bool  = False,
    standalone:       bool  = False,
) -> ChunkingResult:
    """Split video into scene-based chunks using PySceneDetect.

    Calls ``recover_chunking`` first to determine what work remains.
    Scene boundaries are loaded from ``chunking.yaml`` when present, skipping
    re-detection.  Already-split chunks (``COMPLETE`` in recovery) are skipped.

    When *standalone* is ``True`` (direct CLI invocation, not via the
    auto-pipeline), ``discover_inputs`` is called first to verify that the
    extraction phase has produced its outputs.  The auto-pipeline orchestrator
    passes outputs directly and sets *standalone* to ``False`` to bypass this
    check (Req 11.2).

    Args:
        video_file:       Path to source video file.
        output_dir:       Directory for chunk output.
        state_manager:    State manager for persisting scene boundaries.
        job:              Current job state.
        chunking_mode:    LOSSLESS (FFV1 all-intra, default) or REMUX (stream-copy).
        scene_threshold:  Scene detection threshold (default 27.0).
        min_scene_length: Minimum frames per scene (default 15).
        dry_run:          If True, only report status without performing work.
        standalone:       If True, run inputs discovery before proceeding (Req 11.1).

    Returns:
        ``ChunkingResult`` with chunk information.
    """
    logger.info("Chunking phase: %s", video_file.name)

    # Inputs discovery — only when invoked standalone (not via auto-pipeline)
    if standalone:
        discovery = discover_inputs("chunking", state_manager.work_dir, job)
        if not discovery.ok:
            return ChunkingResult(
                outcome=PhaseOutcome.FAILED,
                chunks=[], total_frames=0,
                error=discovery.error,
            )

    if not video_file.exists():
        return ChunkingResult(
            outcome=PhaseOutcome.FAILED,
            chunks=[], total_frames=0,
            error=f"Video file not found: {video_file}",
        )

    if not is_ffmpeg_available():
        return ChunkingResult(
            outcome=PhaseOutcome.FAILED,
            chunks=[], total_frames=0,
            error="ffmpeg not found in PATH",
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    # Recovery: scan filesystem, classify chunks, load scene boundaries (Req 3.3, 5.1–5.7)
    recovery = recover_chunking(
        work_dir=state_manager.work_dir,
        job=job,
        state_manager=state_manager,
    )

    # Fast-path: all chunks COMPLETE and scenes loaded — nothing to do
    if recovery.scenes and not recovery.pending and recovery.chunks:
        chunks = [
            rec.metadata
            for rec in recovery.chunks.values()
            if rec.metadata is not None
        ]
        chunks.sort(key=lambda c: c.chunk_id)
        total_frames = sum(c._frame_count or 0 for c in chunks)
        logger.info(
            "Reusing %d chunk(s) from recovery (%d frames) — skipping scene detection",
            len(chunks), total_frames,
        )
        return ChunkingResult(
            outcome=PhaseOutcome.REUSED,
            chunks=chunks,
            total_frames=total_frames,
        )

    if dry_run:
        logger.info("[DRY-RUN] Would perform scene detection and chunking")
        logger.info("[DRY-RUN]   Scene threshold: %s", scene_threshold)
        logger.info("[DRY-RUN]   Min scene length: %d frames", min_scene_length)
        logger.info("[DRY-RUN]   Chunking mode: %s", chunking_mode.value)
        return ChunkingResult(
            outcome=PhaseOutcome.DRY_RUN,
            chunks=[], total_frames=0,
        )

    return _chunk_video_impl(
        video_file, output_dir, chunking_mode,
        scene_threshold, min_scene_length, state_manager, job, recovery,
    )


def _chunk_video_impl(
    video_file:       Path,
    output_dir:       Path,
    chunking_mode:    ChunkingMode,
    scene_threshold:  float,
    min_scene_length: int,
    state_manager:    JobStateManager,
    job:              JobState,
    recovery:         ChunkingRecovery,
) -> ChunkingResult:
    """Two-phase chunking with recovery-aware state persistence."""
    video_meta = VideoMetadata(path=video_file)

    # Use recovered scene boundaries or run detection (Req 5.3, 5.4)
    if recovery.scenes:
        boundaries = recovery.scenes
        logger.info(
            "Scene boundaries already in chunking.yaml (%d) -- skipping detection.",
            len(boundaries),
        )
    else:
        try:
            boundaries = detect_scenes(
                video_meta=video_meta,
                state_manager=state_manager,
                scene_threshold=scene_threshold,
                min_scene_length=min_scene_length,
            )
        except Exception as exc:
            logger.error("Scene detection failed: %s", exc, exc_info=True)
            return ChunkingResult(
                outcome=PhaseOutcome.FAILED,
                chunks=[], total_frames=0,
                error=str(exc),
            )

    try:
        chunk_metas = split_chunks(
            video_meta=video_meta,
            output_dir=output_dir,
            boundaries=boundaries,
            state_manager=state_manager,
            recovery=recovery,
            chunking_mode=chunking_mode,
        )
    except Exception as exc:
        logger.error("Chunk splitting failed: %s", exc, exc_info=True)
        return ChunkingResult(
            outcome=PhaseOutcome.FAILED,
            chunks=[], total_frames=0,
            error=str(exc),
        )

    if not chunk_metas:
        return ChunkingResult(
            outcome=PhaseOutcome.FAILED,
            chunks=[], total_frames=0,
            error="No valid chunks created.",
        )

    total_frames = sum(c._frame_count or 0 for c in chunk_metas)
    logger.info("Chunking complete: %d chunk(s), %d total frames.", len(chunk_metas), total_frames)
    return ChunkingResult(
        outcome=PhaseOutcome.COMPLETED,
        chunks=chunk_metas,
        total_frames=total_frames,
    )
