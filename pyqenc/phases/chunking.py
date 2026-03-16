"""
Chunking phase for the quality-based encoding pipeline.

This module handles splitting video into scene-based chunks using PySceneDetect.
The phase is split into two independently resumable sub-phases:

1. ``detect_scenes_to_state`` -- runs scene detection and persists boundaries.
2. ``split_chunks_from_state`` -- splits the video at persisted boundaries.

The ``chunk_video`` entry point orchestrates both sub-phases and skips any
work that has already been persisted in ``PipelineState``.

Two chunking modes are supported (see ``ChunkingMode``):

* **LOSSLESS** (default): each chunk is re-encoded to FFV1 all-intra (``-g 1``)
  so every frame is an I-frame and splits are frame-perfect.
* **REMUX**: stream-copy (``-c copy``); faster and smaller chunks but boundaries
  snap to the nearest I-frame before the scene timestamp.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from math import floor
from pathlib import Path
from typing import TYPE_CHECKING

from alive_progress import alive_bar, config_handler
from scenedetect import ContentDetector, detect
from scenedetect.video_splitter import is_ffmpeg_available

from pyqenc.constants import (
    PADDING_FRAME_NUMBER,
    PROGRESS_CHUNK_UNIT,
    RANGE_SEPARATOR,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import (
    ChunkingMode,
    ChunkVideoMetadata,
    PhaseMetadata,
    PhaseStatus,
    PhaseUpdate,
    SceneBoundary,
    VideoMetadata,
)
from pyqenc.utils.ffmpeg import FrameCountError, get_frame_count

if TYPE_CHECKING:
    from pyqenc.progress import ProgressTracker

config_handler.set_global(enrich_print=False) # type: ignore
logger = logging.getLogger(__name__)



_CHUNK_NAME_PATTERN = re.compile(r"^chunk\.(\d+)-(\d+)$")

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


def _chunk_name(start_frame: int, end_frame: int) -> str:
    """Return the canonical chunk file stem for a frame range."""
    return f"{start_frame:0{PADDING_FRAME_NUMBER}d}{RANGE_SEPARATOR}{end_frame:0{PADDING_FRAME_NUMBER}d}"

def _chunk_name_duration(start_ts: float, end_ts: float) -> str:
    """Return the canonical chunk file stem for timestamps range. Use either this or _chunk_name everywhere for consistency, but not both."""
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
class ChunkInfo:
    chunk_id:    str
    file_path:   Path
    start_frame: int
    end_frame:   int
    frame_count: int
    duration:    float


@dataclass
class ChunkingResult:
    chunks:       list[ChunkInfo]
    total_frames: int
    reused:       bool
    needs_work:   bool
    success:      bool
    error:        str | None = None


def detect_scenes_to_state(
    video_meta:       VideoMetadata,
    tracker:          "ProgressTracker",
    scene_threshold:  float = 27.0,
    min_scene_length: int   = 15,
) -> list[SceneBoundary]:
    """Detect scene boundaries and persist them into pipeline state.

    Runs PySceneDetect ContentDetector on video_meta.path and stores
    the resulting boundary list in
    PipelineState.phases["chunking"].metadata.scene_boundaries via tracker.
    If zero scenes are detected the entire video is treated as a single scene
    (one boundary at frame 0 / t=0.0) and a warning is logged.

    Args:
        video_meta:       Metadata for the source video file.
        tracker:          Progress tracker used to persist boundaries.
        scene_threshold:  PySceneDetect content-change threshold (default 27.0).
        min_scene_length: Minimum frames per scene (default 15).

    Returns:
        List of SceneBoundary objects.
    """
    logger.info("Scene detection: analyzing %s", video_meta.path.name)

    with alive_bar(title="Scene detection", monitor=False, stats=False) as bar:
        scene_list = detect(
            str(video_meta.path),
            ContentDetector(threshold=scene_threshold, min_scene_len=min_scene_length),
        )
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

    tracker.update_phase(PhaseUpdate(
        phase="chunking",
        status=PhaseStatus.IN_PROGRESS,
        metadata=PhaseMetadata(scene_boundaries=boundaries),
    ))

    return boundaries


def split_chunks_from_state(
    video_meta:    VideoMetadata,
    output_dir:    Path,
    tracker:       "ProgressTracker",
    chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS,
) -> list[ChunkVideoMetadata]:
    """Split the source video into chunks using persisted scene boundaries.

    Reads scene_boundaries from PipelineState.phases["chunking"].metadata,
    derives frame-range chunk names, skips chunks already recorded in
    PipelineState.chunks_metadata, and calls ffmpeg to split each segment.

    In LOSSLESS mode each chunk is re-encoded to FFV1 all-intra (``-g 1``)
    for frame-perfect boundaries.  In REMUX mode stream-copy is used.

    Each successfully split chunk is immediately recorded via
    tracker.update_chunk_metadata. If a chunk file is missing or empty
    after splitting, a critical error is logged and the chunk is skipped.

    Args:
        video_meta:    Metadata for the source video file.
        output_dir:    Directory where chunk files will be written.
        tracker:       Progress tracker that holds scene boundaries and receives
                       chunk metadata updates.
        chunking_mode: LOSSLESS (FFV1 all-intra, default) or REMUX (stream-copy).

    Returns:
        List of ChunkVideoMetadata for every chunk that was successfully split.
    """
    state = tracker._state
    if state is None:
        raise RuntimeError(
            "ProgressTracker state not initialized before split_chunks_from_state."
        )

    chunking_phase = state.phases.get("chunking")
    boundaries: list[SceneBoundary] = []
    if chunking_phase and chunking_phase.metadata:
        boundaries = chunking_phase.metadata.scene_boundaries

    if not boundaries:
        raise RuntimeError(
            "No scene boundaries found in pipeline state. "
            "Run detect_scenes_to_state first."
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

    total_frames = video_meta.frame_count

    result_chunks: list[ChunkVideoMetadata] = []
    already_done = set(state.chunks_metadata.keys())

    with alive_bar(len(boundaries), title="Chunking", unit=PROGRESS_CHUNK_UNIT) as bar:
        for idx, start_boundary in enumerate(boundaries):
            if idx + 1 < len(boundaries):
                end_frame  = boundaries[idx + 1].frame - 1
                end_ts     = boundaries[idx + 1].timestamp_seconds  # exclusive end = next scene start
            else:
                end_frame  = (total_frames - 1) if total_frames else start_boundary.frame
                end_ts     = video_meta.duration_seconds or 0.0

            start_ts    = start_boundary.timestamp_seconds
            start_frame = start_boundary.frame

            stem       = _chunk_name(start_frame, end_frame)
            stem       = _chunk_name_duration(start_ts, end_ts) # Use timestamp-based naming for more consistency on variable framerate
            chunk_file = output_dir / f"{stem}.mkv"

            if stem in already_done:
                existing = state.chunks_metadata[stem]
                result_chunks.append(existing)
                logger.debug("Skipping already-split chunk: %s", stem)
                bar.text = stem
                bar()
                continue

            if not chunk_file.exists():
                duration = end_ts - start_ts
                cmd: list[str] = [
                    "ffmpeg", "-y",
                    "-ss", str(start_ts),
                    "-i", str(video_meta.path),
                    "-t", str(duration),
                    *video_args,
                    "-an", str(chunk_file),
                ]
                logger.debug("Splitting chunk %s (%.3fs): %s", stem, duration, " ".join(cmd))
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if proc.returncode != 0:
                    logger.critical(
                        "ffmpeg split failed for chunk %s (exit %d): %s",
                        stem, proc.returncode, proc.stderr[-400:],
                    )
                    bar.text = f"✗ {stem}"
                    bar()
                    continue

            if not chunk_file.exists() or chunk_file.stat().st_size == 0:
                logger.critical("Chunk file missing or empty after split: %s", chunk_file.name)
                bar.text = f"✗ {stem}"
                bar()
                continue

            try:
                chunk_frames = get_frame_count(chunk_file, tracker, is_source=False)
            except FrameCountError as exc:
                logger.warning(
                    "Failed to get frame count for %s: %s -- using estimate.", stem, exc
                )
                chunk_frames = end_frame - start_frame + 1

            chunk_meta = ChunkVideoMetadata(
                path=chunk_file,
                chunk_id=stem,
                start_frame=start_frame,
            )
            chunk_meta._frame_count = chunk_frames

            tracker.update_chunk_metadata(chunk_meta)
            result_chunks.append(chunk_meta)
            logger.debug("Chunk %s: %d frames", stem, chunk_frames)
            bar.text = stem
            bar()

    logger.info("Chunk splitting complete: %d chunk(s) ready.", len(result_chunks))
    return result_chunks

def chunk_video(
    video_file:       Path,
    output_dir:       Path,
    chunking_mode:    ChunkingMode = ChunkingMode.LOSSLESS,
    scene_threshold:  float = 27.0,
    min_scene_length: int   = 15,
    force:            bool  = False,
    dry_run:          bool  = False,
    tracker:          "ProgressTracker | None" = None,
) -> ChunkingResult:
    """Split video into scene-based chunks using PySceneDetect.

    Orchestrates detect_scenes_to_state and split_chunks_from_state as two
    independently resumable sub-phases. Scene boundaries are persisted after
    detection so a restart skips re-detection. Already-split chunks are
    skipped during splitting.

    When tracker is None the function falls back to a stateless path.

    Args:
        video_file:       Path to source video file.
        output_dir:       Directory for chunk output.
        chunking_mode:    LOSSLESS (FFV1 all-intra, default) or REMUX (stream-copy).
        scene_threshold:  Scene detection threshold (default 27.0).
        min_scene_length: Minimum frames per scene (default 15).
        force:            If True, ignore existing chunks and re-chunk.
        dry_run:          If True, only report status without performing work.
        tracker:          Optional progress tracker for state persistence.

    Returns:
        ChunkingResult with chunk information.
    """
    logger.info("Chunking phase: %s", video_file.name)

    if not video_file.exists():
        return ChunkingResult(
            success=False, reused=False, needs_work=False,
            chunks=[], total_frames=0,
            error=f"Video file not found: {video_file}",
        )

    if not is_ffmpeg_available():
        return ChunkingResult(
            success=False, reused=False, needs_work=False,
            chunks=[], total_frames=0,
            error="ffmpeg not found in PATH",
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    if not force and tracker is None:
        existing_chunks = sorted(output_dir.glob("chunk.*.mkv"))
        if existing_chunks:
            chunks, valid = _load_existing_chunks(existing_chunks, tracker)
            if valid:
                total_frames = sum(c.frame_count for c in chunks)
                logger.info("Reusing %d existing chunks (%d frames)", len(chunks), total_frames)
                return ChunkingResult(
                    success=True, reused=True, needs_work=False,
                    chunks=chunks, total_frames=total_frames,
                )

    # Fast-path: tracker has persisted boundaries AND all chunk files exist on disk.
    # Skip both scene detection and frame-counting — just load from state.
    if not force and tracker is not None:
        state = tracker._state
        if state is not None:
            chunking_phase = state.phases.get("chunking")
            has_boundaries = (
                chunking_phase is not None
                and chunking_phase.metadata is not None
                and bool(chunking_phase.metadata.scene_boundaries)
            )
            if has_boundaries and state.chunks_metadata:
                existing_chunk_files = sorted(output_dir.glob("chunk.*.mkv"))
                existing_stems = {f.stem for f in existing_chunk_files}
                persisted_stems = set(state.chunks_metadata.keys())
                if persisted_stems and persisted_stems == existing_stems:
                    chunks: list[ChunkInfo] = []
                    total_frames = 0
                    for meta in state.chunks_metadata.values():
                        m = _CHUNK_NAME_PATTERN.match(meta.chunk_id)
                        end_frame = int(m.group(2)) if m else 0
                        fc = meta._frame_count or 0
                        chunks.append(ChunkInfo(
                            chunk_id=meta.chunk_id,
                            file_path=meta.path,
                            start_frame=meta.start_frame,
                            end_frame=end_frame,
                            frame_count=fc,
                            duration=meta._duration_seconds or 0.0,
                        ))
                        total_frames += fc
                    chunks.sort(key=lambda c: c.start_frame)
                    logger.info(
                        "Reusing %d chunks from state (%d frames) — skipping scene detection",
                        len(chunks), total_frames,
                    )
                    return ChunkingResult(
                        success=True, reused=True, needs_work=False,
                        chunks=chunks, total_frames=total_frames,
                    )

    if dry_run:
        logger.info("[DRY-RUN] Would perform scene detection and chunking")
        logger.info("[DRY-RUN]   Scene threshold: %s", scene_threshold)
        logger.info("[DRY-RUN]   Min scene length: %d frames", min_scene_length)
        logger.info("[DRY-RUN]   Chunking mode: %s", chunking_mode.value)
        return ChunkingResult(
            success=True, reused=False, needs_work=True,
            chunks=[], total_frames=0,
        )

    if tracker is not None:
        return _chunk_video_tracked(
            video_file, output_dir, chunking_mode,
            scene_threshold, min_scene_length, tracker,
        )

    return _chunk_video_stateless(
        video_file, output_dir, chunking_mode,
        scene_threshold, min_scene_length,
    )


def _load_existing_chunks(
    existing_chunks: list[Path],
    tracker:         "ProgressTracker | None",
) -> tuple[list[ChunkInfo], bool]:
    """Try to load ChunkInfo from existing chunk files on disk."""
    chunks: list[ChunkInfo] = []
    for chunk_file in existing_chunks:
        m = _CHUNK_NAME_PATTERN.match(chunk_file.stem)
        if m is None:
            logger.warning("Skipping unrecognized chunk file: %s", chunk_file.name)
            continue
        start_frame = int(m.group(1))
        end_frame   = int(m.group(2))
        try:
            frame_count = get_frame_count(chunk_file, tracker, is_source=False)
            chunks.append(ChunkInfo(
                chunk_id=chunk_file.stem,
                file_path=chunk_file,
                start_frame=start_frame,
                end_frame=end_frame,
                frame_count=frame_count,
                duration=0.0,
            ))
        except FrameCountError as exc:
            logger.warning("Failed to validate chunk %s: %s", chunk_file.name, exc)
            return chunks, False

    return chunks, len(chunks) == len(existing_chunks)


def _chunk_video_tracked(
    video_file:       Path,
    output_dir:       Path,
    chunking_mode:    ChunkingMode,
    scene_threshold:  float,
    min_scene_length: int,
    tracker:          "ProgressTracker",
) -> ChunkingResult:
    """Two-phase chunking with full state persistence via tracker."""
    state = tracker._state
    if state is None:
        return ChunkingResult(
            success=False, reused=False, needs_work=False,
            chunks=[], total_frames=0,
            error="ProgressTracker state not initialized.",
        )

    video_meta = VideoMetadata(path=video_file)

    chunking_phase = state.phases.get("chunking")

    if (chunking_phase is not None # Check if there are boundaries in the Progress tracker
        and chunking_phase.metadata is not None
        and bool(chunking_phase.metadata.scene_boundaries)):
            boundary_count = len(chunking_phase.metadata.scene_boundaries)
            logger.info(
                "Scene boundaries already in state (%d) -- skipping detection.",
                boundary_count,
            )
    else:
        try:
            detect_scenes_to_state(
                video_meta=video_meta,
                tracker=tracker,
                scene_threshold=scene_threshold,
                min_scene_length=min_scene_length,
            )
        except Exception as exc:
            logger.error("Scene detection failed: %s", exc, exc_info=True)
            return ChunkingResult(
                success=False, reused=False, needs_work=False,
                chunks=[], total_frames=0,
                error=str(exc),
            )

    try:
        chunk_metas = split_chunks_from_state(
            video_meta=video_meta,
            output_dir=output_dir,
            tracker=tracker,
            chunking_mode=chunking_mode,
        )
    except Exception as exc:
        logger.error("Chunk splitting failed: %s", exc, exc_info=True)
        return ChunkingResult(
            success=False, reused=False, needs_work=False,
            chunks=[], total_frames=0,
            error=str(exc),
        )

    if not chunk_metas:
        return ChunkingResult(
            success=False, reused=False, needs_work=False,
            chunks=[], total_frames=0,
            error="No valid chunks created.",
        )

    chunks: list[ChunkInfo] = []
    total_frames = 0
    for meta in chunk_metas:
        m = _CHUNK_NAME_PATTERN.match(meta.chunk_id)
        end_frame = int(m.group(2)) if m else 0
        fc = meta._frame_count or 0
        chunks.append(ChunkInfo(
            chunk_id=meta.chunk_id,
            file_path=meta.path,
            start_frame=meta.start_frame,
            end_frame=end_frame,
            frame_count=fc,
            duration=meta._duration_seconds or 0.0,
        ))
        total_frames += fc

    reused = len(state.chunks_metadata) > 0

    logger.info("Chunking complete: %d chunk(s), %d total frames.", len(chunks), total_frames)
    return ChunkingResult(
        success=True, reused=reused, needs_work=False,
        chunks=chunks, total_frames=total_frames,
    )


def _chunk_video_stateless(
    video_file:       Path,
    output_dir:       Path,
    chunking_mode:    ChunkingMode,
    scene_threshold:  float,
    min_scene_length: int,
) -> ChunkingResult:
    """Stateless chunking path used when no tracker is provided."""
    try:
        logger.info("Detecting scene changes (stateless)...")
        with alive_bar(title="Scene detection", monitor=False, stats=False) as bar:
            scene_list = detect(
                str(video_file),
                ContentDetector(threshold=scene_threshold, min_scene_len=min_scene_length),
            )
            bar()
        logger.info("Detected %d scene change(s).", len(scene_list))

        # Resolve video args once before the loop.
        if chunking_mode == ChunkingMode.LOSSLESS:
            video_meta_probe = VideoMetadata(path=video_file)
            pix_fmt = video_meta_probe.pix_fmt
            if pix_fmt is None:
                logger.warning(
                    "Could not determine pixel format for %s; falling back to yuv420p.",
                    video_file.name,
                )
                pix_fmt = "yuv420p"
            video_args: list[str] = [*FFV1_VIDEO_ARGS, "-pix_fmt", pix_fmt]
            logger.info(
                "Chunking mode: lossless FFV1 (pix_fmt=%s) — frame-perfect splits.", pix_fmt
            )
        else:
            video_args = ["-c", "copy"]
            logger.info("Chunking mode: remux (stream-copy) — I-frame-snapped splits.")

        chunks: list[ChunkInfo] = []
        total_frames = 0

        with alive_bar(len(scene_list), title="Chunking", unit=PROGRESS_CHUNK_UNIT) as bar:
            for scene_start, scene_end in scene_list:
                start_ts    = scene_start.get_seconds()
                end_ts      = scene_end.get_seconds()   # exclusive end = next scene start
                start_frm   = scene_start.get_frames()
                end_frm     = scene_end.get_frames() - 1

                stem       = _chunk_name(start_frm, end_frm)
                stem       = _chunk_name_duration(start_ts, end_ts) # Use timestamp-based naming for more consistency on variable framerate
                chunk_file = output_dir / f"{stem}.mkv"

                if not chunk_file.exists():
                    duration = end_ts - start_ts
                    cmd: list[str] = [
                        "ffmpeg", "-y",
                        "-ss", str(start_ts),
                        "-i", str(video_file),
                        "-t", str(duration),
                        *video_args,
                        "-an", str(chunk_file),
                    ]
                    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                    if proc.returncode != 0:
                        logger.error("ffmpeg split failed for %s: %s", stem, proc.stderr[-300:])
                        bar.text = f"✗ {stem}"
                        bar()
                        continue

                if not chunk_file.exists() or chunk_file.stat().st_size == 0:
                    logger.critical("Chunk file missing or empty: %s", chunk_file.name)
                    bar.text = f"✗ {stem}"
                    bar()
                    continue

                try:
                    chunk_frames = get_frame_count(chunk_file, None, is_source=False)
                except FrameCountError:
                    chunk_frames = end_frm - start_frm + 1

                chunks.append(ChunkInfo(
                    chunk_id=stem,
                    file_path=chunk_file,
                    start_frame=start_frm,
                    end_frame=end_frm,
                    frame_count=chunk_frames,
                    duration=end_ts - start_ts,
                ))
                total_frames += chunk_frames
                bar.text = stem  # type: ignore[union-attr]
                bar()  # type: ignore[operator]

        if not chunks:
            return ChunkingResult(
                success=False, reused=False, needs_work=False,
                chunks=[], total_frames=0,
                error="No valid chunks created.",
            )

        logger.info("Created %d chunk(s) with %d total frames.", len(chunks), total_frames)
        return ChunkingResult(
            success=True, reused=False, needs_work=False,
            chunks=chunks, total_frames=total_frames,
        )

    except Exception as exc:
        logger.error("Chunking failed: %s", exc, exc_info=True)
        return ChunkingResult(
            success=False, reused=False, needs_work=False,
            chunks=[], total_frames=0,
            error=str(exc),
        )
