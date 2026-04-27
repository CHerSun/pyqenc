"""
Chunking phase for the quality-based encoding pipeline.

This module handles splitting video into scene-based chunks using PySceneDetect.
Two independently resumable steps, orchestrated by ``ChunkingPhase``:

1. ``detect_scenes`` -- runs scene detection; caller persists boundaries via ``ChunkingPhase.params``.
2. ``split_chunks``  -- splits the video at persisted boundaries, writing chunk sidecars.

Two chunking modes are supported (see ``ChunkingMode``):

* **LOSSLESS** (default): each chunk is re-encoded to FFV1 all-intra (``-g 1``)
  so every frame is an I-frame and splits are frame-perfect.
* **REMUX**: stream-copy (``-c copy``); faster and smaller chunks but boundaries
  snap to the nearest I-frame before the scene timestamp.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyqenc.metrics import MetricsCollector

from alive_progress import alive_bar, config_handler
from scenedetect import ContentDetector, detect

from pyqenc.constants import (
    CHUNK_NAME_PATTERN,
    RANGE_SEPARATOR,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
)
from pyqenc.models import (
    ChunkingMode,
    ChunkMetadata,
    PhaseOutcome,
    SceneBoundary,
    VideoMetadata,
)
from pyqenc.state import (
    ArtifactState,
    ChunkingParams,
    ChunkSidecar,
)
from pyqenc.utils.alive import AdvanceState, ProgressBar
from pyqenc.utils.ffmpeg_runner import run_ffmpeg
from pyqenc.utils.yaml_utils import write_yaml_atomic

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


def _expand_scenes(
    boundaries:      list[SceneBoundary],
    source_duration: float | None,
    output_dir:      Path,
) -> list[tuple[float, float, str, Path]]:
    """Expand scene boundaries into ``(start_ts, end_ts, stem, chunk_file)`` tuples.

    For all but the last boundary ``end_ts`` is the next boundary's timestamp.
    For the last boundary ``end_ts`` is ``source_duration``.  When
    ``source_duration`` is ``None`` the last entry is omitted — the caller
    cannot resolve it yet and must handle it at execution time.

    Args:
        boundaries:      Scene boundaries in order.
        source_duration: Total video duration in seconds, or ``None`` if unknown.
        output_dir:      Directory where chunk files live (used to build paths).

    Returns:
        One tuple per resolvable boundary.
    """
    result: list[tuple[float, float, str, Path]] = []
    for i, boundary in enumerate(boundaries):
        start_ts = boundary.timestamp_seconds
        end_ts: float | None = (
            boundaries[i + 1].timestamp_seconds if i + 1 < len(boundaries) else source_duration
        )
        if end_ts is None:
            continue
        stem = _chunk_name_duration(start_ts, end_ts)
        result.append((start_ts, end_ts, stem, output_dir / f"{stem}.mkv"))
    return result


def detect_scenes(
    video_meta:       VideoMetadata,
    scene_threshold:  float = 27.0,
    min_scene_length: int   = 15,
) -> list[SceneBoundary]:
    """Detect scene boundaries in *video_meta* using PySceneDetect.

    Pure computation — does not persist anything.  The caller is responsible
    for updating ``ChunkingPhase.params.scenes`` and saving ``chunking.yaml``.

    If zero scenes are detected the entire video is treated as a single scene
    (one boundary at frame 0 / t=0.0) and a warning is logged.

    Args:
        video_meta:       Metadata for the source video file.
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

    return boundaries


def split_chunks(
    video_meta:    VideoMetadata,
    output_dir:    Path,
    boundaries:    list[SceneBoundary],
    recovery:      ChunkingRecovery,
    chunking_mode: ChunkingMode = ChunkingMode.LOSSLESS,
    *,
    collector:     "MetricsCollector",
) -> list[ChunkMetadata]:
    """Split the source video into chunks using scene boundaries.

    Skips chunks already present on disk (as determined by *recovery*).
    Writes a ``<chunk_stem>.yaml`` sidecar after each successful split (Req 5.5).
    Calls ``collector.step(TimeKey.CHUNKING_SPLIT)`` after each successful split
    to trigger incremental metrics flushes (Req 6.5, 2.2a).

    Args:
        video_meta:    Metadata for the source video file.
        output_dir:    Directory where chunk files will be written.
        boundaries:    Scene boundaries to split at.
        recovery:      Recovery result from ``recover_chunking``; chunks already
                       ``COMPLETE`` are skipped.
        chunking_mode: LOSSLESS (FFV1 all-intra, default) or REMUX (stream-copy).
        collector:     Metrics collector for step triggering after each split.

    Returns:
        List of ``ChunkMetadata`` for every chunk that was successfully split or reused.
    """
    from pyqenc.metrics import MetricKey
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
    with ProgressBar(total_seconds, title="Chunking", total_count=len(boundaries)) as advance:
        for start_ts, end_ts, stem, chunk_file in _expand_scenes(
            boundaries, video_meta.duration_seconds, output_dir
        ):

            # Skip chunks already COMPLETE from recovery (Req 5.2)
            if stem in complete_from_recovery:
                result_chunks.append(complete_from_recovery[stem])
                logger.debug("Skipping already-complete chunk: %s", stem)
                advance(end_ts - start_ts, AdvanceState.SKIPPED)
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
            collector.step(MetricKey.CHUNKING)
            advance(end_ts - start_ts)

        advance(0, AdvanceState.COMPLETE)

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


# ---------------------------------------------------------------------------
# ChunkingPhase — Phase object (task 6)
# ---------------------------------------------------------------------------

import shutil
from dataclasses import dataclass as _dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyqenc.metrics import MetricsCollector
    from pyqenc.models import PipelineConfig
    from pyqenc.phase import Phase, PhaseResult
    from pyqenc.phases.extraction import ExtractionPhase
    from pyqenc.phases.job import JobPhase, JobPhaseResult

from pyqenc.constants import (
    CHUNKS_DIR,
    EXTRACTED_DIR,
    TEMP_SUFFIX,
    THICK_LINE,
    THIN_LINE,
)
from pyqenc.models import PhaseOutcome
from pyqenc.phase import Artifact, Phase, PhaseResult
from pyqenc.state import ArtifactState, ChunkingParams
from pyqenc.utils.log_format import emit_phase_banner, log_recovery_line

_CHUNKING_YAML     = "chunking.yaml"


class ChunkArtifact(Artifact):
    """Chunking artifact for a single video chunk.

    ``metadata`` is lazy-loaded from the sidecar YAML on first access.
    The sidecar path is derived from ``path`` (same stem, ``.yaml`` suffix).
    Returns ``None`` if the sidecar is absent or cannot be parsed.
    """

    def __init__(self, path: Path, state: ArtifactState) -> None:
        super().__init__(path=path, state=state)
        self._metadata:        ChunkMetadata | None = None
        self._metadata_loaded: bool                 = False

    @property
    def metadata(self) -> ChunkMetadata | None:
        """Chunk metadata; lazy-loaded from sidecar YAML on first access."""
        if self._metadata_loaded:
            return self._metadata
        self._metadata_loaded = True
        sidecar = self.path.with_suffix(".yaml")
        try:
            import yaml as _yaml
            from pyqenc.state import ChunkSidecar as _ChunkSidecar
            with sidecar.open("r", encoding="utf-8") as fh:
                data = _yaml.safe_load(fh)
            self._metadata = _ChunkSidecar.from_yaml_dict(
                data, chunk_id=self.path.stem, path=self.path
            ).chunk
        except Exception:
            pass
        return self._metadata


@_dataclass
class ChunkingPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying chunking-specific payload.

    Attributes:
        chunks: List of chunk metadata for all ``COMPLETE`` artifacts.
    """

    chunks: list[ChunkMetadata] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.chunks is None:
            self.chunks = []


class ChunkingPhase:
    """Phase object for scene-based video chunking.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the chunking phase.  Wraps the existing ``detect_scenes`` and
    ``split_chunks`` helpers.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "chunking"

    def __init__(
        self,
        config:    "PipelineConfig",
        phases:    "dict[type[Phase], Phase] | None" = None,
        *,
        collector: "MetricsCollector",
    ) -> None:
        from typing import cast

        from pyqenc.phases.extraction import ExtractionPhase as _ExtractionPhase
        from pyqenc.phases.job import JobPhase as _JobPhase

        self._config    = config
        self._collector: "MetricsCollector" = collector
        self._job:        "_JobPhase | None"        = cast(_JobPhase,        phases[_JobPhase])        if phases else None
        self._extraction: "_ExtractionPhase | None" = cast(_ExtractionPhase, phases[_ExtractionPhase]) if phases else None
        self.params       = ChunkingParams(chunking_mode=config.chunking_mode.value, scenes=[])
        self.result:      "ChunkingPhaseResult | None" = None
        self.dependencies: "list[Phase]"            = [d for d in [self._job, self._extraction] if d is not None]
        # Set by _recover() when a chunking-mode mismatch is detected
        self._mode_mismatch_error:    str  = ""
        self._mode_changed_force_wipe: bool = False

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> "ChunkingPhaseResult":
        """Classify existing chunk artifacts without executing any work.

        Returns:
            ``ChunkingPhaseResult`` with all artifacts classified.
        """
        if self.result is not None:
            return self.result

        dep_result = self._ensure_dependencies(execute=False)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        artifacts = self._recover(force_wipe=force_wipe, execute=False)

        if self._mode_mismatch_error:
            self.result = _failed(self._mode_mismatch_error)
            return self.result

        chunks    = [a.metadata for a in artifacts if a.state == ArtifactState.COMPLETE and a.metadata is not None]
        outcome   = self._outcome_from_artifacts(artifacts, did_work=False)

        self.result = ChunkingPhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = _recovery_message(artifacts),
            chunks    = chunks,
        )
        return self.result

    def run(self, dry_run: bool = False) -> "ChunkingPhaseResult":
        """Recover, detect scenes if needed, split pending chunks, cache result.

        Sequence:
        1. Emit phase banner.
        2. Ensure dependencies have results (scan if needed).
        3. Run ``_recover()`` — handles ``force_wipe``.
        4. Log recovery result line.
        5. In dry-run mode: return ``DRY_RUN`` if any artifacts are pending.
        6. Detect scenes if not cached; split pending chunks.
        7. Log completion summary.

        Args:
            dry_run: When ``True``, report what would be done without writing files.

        Returns:
            ``ChunkingPhaseResult`` with all artifacts ``COMPLETE`` on success.
        """
        emit_phase_banner("CHUNKING", logger)

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        # Key parameters
        logger.info("Mode:  %s", self._config.chunking_mode.value)

        from pyqenc.metrics import MetricKey as _MetricKey
        with self._collector.time(_MetricKey.RECOVERY):
            artifacts = self._recover(force_wipe=force_wipe, execute=True)

        # Mode mismatch without --force: abort
        if self._mode_mismatch_error:
            self.result = _failed(self._mode_mismatch_error)
            return self.result

        # Mode changed with --force: chunks wiped, propagate force_wipe to downstream
        # via JobPhaseResult so all downstream phases see it through the standard path.
        if self._mode_changed_force_wipe:
            job_result.force_wipe = True  # type: ignore[union-attr]
            force_wipe = True

        complete_count = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
        pending_count  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
        log_recovery_line(logger, complete_count, pending_count)

        # Action plan — log scene count and pending work before starting
        recovered_scenes = getattr(self, "_recovered_scenes", [])
        if recovered_scenes:
            logger.info("Scenes:  %d (from chunking.yaml)", len(recovered_scenes))
        if pending_count > 0:
            logger.info("Pending: %d chunk(s) to split", pending_count)

        # Dry-run path
        if dry_run:
            outcome = PhaseOutcome.REUSED if (pending_count == 0 and complete_count > 0) else PhaseOutcome.DRY_RUN
            chunks  = [a.metadata for a in artifacts if a.state == ArtifactState.COMPLETE and a.metadata is not None]
            self.result = ChunkingPhaseResult(
                outcome   = outcome,
                artifacts = artifacts,
                message   = "dry-run",
                chunks    = chunks,
            )
            return self.result

        # Nothing to do — only skip if we actually have complete chunks
        if pending_count == 0 and complete_count > 0:
            chunks = [a.metadata for a in artifacts if a.state == ArtifactState.COMPLETE and a.metadata is not None]
            chunks.sort(key=lambda c: c.chunk_id)
            self.result = ChunkingPhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = artifacts,
                message   = "all chunks reused",
                chunks    = chunks,
            )
            return self.result

        # Execute chunking
        result = self._execute_chunking(artifacts)
        self.result = result
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, execute: bool) -> "ChunkingPhaseResult | None":
        """Scan/run dependencies if they have no cached result; fail fast if incomplete.

        Args:
            execute: When ``True``, call ``dep.run()`` for deps without a cached result.

        Returns:
            A ``FAILED`` result if any dependency is not complete; ``None`` otherwise.
        """
        if self._job is None:
            return _failed("ChunkingPhase requires JobPhase")

        if self._job.result is None:
            if execute:
                self._job.run()
            else:
                self._job.scan()

        if not self._job.result.is_complete:  # type: ignore[union-attr]
            err = "JobPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        if self._extraction is None:
            return _failed("ChunkingPhase requires ExtractionPhase")

        if self._extraction.result is None:
            if execute:
                self._extraction.run()
            else:
                self._extraction.scan()

        if not self._extraction.result.is_complete:  # type: ignore[union-attr]
            err = "ExtractionPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        return None

    def _recover(self, force_wipe: bool, execute: bool) -> list[ChunkArtifact]:
        """Classify chunk artifacts and handle force-wipe.

        Steps:
        1. If ``force_wipe`` and execute: delete ``chunks/`` and ``chunking.yaml``.
        2. Clean up leftover ``.tmp`` files (execute mode only).
        3. Load scene boundaries from ``chunking.yaml``.
        4. Scan ``chunks/`` and classify each chunk.

        Args:
            force_wipe: When ``True``, wipe all chunk artifacts first.
            execute:    When ``True``, wipe and ``.tmp`` cleanup are performed.

        Returns:
            List of ``ChunkArtifact`` objects.
        """
        work_dir   = self._config.work_dir
        chunks_dir = work_dir / CHUNKS_DIR
        yaml_path  = work_dir / _CHUNKING_YAML

        # Step 1: force-wipe
        if force_wipe and execute:
            if chunks_dir.exists():
                shutil.rmtree(chunks_dir)
                logger.debug("force_wipe: deleted %s", chunks_dir)
            if yaml_path.exists():
                yaml_path.unlink()
                logger.debug("force_wipe: deleted %s", yaml_path)

        # Step 2: clean up .tmp files (execute mode only)
        if execute and chunks_dir.exists():
            for tmp in chunks_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        if not chunks_dir.exists():
            return []

        # Step 3: load scene boundaries from chunking.yaml
        chunking_params = ChunkingParams.load(yaml_path)
        scenes: list[SceneBoundary] = []
        if chunking_params is not None and chunking_params.scenes:
            scenes = chunking_params.scenes
            logger.debug(
                "Chunking recovery: loaded %d scene boundary(ies) from chunking.yaml",
                len(scenes),
            )

            # Mode mismatch check: if the persisted mode differs from the current
            # config, chunks are incompatible and cannot be reused.
            persisted_mode = chunking_params.chunking_mode
            current_mode   = self.params.chunking_mode
            if persisted_mode is not None and persisted_mode != current_mode:
                if self._config.force and execute:
                    logger.warning(
                        "Chunking mode changed (%s → %s) — --force: wiping chunks/ and downstream artifacts",
                        persisted_mode, current_mode,
                    )
                    if chunks_dir.exists():
                        shutil.rmtree(chunks_dir)
                        logger.debug("force_wipe: deleted %s", chunks_dir)
                    yaml_path.unlink(missing_ok=True)
                    logger.debug("force_wipe: deleted %s", yaml_path)
                    # Signal downstream phases to wipe their artifacts too
                    self._mode_changed_force_wipe = True
                    return []
                else:
                    err = (
                        f"Chunking mode changed since last run "
                        f"(persisted={persisted_mode!r}, current={current_mode!r}). "
                        "Existing chunks are incompatible. "
                        "Re-run with --force to delete stale chunks and continue."
                    )
                    logger.critical(err)
                    self._mode_mismatch_error = err
                    return []
        else:
            logger.debug("Chunking recovery: chunking.yaml absent or empty — scene detection needed")

        # Step 4: classify chunks.
        #
        # When scene boundaries are known (from chunking.yaml), the scene list is
        # the authoritative source of truth.  We iterate expected chunks derived
        # from the boundaries and classify each one:
        #   - file + sidecar present  → COMPLETE
        #   - file present, no sidecar → ARTIFACT_ONLY (pending)
        #   - file absent              → ABSENT (pending)
        # Files on disk that are NOT in the expected set are STALE and are ignored
        # (not added to artifacts, not counted as pending).
        #
        # When no scene boundaries are available yet, fall back to a plain disk
        # scan — scene detection will run during execution to determine boundaries.
        artifacts:   list[ChunkArtifact] = []
        pending_ids: list[str]           = []

        if scenes:
            # Get source duration from JobPhase result — it's already probed and cached there.
            job_result = self._job.result if self._job else None  # type: ignore[union-attr]
            job_state  = getattr(job_result, "job", None)
            source_duration: float | None = (
                job_state.source.duration_seconds if job_state is not None else None
            )

            for start_ts, end_ts, stem, chunk_file in _expand_scenes(
                scenes, source_duration, chunks_dir
            ):
                if chunk_file.exists():
                    if chunk_file.with_suffix(".yaml").exists():
                        artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.COMPLETE))
                        logger.debug("Chunk %s: COMPLETE", stem)
                    else:
                        artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.ARTIFACT_ONLY))
                        pending_ids.append(stem)
                        logger.debug("Chunk %s: ARTIFACT_ONLY (sidecar missing)", stem)
                else:
                    artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.ABSENT))
                    pending_ids.append(stem)
                    logger.debug("Chunk %s: ABSENT (missing from disk)", stem)
        else:
            # No scene boundaries yet — plain disk scan, scene detection will follow.
            for chunk_file in sorted(chunks_dir.glob("*.mkv")):
                stem = chunk_file.stem
                if not CHUNK_NAME_PATTERN.match(stem):
                    logger.debug("Skipping non-chunk file: %s", chunk_file.name)
                    continue
                if chunk_file.with_suffix(".yaml").exists():
                    artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.COMPLETE))
                    logger.debug("Chunk %s: COMPLETE", stem)
                else:
                    artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.ARTIFACT_ONLY))
                    pending_ids.append(stem)
                    logger.debug("Chunk %s: ARTIFACT_ONLY (sidecar missing)", stem)

        complete_count = len(artifacts) - len(pending_ids)
        logger.debug(
            "Chunking recovery: %d chunk(s) found — %d COMPLETE, %d pending",
            len(artifacts), complete_count, len(pending_ids),
        )

        # Store recovered scene boundaries for use in _execute_chunking
        self._recovered_scenes: list[SceneBoundary] = scenes

        return artifacts

    def _execute_chunking(self, artifacts: list[ChunkArtifact]) -> "ChunkingPhaseResult":
        """Detect scenes if needed and split pending chunks.

        Args:
            artifacts: Artifact list from ``_recover()``.

        Returns:
            ``ChunkingPhaseResult`` after chunking.
        """
        from pyqenc.metrics import MetricKey
        work_dir   = self._config.work_dir
        chunks_dir = work_dir / CHUNKS_DIR
        chunks_dir.mkdir(parents=True, exist_ok=True)

        # Resolve the video file from ExtractionPhase result
        video_file = self._resolve_video_file()
        if video_file is None:
            err = "No extracted video file available for chunking"
            logger.critical(err)
            return _failed(err)

        if not video_file.exists():
            err = f"Video file not found: {video_file}"
            logger.critical(err)
            return _failed(err)

        job_result = self._job.result  # type: ignore[union-attr]
        job_state  = getattr(job_result, "job", None)
        if job_state is None:
            from pyqenc.state import JobState as _JobState
            job_state = _JobState(source=VideoMetadata(path=self._config.source_video))
        video_meta = job_state.source

        # Use recovered scene boundaries or run detection
        boundaries = getattr(self, "_recovered_scenes", [])
        if boundaries:
            logger.info(
                "Scene boundaries already in chunking.yaml (%d) — skipping detection.",
                len(boundaries),
            )
        else:
            try:
                with self._collector.time(MetricKey.CHUNKING):
                    boundaries = detect_scenes(
                        video_meta       = video_meta,
                        scene_threshold  = 27.0,
                        min_scene_length = 15,
                    )
                self.params.scenes = boundaries
                self.params.save(work_dir / _CHUNKING_YAML)
            except Exception as exc:
                logger.error("Scene detection failed: %s", exc, exc_info=True)
                return _failed(str(exc))

        # Build recovery object for split_chunks (it needs to know which chunks are already COMPLETE)
        from pyqenc.phases.recovery import ChunkingRecovery, ChunkRecovery
        recovery_obj = ChunkingRecovery(
            scenes  = boundaries,
            chunks  = {
                a.metadata.chunk_id: ChunkRecovery(
                    chunk_id = a.metadata.chunk_id,
                    path     = a.path,
                    state    = a.state,
                    metadata = a.metadata,
                )
                for a in artifacts
                if a.state == ArtifactState.COMPLETE and a.metadata is not None
            },
            pending = [
                a.path.stem for a in artifacts
                if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY)
            ],
        )

        try:
            with self._collector.time(MetricKey.CHUNKING):
                chunk_metas = split_chunks(
                    video_meta    = video_meta,
                    output_dir    = chunks_dir,
                    boundaries    = boundaries,
                    recovery      = recovery_obj,
                    chunking_mode = self._config.chunking_mode,
                    collector     = self._collector,
                )
        except Exception as exc:
            logger.error("Chunk splitting failed: %s", exc, exc_info=True)
            return _failed(str(exc))

        if not chunk_metas:
            return _failed("No valid chunks created.")

        # Build final artifact list
        final_artifacts: list[ChunkArtifact] = []
        for cm in chunk_metas:
            final_artifacts.append(ChunkArtifact(
                path  = cm.path,
                state = ArtifactState.COMPLETE,
            ))

        total_frames = sum(c._frame_count or 0 for c in chunk_metas)
        logger.info(
            "%s Chunking complete: %d chunk(s), %d total frames.",
            "✔", len(chunk_metas), total_frames,
        )
        logger.info(THICK_LINE)

        return ChunkingPhaseResult(
            outcome   = PhaseOutcome.COMPLETED,
            artifacts = final_artifacts,
            message   = f"chunked into {len(chunk_metas)} chunk(s)",
            chunks    = chunk_metas,
        )

    def _resolve_video_file(self) -> Path | None:
        """Resolve the extracted video file from ExtractionPhase result."""
        if self._extraction is None or self._extraction.result is None:
            return None
        video_meta = getattr(self._extraction.result, "video", None)
        if video_meta is not None:
            return video_meta.path
        # Fallback: scan extracted/ for a .mkv file
        extracted_dir = self._config.work_dir / EXTRACTED_DIR
        if extracted_dir.exists():
            for f in sorted(extracted_dir.glob("*.mkv")):
                if not f.name.endswith(TEMP_SUFFIX):
                    return f
        return None

    @staticmethod
    def _outcome_from_artifacts(
        artifacts: list[ChunkArtifact],
        did_work:  bool,
    ) -> PhaseOutcome:
        """Derive ``PhaseOutcome`` from artifact states."""
        if any(a.state == ArtifactState.ABSENT for a in artifacts):
            return PhaseOutcome.DRY_RUN
        if all(a.state == ArtifactState.COMPLETE for a in artifacts) and artifacts:
            return PhaseOutcome.REUSED if not did_work else PhaseOutcome.COMPLETED
        return PhaseOutcome.DRY_RUN


# ---------------------------------------------------------------------------
# Module-level logging helpers
# ---------------------------------------------------------------------------

def _recovery_message(artifacts: list[ChunkArtifact]) -> str:
    complete = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
    pending  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
    return f"{complete} complete, {pending} pending"


def _failed(error: str) -> ChunkingPhaseResult:
    """Return a ``FAILED`` ``ChunkingPhaseResult`` with the given error."""
    return ChunkingPhaseResult(
        outcome   = PhaseOutcome.FAILED,
        artifacts = [],
        message   = error,
        error     = error,
        chunks    = [],
    )
