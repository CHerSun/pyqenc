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
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
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
    collector:     MetricsCollector,
) -> list[ChunkMetadata]:
    """Split the source video into chunks using scene boundaries.

    Skips chunks already present on disk (as determined by *recovery*).
    Writes a ``<chunk_stem>.yaml`` sidecar after each successful split (Req 5.5).
    Calls ``collector.step(MetricKey.CHUNKING, "split")`` after each successful split
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
                path            = chunk_file,
                chunk_id        = stem,
                start_timestamp = start_ts,
                end_timestamp   = end_ts,
                frame_count     = 0,
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

            # Set duration and frame count from the known range / ffmpeg output
            chunk_meta._duration_seconds = end_ts - start_ts
            chunk_meta.frame_count = split_result.frame_count or 0

            if chunk_meta.frame_count == 0:
                logger.warning("Frame count not found in ffmpeg output for chunk %s", stem)

            # Write chunk sidecar (Req 5.5)
            _write_chunk_sidecar(chunk_file, chunk_meta)

            result_chunks.append(chunk_meta)
            logger.debug("Chunk %s split successfully", stem)
            collector.step(MetricKey.CHUNKING, "split")
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyqenc.metrics import MetricsCollector
    from pyqenc.phase import Phase, PhaseResult

from pyqenc.constants import (
    CHUNKS_DIR,
    EXTRACTED_DIR,
    TEMP_SUFFIX,
    THICK_LINE,
)
from pyqenc.metrics import MetricKey
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

_CHUNKING_YAML     = "chunking.yaml"


class ChunkArtifact(Artifact):
    """Chunking artifact for a single video chunk.

    ``metadata`` is lazy-loaded from the sidecar YAML on first access.
    The sidecar path is derived from ``path`` (same stem, ``.yaml`` suffix).
    Returns ``None`` if the sidecar is absent or cannot be parsed.
    """

    def __init__(self, path: Path, state: ArtifactState, wanted: bool = True) -> None:
        super().__init__(path=path, state=state, wanted=wanted)
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


@dataclass
class ChunkingPhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying chunking-specific payload.

    Attributes:
        chunks: List of chunk metadata for all ``COMPLETE`` artifacts.
    """

    chunks: list[ChunkMetadata] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.chunks is None:
            self.chunks = []


class ChunkingPhase(Phase):
    """Phase object for scene-based video chunking.

    Owns artifact enumeration, recovery, invalidation, execution, and logging
    for the chunking phase.  Wraps the existing ``detect_scenes`` and
    ``split_chunks`` helpers. The uniform run footprint is inherited from
    :class:`Phase`.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "chunking"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (JobPhase, ExtractionPhase)
    _METRIC_KEY: MetricKey = MetricKey.CHUNKING

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry | None = None,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

        self.params = ChunkingParams(chunking_mode=config.chunking.mode.value, scenes=[])

        # Set by _recover() when scene boundaries were loaded from chunking.yaml.
        self._recovered_scenes: list[SceneBoundary] = []

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the chunking mode (key parameter)."""
        logger.info("Mode:  %s", self._config.chunking.mode.value)

    def _recovery_unit(self) -> str:
        """The recovery summary counts chunks."""
        return "chunk"

    def _recover(self) -> Recovery:
        """Classify chunk artifacts, handle force-wipe and mode invalidation.

        Steps:
        1. If ``force_wipe``: delete ``chunks/`` and ``chunking.yaml``.
        2. Clean up leftover ``.tmp`` files.
        3. Load scene boundaries from ``chunking.yaml``; a persisted chunking
           mode differing from the current config is a fatal invalidation
           without ``--force`` (chunks are incompatible), or — with ``--force``
           — a wipe of ``chunks/`` + sidecar with ``force_wipe`` propagated
           downstream through the job result.
        4. Classify chunks from the authoritative scene list (expected set):
           file + sidecar → COMPLETE, file without sidecar → PARTIAL, missing
           → ABSENT. Chunk files on disk that are NOT in the expected set are
           surplus and surface as ``wanted=False`` artifacts (kept in place,
           never pending).

        Returns:
            The :class:`Recovery` single source of truth.

        Raises:
            RecoveryError: On a chunking-mode change without ``--force``.
        """
        work_dir   = self._dep(JobPhase).result.work_dir  # type: ignore[union-attr]
        chunks_dir = work_dir / CHUNKS_DIR
        yaml_path  = work_dir / _CHUNKING_YAML
        force_wipe = getattr(self._dep(JobPhase).result, "force_wipe", False)  # type: ignore[union-attr]

        # Step 1: force-wipe
        if force_wipe:
            if chunks_dir.exists():
                shutil.rmtree(chunks_dir)
                logger.debug("force_wipe: deleted %s", chunks_dir)
            if yaml_path.exists():
                yaml_path.unlink()
                logger.debug("force_wipe: deleted %s", yaml_path)

        # Step 2: clean up .tmp files
        if chunks_dir.exists():
            for tmp in chunks_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        if not chunks_dir.exists():
            self._recovered_scenes = []
            return Recovery(pending=True)

        # Step 3: load scene boundaries from chunking.yaml
        chunking_params = ChunkingParams.load(yaml_path)
        scenes: list[SceneBoundary] = []
        if chunking_params is not None and chunking_params.scenes:
            scenes = chunking_params.scenes
            logger.debug(
                "Chunking recovery: loaded %d scene boundary(ies) from chunking.yaml",
                len(scenes),
            )
            if scenes:
                logger.info("Scenes:  %d (from chunking.yaml)", len(scenes))

            # Mode mismatch check: if the persisted mode differs from the current
            # config, chunks are incompatible and cannot be reused.
            persisted_mode = chunking_params.chunking_mode
            current_mode   = self.params.chunking_mode
            if persisted_mode is not None and persisted_mode != current_mode:
                if self._dep(JobPhase).result.force_wipe:  # type: ignore[union-attr]
                    logger.warning(
                        "Chunking mode changed (%s → %s) — --force: wiping chunks/ and downstream artifacts",
                        persisted_mode, current_mode,
                    )
                    if chunks_dir.exists():
                        shutil.rmtree(chunks_dir)
                        logger.debug("force_wipe: deleted %s", chunks_dir)
                    yaml_path.unlink(missing_ok=True)
                    logger.debug("force_wipe: deleted %s", yaml_path)
                    # Propagate force_wipe to downstream phases through the
                    # standard path (the job result).
                    self._dep(JobPhase).result.force_wipe = True  # type: ignore[union-attr]
                    self._recovered_scenes = []
                    return Recovery(pending=True)
                raise RecoveryError(
                    f"Chunking mode changed since last run "
                    f"(persisted={persisted_mode!r}, current={current_mode!r}). "
                    "Existing chunks are incompatible. "
                    "Re-run with --force to delete stale chunks and continue."
                )
        else:
            logger.debug("Chunking recovery: chunking.yaml absent or empty — scene detection needed")

        # Step 4: classify chunks.
        #
        # When scene boundaries are known (from chunking.yaml), the scene list is
        # the authoritative source of truth.  We iterate expected chunks derived
        # from the boundaries and classify each one:
        #   - file + sidecar present  → COMPLETE
        #   - file present, no sidecar → PARTIAL (pending)
        #   - file absent              → ABSENT (pending)
        # Files on disk that are NOT in the expected set are surplus: they
        # surface as present-but-unwanted (wanted=False) per the Phase
        # Contract — retained in place, never counted as pending.
        #
        # When no scene boundaries are available yet, fall back to a plain disk
        # scan — scene detection will run during execution to determine boundaries.
        artifacts:      list[ChunkArtifact] = []
        pending_ids:    list[str]           = []
        expected_paths: set[Path]           = set()

        if scenes:
            # Get source duration from JobPhase result — it's already probed and cached there.
            job_result = self._dep(JobPhase).result
            job_state  = getattr(job_result, "job", None)
            source_duration: float | None = (
                job_state.source.duration_seconds if job_state is not None else None
            )

            for start_ts, end_ts, stem, chunk_file in _expand_scenes(
                scenes, source_duration, chunks_dir
            ):
                expected_paths.add(chunk_file)
                if chunk_file.exists():
                    if chunk_file.with_suffix(".yaml").exists():
                        artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.COMPLETE))
                        logger.debug("Chunk %s: COMPLETE", stem)
                    else:
                        artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.PARTIAL))
                        pending_ids.append(stem)
                        logger.debug("Chunk %s: PARTIAL (sidecar missing)", stem)
                else:
                    artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.ABSENT))
                    pending_ids.append(stem)
                    logger.debug("Chunk %s: ABSENT (missing from disk)", stem)

            # Surface present-but-unwanted surplus chunk files (scene list
            # changed underneath them; deletion only via explicit cleanup).
            for chunk_file in sorted(chunks_dir.glob("*.mkv")):
                if chunk_file in expected_paths or not CHUNK_NAME_PATTERN.match(chunk_file.stem):
                    continue
                state = (
                    ArtifactState.COMPLETE
                    if chunk_file.with_suffix(".yaml").exists()
                    else ArtifactState.PARTIAL
                )
                artifacts.append(ChunkArtifact(
                    path=chunk_file, state=state, wanted=False,
                ))
                logger.debug("Chunk %s: surplus (not in scene list) — unwanted", chunk_file.stem)
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
                    artifacts.append(ChunkArtifact(path=chunk_file, state=ArtifactState.PARTIAL))
                    pending_ids.append(stem)
                    logger.debug("Chunk %s: PARTIAL (sidecar missing)", stem)

        complete_count = len(artifacts) - len(pending_ids)
        logger.debug(
            "Chunking recovery: %d chunk(s) found — %d COMPLETE, %d pending",
            len(artifacts), complete_count, len(pending_ids),
        )

        # Store recovered scene boundaries for use in _execute
        self._recovered_scenes = scenes

        return Recovery.from_artifacts(artifacts)

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[ChunkArtifact],
        message:   str,
        error:     str | None = None,
    ) -> ChunkingPhaseResult:
        """Assemble a ``ChunkingPhaseResult`` deriving the chunk payload.

        ``chunks`` holds the metadata of all ``COMPLETE`` wanted artifacts
        (lazy sidecar load), sorted by chunk id.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list.
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result.
        """
        chunks = [
            a.metadata for a in artifacts
            if a.state == ArtifactState.COMPLETE and a.metadata is not None
        ]
        chunks.sort(key=lambda c: c.chunk_id)
        return ChunkingPhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = message,
            error     = error,
            chunks    = chunks,
        )

    def finalize(self, ctx: FinalizeContext) -> None:
        """Perform end-of-run housekeeping for the chunking phase.

        When ``ctx.deep_cleanup`` is ``True``, deletes this phase's own
        ``chunks/`` directory — its chunk files are consumables reproducible
        from the extracted video and are only needed until merging completes.
        ``chunking.yaml`` is a recovery sidecar and is left in place. Deletion
        is guarded by an existence check and never raises: any ``OSError`` is
        caught and logged as a warning so a cleanup failure never fails the run.

        Args:
            ctx: Pre-resolved end-of-run decisions from the runner.
        """
        if not ctx.deep_cleanup:
            return
        job = self._dep(JobPhase)
        if job.result is None:
            return
        chunks_dir = job.result.work_dir / CHUNKS_DIR
        if chunks_dir.exists():
            try:
                shutil.rmtree(chunks_dir)
                logger.debug("deep cleanup: deleted %s", chunks_dir)
            except OSError as exc:
                logger.warning("deep cleanup: could not delete %s: %s", chunks_dir, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------


    def _execute(
        self,
        wanted:  list[ChunkArtifact],
        dry_run: bool,
    ) -> ChunkingPhaseResult:
        """Detect scenes if needed and split pending chunks.

        The top-level ``chunking`` span belongs to the template; the two
        sub-actions carry their own dotted spans (``chunking.scene_detect``,
        ``chunking.split``). ``dry_run`` is never ``True`` here (chunking is
        not a readonly-execute phase; the template previews instead).

        Args:
            wanted:  Wanted artifact list from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``ChunkingPhaseResult`` after chunking.
        """
        from pyqenc.metrics import MetricKey
        artifacts = wanted
        work_dir   = self._dep(JobPhase).result.work_dir  # type: ignore[union-attr]
        chunks_dir = work_dir / CHUNKS_DIR
        chunks_dir.mkdir(parents=True, exist_ok=True)

        pending = [a for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)]
        if pending:
            logger.info("Pending: %d chunk(s) to split", len(pending))

        # Resolve the video file from ExtractionPhase result
        video_file = self._resolve_video_file()
        if video_file is None:
            err = "No extracted video file available for chunking"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)

        if not video_file.exists():
            err = f"Video file not found: {video_file}"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)

        job_result = self._dep(JobPhase).result  # type: ignore[union-attr]
        job_state  = getattr(job_result, "job", None)
        if job_state is None:
            from pyqenc.state import JobState as _JobState
            job_state = _JobState(source=VideoMetadata(path=job_result.source))
        video_meta = job_state.source

        # Use recovered scene boundaries or run detection
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
                        video_meta       = video_meta,
                        scene_threshold  = self._config.chunking.scene_threshold,
                        min_scene_length = self._config.chunking.min_scene_length,
                    )
                self.params.scenes = boundaries
                self.params.save(work_dir / _CHUNKING_YAML)
            except Exception as exc:
                logger.error("Scene detection failed: %s", exc, exc_info=True)
                return self._make_result(PhaseOutcome.FAILED, [], str(exc), error=str(exc))

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
                if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)
            ],
        )

        try:
            with self._collector.time(MetricKey.CHUNKING, "split"):
                chunk_metas = split_chunks(
                    video_meta    = video_meta,
                    output_dir    = chunks_dir,
                    boundaries    = boundaries,
                    recovery      = recovery_obj,
                    chunking_mode = self._config.chunking.mode,
                    collector     = self._collector,
                )
        except Exception as exc:
            logger.error("Chunk splitting failed: %s", exc, exc_info=True)
            return self._make_result(PhaseOutcome.FAILED, [], str(exc), error=str(exc))

        if not chunk_metas:
            err = "No valid chunks created."
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)

        # Build final artifact list
        final_artifacts: list[ChunkArtifact] = []
        for cm in chunk_metas:
            final_artifacts.append(ChunkArtifact(
                path  = cm.path,
                state = ArtifactState.COMPLETE,
            ))

        total_frames = sum(c.frame_count for c in chunk_metas)
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
        extraction = self._dep(ExtractionPhase)
        if extraction.result is None:
            return None
        video_meta = getattr(extraction.result, "video", None)
        if video_meta is not None:
            return video_meta.path
        # Fallback: scan extracted/ for a .mkv file
        extracted_dir = self._dep(JobPhase).result.work_dir / EXTRACTED_DIR  # type: ignore[union-attr]
        if extracted_dir.exists():
            for f in sorted(extracted_dir.glob("*.mkv")):
                if not f.name.endswith(TEMP_SUFFIX):
                    return f
        return None


# ---------------------------------------------------------------------------
# Module-level logging helpers
# ---------------------------------------------------------------------------
