"""Phase recovery utilities for the pyqenc pipeline.

This module provides per-phase recovery functions that scan the filesystem,
classify existing artifacts using ``ArtifactState``, and return structured
results describing what work remains.  Each phase calls its recovery function
at startup before executing any work.

Recovery follows a two-step pattern:
1. **Parameter pre-validation** — compare current run parameters against the
   stored phase parameter file; invalidate stale artifacts if they differ.
2. **Artifact recovery** — scan the output directory, classify each artifact,
   and build the work-remaining set.

Extraction has no run-variable parameters, so it skips step 1 and proceeds
directly to artifact recovery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from pyqenc.constants import (
    CHUNK_NAME_PATTERN,
    ENCODED_ATTEMPT_GLOB_PATTERN,
    ENCODED_ATTEMPT_NAME_PATTERN,
    TEMP_SUFFIX,
)
from pyqenc.models import ChunkMetadata, PhaseOutcome, QualityTarget, SceneBoundary
from pyqenc.quality import CRFHistory
from pyqenc.state import ArtifactState, ChunkSidecar, EncodingResultSidecar, JobState, MetricsSidecar

if TYPE_CHECKING:
    from pyqenc.state import JobStateManager

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directory name constants (used by both discovery and recovery functions)
# ---------------------------------------------------------------------------

_EXTRACTED_DIR = "extracted"
_CHUNKS_DIR    = "chunks"
_ENCODED_DIR   = "encoded"


# ---------------------------------------------------------------------------
# Inputs discovery (standalone phase execution)
# ---------------------------------------------------------------------------

@dataclass
class InputsDiscovery:
    """Result of inputs discovery for a phase running in standalone mode.

    Attributes:
        phase:    Name of the phase that performed discovery.
        inputs:   Per-input artifact states, keyed by filename or identifier.
        ok:       ``True`` when all inputs are ``COMPLETE`` and the phase may proceed.
        error:    Human-readable error message when ``ok`` is ``False``.
    """

    phase:  str
    inputs: dict[str, ArtifactState] = field(default_factory=dict)
    ok:     bool                     = True
    error:  str | None               = None


def discover_inputs(
    phase:    str,
    work_dir: Path,
    job:      "JobState | None" = None,
) -> InputsDiscovery:
    """Scan prerequisite output directories and validate inputs for *phase*.

    Called at the start of each phase when it is invoked standalone (not as
    part of the auto-pipeline).  The auto-pipeline orchestrator bypasses this
    by passing prerequisite outputs directly to each phase (Req 11.2).

    Each phase checks that the artifacts produced by its prerequisite phase are
    all in ``COMPLETE`` state.  If any input is ``ABSENT`` or ``ARTIFACT_ONLY``,
    a critical-level message is logged and ``InputsDiscovery.ok`` is ``False``
    (Req 10.1, 11.1, 11.3, 11.4).

    Phase → prerequisite mapping:
    - ``chunking``     → ``extracted/`` (video ``.mkv`` files)
    - ``optimization`` → ``chunks/``    (chunk ``.mkv`` + ``.yaml`` sidecar pairs)
    - ``encoding``     → ``chunks/``    (chunk ``.mkv`` + ``.yaml`` sidecar pairs)
    - ``audio``        → ``extracted/`` (audio ``.mka`` files)
    - ``merge``        → ``encoded/``   (at least one strategy dir with ``.mkv`` files)
    - ``extraction``   → no prerequisites (source video validated by ``JobStateManager``)

    Args:
        phase:    Phase name (e.g. ``'chunking'``, ``'optimization'``).
        work_dir: Pipeline working directory.
        job:      Current job state (used for logging context; optional).

    Returns:
        ``InputsDiscovery`` describing the state of all prerequisite inputs.
    """
    phase_lower = phase.lower()

    if phase_lower == "extraction":
        # Extraction has no prerequisite phase — source video is validated by JobStateManager
        _logger.debug("Inputs discovery: extraction has no prerequisite phase")
        return InputsDiscovery(phase=phase, ok=True)

    if phase_lower == "chunking":
        return _discover_extraction_outputs(work_dir)

    if phase_lower in ("optimization", "encoding"):
        return _discover_chunking_outputs(work_dir)

    if phase_lower == "audio":
        return _discover_extraction_audio_outputs(work_dir)

    if phase_lower == "merge":
        return _discover_encoding_outputs(work_dir)

    _logger.warning("Inputs discovery: unknown phase %r — skipping", phase)
    return InputsDiscovery(phase=phase, ok=True)


def _discover_extraction_outputs(work_dir: Path) -> InputsDiscovery:
    """Check that the extraction phase produced at least one complete video stream.

    Used by the chunking phase when running standalone (Req 11.1, 10.1).

    Args:
        work_dir: Pipeline working directory.

    Returns:
        ``InputsDiscovery`` for the ``chunking`` phase.
    """
    extracted_dir = work_dir / _EXTRACTED_DIR
    inputs: dict[str, ArtifactState] = {}

    if not extracted_dir.exists():
        msg = (
            f"Inputs discovery (chunking): extracted/ directory not found in {work_dir}. "
            "Run the extraction phase first: pyqenc extract <source>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="chunking", inputs=inputs, ok=False, error=msg)

    video_files = [
        f for f in extracted_dir.iterdir()
        if f.is_file() and f.suffix == ".mkv" and not f.name.endswith(TEMP_SUFFIX)
    ]

    if not video_files:
        msg = (
            f"Inputs discovery (chunking): no extracted video (.mkv) files found in {extracted_dir}. "
            "Run the extraction phase first: pyqenc extract <source>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="chunking", inputs=inputs, ok=False, error=msg)

    for vf in video_files:
        inputs[vf.name] = ArtifactState.COMPLETE

    _logger.info(
        "Inputs discovery (chunking): %d extracted video file(s) found — all COMPLETE",
        len(video_files),
    )
    return InputsDiscovery(phase="chunking", inputs=inputs, ok=True)


def _discover_chunking_outputs(work_dir: Path) -> InputsDiscovery:
    """Check that the chunking phase produced a complete set of chunks.

    Used by the optimization and encoding phases when running standalone
    (Req 10.1, 10.2, 10.4, 11.1).

    All chunks must be in ``COMPLETE`` state (file + sidecar present).
    ``ARTIFACT_ONLY`` or ``ABSENT`` chunks are not acceptable as prerequisites
    (Req 10.1).

    Args:
        work_dir: Pipeline working directory.

    Returns:
        ``InputsDiscovery`` for the ``optimization`` or ``encoding`` phase.
    """
    chunks_dir = work_dir / _CHUNKS_DIR
    inputs: dict[str, ArtifactState] = {}

    if not chunks_dir.exists():
        msg = (
            f"Inputs discovery: chunks/ directory not found in {work_dir}. "
            "Run the chunking phase first: pyqenc chunk <video>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="optimization/encoding", inputs=inputs, ok=False, error=msg)

    chunk_files = [
        f for f in chunks_dir.glob("*.mkv")
        if CHUNK_NAME_PATTERN.match(f.stem) and not f.name.endswith(TEMP_SUFFIX)
    ]

    if not chunk_files:
        msg = (
            f"Inputs discovery: no chunk files found in {chunks_dir}. "
            "Run the chunking phase first: pyqenc chunk <video>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="optimization/encoding", inputs=inputs, ok=False, error=msg)

    incomplete: list[str] = []
    for cf in sorted(chunk_files):
        sidecar = cf.with_suffix(".yaml")
        if sidecar.exists():
            inputs[cf.name] = ArtifactState.COMPLETE
        else:
            inputs[cf.name] = ArtifactState.ARTIFACT_ONLY
            incomplete.append(cf.name)

    if incomplete:
        msg = (
            f"Inputs discovery: {len(incomplete)} chunk(s) are missing their sidecar "
            f"(ARTIFACT_ONLY — not acceptable as prerequisites): "
            f"{', '.join(incomplete[:5])}{'...' if len(incomplete) > 5 else ''}. "
            "Re-run the chunking phase to repair: pyqenc chunk <video>"
        )
        _logger.critical(msg)
        return InputsDiscovery(
            phase="optimization/encoding", inputs=inputs, ok=False, error=msg
        )

    _logger.info(
        "Inputs discovery: %d chunk(s) found — all COMPLETE",
        len(chunk_files),
    )
    return InputsDiscovery(phase="optimization/encoding", inputs=inputs, ok=True)


def _discover_extraction_audio_outputs(work_dir: Path) -> InputsDiscovery:
    """Check that the extraction phase produced at least one complete audio stream.

    Used by the audio phase when running standalone (Req 11.1, 10.1).

    Args:
        work_dir: Pipeline working directory.

    Returns:
        ``InputsDiscovery`` for the ``audio`` phase.
    """
    extracted_dir = work_dir / _EXTRACTED_DIR
    inputs: dict[str, ArtifactState] = {}

    if not extracted_dir.exists():
        msg = (
            f"Inputs discovery (audio): extracted/ directory not found in {work_dir}. "
            "Run the extraction phase first: pyqenc extract <source>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="audio", inputs=inputs, ok=False, error=msg)

    audio_files = [
        f for f in extracted_dir.iterdir()
        if f.is_file() and f.suffix == ".mka" and not f.name.endswith(TEMP_SUFFIX)
    ]

    if not audio_files:
        msg = (
            f"Inputs discovery (audio): no extracted audio (.mka) files found in {extracted_dir}. "
            "Run the extraction phase first: pyqenc extract <source>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="audio", inputs=inputs, ok=False, error=msg)

    for af in audio_files:
        inputs[af.name] = ArtifactState.COMPLETE

    _logger.info(
        "Inputs discovery (audio): %d extracted audio file(s) found — all COMPLETE",
        len(audio_files),
    )
    return InputsDiscovery(phase="audio", inputs=inputs, ok=True)


def _discover_encoding_outputs(work_dir: Path) -> InputsDiscovery:
    """Check that the encoding phase produced at least one complete encoded chunk.

    Used by the merge phase when running standalone (Req 10.5, 11.1).

    At least one strategy directory must exist with at least one ``.mkv`` file
    (no ``.tmp`` suffix).

    Args:
        work_dir: Pipeline working directory.

    Returns:
        ``InputsDiscovery`` for the ``merge`` phase.
    """
    encoded_dir = work_dir / _ENCODED_DIR
    inputs: dict[str, ArtifactState] = {}

    if not encoded_dir.exists():
        msg = (
            f"Inputs discovery (merge): encoded/ directory not found in {work_dir}. "
            "Run the encoding phase first: pyqenc encode <chunks_dir>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="merge", inputs=inputs, ok=False, error=msg)

    total_encoded = 0
    for strategy_dir in encoded_dir.iterdir():
        if not strategy_dir.is_dir():
            continue
        encoded_files = [
            f for f in strategy_dir.glob("*.mkv")
            if not f.name.endswith(TEMP_SUFFIX)
        ]
        for ef in encoded_files:
            inputs[f"{strategy_dir.name}/{ef.name}"] = ArtifactState.COMPLETE
            total_encoded += 1

    if total_encoded == 0:
        msg = (
            f"Inputs discovery (merge): no encoded chunk files found in {encoded_dir}. "
            "Run the encoding phase first: pyqenc encode <chunks_dir>"
        )
        _logger.critical(msg)
        return InputsDiscovery(phase="merge", inputs=inputs, ok=False, error=msg)

    _logger.info(
        "Inputs discovery (merge): %d encoded file(s) found across %d strategy dir(s) — all COMPLETE",
        total_encoded,
        sum(1 for d in encoded_dir.iterdir() if d.is_dir()),
    )
    return InputsDiscovery(phase="merge", inputs=inputs, ok=True)

# ---------------------------------------------------------------------------
# Extraction recovery
# ---------------------------------------------------------------------------


@dataclass
class ExtractionRecovery:
    """Result of extraction phase recovery.

    Attributes:
        video_files:  Artifact files found in the ``extracted/`` directory
                      classified as ``COMPLETE`` (present, written via ``.tmp``
                      protocol).
        state:        Overall state of the extraction phase:
                      ``COMPLETE`` — all expected artifacts are present;
                      ``ABSENT``   — no artifacts found; extraction must run.
        did_work:     Always ``False`` for recovery itself; set to ``True`` by
                      the phase after it performs actual extraction work.
    """

    video_files: list[Path]
    state:       ArtifactState
    did_work:    bool = False


def _cleanup_tmp_files(directory: Path) -> None:
    """Delete any leftover ``.tmp`` files in *directory*.

    Called at the start of each phase to remove partial files from a
    previously interrupted run.  Logs a warning for each file removed.

    Args:
        directory: Directory to scan for ``.tmp`` files.
    """
    if not directory.exists():
        return
    for tmp_file in directory.glob(f"*{TEMP_SUFFIX}"):
        try:
            tmp_file.unlink()
            _logger.warning(
                "Removed leftover temp file from previous interrupted run: %s",
                tmp_file,
            )
        except OSError as exc:
            _logger.warning("Could not remove temp file %s: %s", tmp_file, exc)


def recover_extraction(work_dir: Path, job: JobState) -> ExtractionRecovery:
    """Perform artifact recovery for the extraction phase.

    Extraction has no run-variable parameters, so there is no parameter
    pre-validation step.  Source file identity is validated by
    ``JobStateManager.validate`` at pipeline startup (Requirement 3.2).

    Steps:
    1. Clean up any leftover ``.tmp`` files in ``extracted/`` (Req 7.7).
    2. Scan ``extracted/`` for artifact files.
    3. Classify the overall state as ``COMPLETE`` (artifacts present) or
       ``ABSENT`` (no artifacts found).

    The ``.tmp`` protocol guarantees that any file present without a ``.tmp``
    suffix was written completely and successfully — no additional integrity
    checks are needed (Req 7.8, 4.1).

    Args:
        work_dir: Pipeline working directory.
        job:      Current job state (used for logging context).

    Returns:
        ``ExtractionRecovery`` describing what was found.
    """
    extracted_dir = work_dir / _EXTRACTED_DIR

    # Step 1: clean up leftover .tmp files (Req 7.7)
    _cleanup_tmp_files(extracted_dir)

    # Step 2: scan for artifact files
    if not extracted_dir.exists():
        _logger.debug("Extracted directory does not exist — extraction needed")
        return ExtractionRecovery(video_files=[], state=ArtifactState.ABSENT)

    # Collect all non-.tmp files (video .mkv and audio .mka)
    artifact_files: list[Path] = [
        f for f in extracted_dir.iterdir()
        if f.is_file() and not f.name.endswith(TEMP_SUFFIX)
    ]

    if not artifact_files:
        _logger.debug("No extracted artifacts found — extraction needed")
        return ExtractionRecovery(video_files=[], state=ArtifactState.ABSENT)

    # Step 3: classify — presence of final files (no .tmp) means COMPLETE
    video_files = [f for f in artifact_files if f.suffix == ".mkv"]
    _logger.info(
        "Extraction recovery: found %d artifact(s) — reusing (%s)",
        len(artifact_files),
        ", ".join(f.name for f in artifact_files),
    )
    return ExtractionRecovery(video_files=video_files, state=ArtifactState.COMPLETE)


# ---------------------------------------------------------------------------
# Chunking recovery
# ---------------------------------------------------------------------------


@dataclass
class ChunkRecovery:
    """Recovery state for a single chunk artifact.

    Attributes:
        chunk_id:  Chunk identifier (timestamp-range stem).
        path:      Path to the chunk ``.mkv`` file.
        state:     ``ABSENT`` — file not present;
                   ``ARTIFACT_ONLY`` — file present, sidecar missing;
                   ``COMPLETE`` — file and sidecar both present and valid.
        metadata:  ``ChunkMetadata`` loaded from sidecar (``COMPLETE`` only);
                   ``None`` for ``ABSENT`` / ``ARTIFACT_ONLY``.
    """

    chunk_id: str
    path:     Path
    state:    ArtifactState
    metadata: ChunkMetadata | None = None


@dataclass
class ChunkingRecovery:
    """Result of chunking phase recovery.

    Attributes:
        scenes:    Scene boundaries loaded from ``chunking.yaml``; empty list
                   when the file is absent (scene detection must run).
        chunks:    Per-chunk recovery state, keyed by ``chunk_id``.
        pending:   Chunk IDs that still need work (``ABSENT`` or
                   ``ARTIFACT_ONLY``).
        did_work:  Set to ``True`` by the phase after it performs actual work.
    """

    scenes:   list[SceneBoundary]              = field(default_factory=list)
    chunks:   dict[str, ChunkRecovery]         = field(default_factory=dict)
    pending:  list[str]                        = field(default_factory=list)
    did_work: bool                             = False


def _probe_chunk_metadata(chunk_file: Path, chunk_id: str) -> ChunkMetadata | None:
    """Probe *chunk_file* with ffprobe to obtain its metadata.

    Used when a chunk ``.mkv`` is present but its sidecar is missing
    (``ARTIFACT_ONLY`` state).

    Args:
        chunk_file: Path to the chunk ``.mkv`` file.
        chunk_id:   Chunk identifier (timestamp-range stem).

    Returns:
        ``ChunkMetadata`` on success, ``None`` on failure.
    """
    from pyqenc.models import VideoMetadata  # local import to avoid circular

    meta = VideoMetadata(path=chunk_file)
    # Trigger probes — duration/fps/resolution via ffprobe, frame_count via null-encode
    _ = meta.duration_seconds
    _ = meta.fps
    _ = meta.resolution
    _ = meta.frame_count

    if meta._duration_seconds is None:
        _logger.warning("Could not probe duration for chunk %s — skipping sidecar write", chunk_id)
        return None

    # Parse start/end timestamps from the chunk_id stem
    # Format: HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm
    try:
        start_ts, end_ts = _parse_chunk_timestamps(chunk_id)
    except ValueError as exc:
        _logger.warning("Could not parse timestamps from chunk_id %r: %s", chunk_id, exc)
        return None

    chunk_meta = ChunkMetadata(
        path=chunk_file,
        chunk_id=chunk_id,
        start_timestamp=start_ts,
        end_timestamp=end_ts,
    )
    chunk_meta._duration_seconds = meta._duration_seconds
    chunk_meta._frame_count      = meta._frame_count
    chunk_meta._fps              = meta._fps
    chunk_meta._resolution       = meta._resolution
    chunk_meta._pix_fmt          = meta._pix_fmt
    return chunk_meta


def _parse_chunk_timestamps(chunk_id: str) -> tuple[float, float]:
    """Parse start and end timestamps (in seconds) from a chunk_id stem.

    Chunk IDs have the form ``HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm`` where ``꞉`` is
    ``TIME_SEPARATOR_SAFE`` and ``․`` is ``TIME_SEPARATOR_MS``.

    Args:
        chunk_id: Timestamp-range chunk identifier.

    Returns:
        ``(start_seconds, end_seconds)`` tuple.

    Raises:
        ValueError: If the chunk_id does not match the expected format.
    """
    from pyqenc.constants import RANGE_SEPARATOR, TIME_SEPARATOR_MS, TIME_SEPARATOR_SAFE

    parts = chunk_id.split(RANGE_SEPARATOR, 1)
    if len(parts) != 2:
        raise ValueError(f"Expected exactly one '{RANGE_SEPARATOR}' in chunk_id: {chunk_id!r}")

    def _ts_to_seconds(ts: str) -> float:
        hms = ts.split(TIME_SEPARATOR_SAFE)
        if len(hms) != 3:
            raise ValueError(f"Expected HH꞉MM꞉SS in timestamp: {ts!r}")
        h, m, s_ms = hms
        s_ms = s_ms.replace(TIME_SEPARATOR_MS, ".")
        return int(h) * 3600 + int(m) * 60 + float(s_ms)

    return _ts_to_seconds(parts[0]), _ts_to_seconds(parts[1])


def recover_chunking(
    work_dir:     Path,
    job:          JobState,
    state_manager: "JobStateManager | None" = None,
) -> ChunkingRecovery:
    """Perform artifact recovery for the chunking phase.

    Chunking has no parameter pre-validation step (no run-variable parameters).
    Recovery proceeds directly to artifact recovery (Req 3.3, 5.1).

    Steps:
    1. Clean up any leftover ``.tmp`` files in ``chunks/`` (Req 7.7).
    2. Load scene boundaries from ``chunking.yaml`` if present (Req 5.3, 5.4).
    3. Scan ``chunks/`` for existing ``.mkv`` files matching the chunk name
       pattern; classify each as ``ABSENT``, ``ARTIFACT_ONLY``, or ``COMPLETE``
       (Req 5.2, 5.6, 5.7).
    4. For ``ARTIFACT_ONLY`` chunks: probe the file and write the sidecar
       (Req 5.7).

    Args:
        work_dir:      Pipeline working directory.
        job:           Current job state (used for logging context).
        state_manager: Optional ``JobStateManager`` used to load ``chunking.yaml``.
                       When ``None``, the file is loaded directly from *work_dir*.

    Returns:
        ``ChunkingRecovery`` describing what was found and what work remains.
    """
    from pyqenc.state import ChunkingParams, JobStateManager
    from pyqenc.utils.yaml_utils import write_yaml_atomic

    chunks_dir = work_dir / _CHUNKS_DIR

    # Step 1: clean up leftover .tmp files (Req 7.7)
    _cleanup_tmp_files(chunks_dir)

    # Step 2: load scene boundaries from chunking.yaml (Req 5.3, 5.4)
    if state_manager is not None:
        chunking_params = state_manager.load_chunking()
    else:
        _sm = JobStateManager(work_dir=work_dir, source_video=job.source.path)
        chunking_params = _sm.load_chunking()

    scenes: list[SceneBoundary] = []
    if chunking_params is not None and chunking_params.scenes:
        scenes = chunking_params.scenes
        _logger.info(
            "Chunking recovery: loaded %d scene boundary(ies) from chunking.yaml",
            len(scenes),
        )
    else:
        _logger.debug("Chunking recovery: chunking.yaml absent or empty — scene detection needed")

    # Step 3: scan chunks/ for existing .mkv files
    chunk_recoveries: dict[str, ChunkRecovery] = {}
    pending: list[str] = []

    if not chunks_dir.exists():
        _logger.debug("Chunks directory does not exist — all chunks pending")
        return ChunkingRecovery(scenes=scenes, chunks={}, pending=[])

    # Discover all chunk .mkv files (matching the timestamp-range pattern)
    for chunk_file in sorted(chunks_dir.glob("*.mkv")):
        chunk_id = chunk_file.stem
        if not CHUNK_NAME_PATTERN.match(chunk_id):
            _logger.debug("Skipping non-chunk file: %s", chunk_file.name)
            continue

        sidecar_path = chunk_file.with_suffix(".yaml")

        if not sidecar_path.exists():
            # ARTIFACT_ONLY: file present, sidecar missing — probe and write sidecar (Req 5.7)
            _logger.debug(
                "Chunk %s: file present, sidecar missing — probing and writing sidecar", chunk_id
            )
            chunk_meta = _probe_chunk_metadata(chunk_file, chunk_id)
            if chunk_meta is not None:
                sidecar = ChunkSidecar(chunk=chunk_meta)
                try:
                    write_yaml_atomic(sidecar_path, sidecar.to_yaml_dict())
                    _logger.info("Wrote missing sidecar for chunk %s", chunk_id)
                    chunk_recoveries[chunk_id] = ChunkRecovery(
                        chunk_id=chunk_id,
                        path=chunk_file,
                        state=ArtifactState.COMPLETE,
                        metadata=chunk_meta,
                    )
                except Exception as exc:
                    _logger.warning(
                        "Could not write sidecar for chunk %s: %s — treating as ARTIFACT_ONLY",
                        chunk_id, exc,
                    )
                    chunk_recoveries[chunk_id] = ChunkRecovery(
                        chunk_id=chunk_id,
                        path=chunk_file,
                        state=ArtifactState.ARTIFACT_ONLY,
                    )
                    pending.append(chunk_id)
            else:
                chunk_recoveries[chunk_id] = ChunkRecovery(
                    chunk_id=chunk_id,
                    path=chunk_file,
                    state=ArtifactState.ARTIFACT_ONLY,
                )
                pending.append(chunk_id)
        else:
            # Sidecar present — try to load it (Req 5.6)
            try:
                import yaml
                with sidecar_path.open("r", encoding="utf-8") as fh:
                    sidecar_data = yaml.safe_load(fh)
                chunk_meta = ChunkSidecar.from_yaml_dict(
                    sidecar_data, chunk_id=chunk_id, path=chunk_file
                ).chunk
                chunk_recoveries[chunk_id] = ChunkRecovery(
                    chunk_id=chunk_id,
                    path=chunk_file,
                    state=ArtifactState.COMPLETE,
                    metadata=chunk_meta,
                )
                _logger.debug("Chunk %s: COMPLETE (sidecar loaded)", chunk_id)
            except Exception as exc:
                _logger.warning(
                    "Could not load sidecar for chunk %s: %s — treating as ARTIFACT_ONLY",
                    chunk_id, exc,
                )
                chunk_recoveries[chunk_id] = ChunkRecovery(
                    chunk_id=chunk_id,
                    path=chunk_file,
                    state=ArtifactState.ARTIFACT_ONLY,
                )
                pending.append(chunk_id)

    complete_count = sum(1 for r in chunk_recoveries.values() if r.state == ArtifactState.COMPLETE)
    _logger.info(
        "Chunking recovery: %d chunk(s) found — %d COMPLETE, %d pending",
        len(chunk_recoveries),
        complete_count,
        len(pending),
    )

    return ChunkingRecovery(
        scenes=scenes,
        chunks=chunk_recoveries,
        pending=pending,
    )


# ---------------------------------------------------------------------------
# Attempts recovery (shared by optimization and encoding phases)
# ---------------------------------------------------------------------------


@dataclass
class AttemptRecovery:
    """Recovery state for one encoded attempt file (one CRF value).

    Attributes:
        path:    Path to the encoded attempt ``.mkv`` file.
        crf:     CRF value used for this attempt.
        state:   ``ARTIFACT_ONLY`` — sidecar absent or incomplete;
                 ``COMPLETE``      — sidecar present and all metrics loaded.
        metrics: All measured metric values from the sidecar, or ``None``
                 when ``state`` is ``ARTIFACT_ONLY``.
    """

    path:    Path
    crf:     float
    state:   ArtifactState
    metrics: dict[str, float] | None = None


@dataclass
class EncodingRecovery:
    """Recovery state for a ``(chunk_id, strategy)`` pair — the CRF search as a whole.

    Attributes:
        chunk_id:  Chunk identifier.
        strategy:  Strategy name.
        state:     Overall pair state: ``ABSENT`` / ``ARTIFACT_ONLY`` / ``COMPLETE``.
        history:   ``CRFHistory`` reconstructed from per-attempt sidecars; empty
                   when ``state`` is ``ABSENT``.
        attempts:  Individual attempt files found on disk.
    """

    chunk_id: str
    strategy: str
    state:    ArtifactState
    history:  CRFHistory                = field(default_factory=CRFHistory)
    attempts: list[AttemptRecovery]     = field(default_factory=list)


@dataclass
class PhaseRecovery:
    """Recovery result for an entire optimization or encoding phase.

    Attributes:
        pairs:    Per-pair recovery state, keyed by ``(chunk_id, strategy)``.
        pending:  Pairs where ``state != COMPLETE`` — work still needed.
        did_work: Set to ``True`` by the phase after it performs actual work.
    """

    pairs:    dict[tuple[str, str], EncodingRecovery] = field(default_factory=dict)
    pending:  list[tuple[str, str]]                   = field(default_factory=list)
    did_work: bool                                    = False


def _strategy_dir(work_dir: Path, strategy: str) -> Path:
    """Return the output directory for *strategy* under ``encoded/``.

    Mirrors the naming logic in ``ChunkEncoder._get_output_dir``.

    Args:
        work_dir:  Pipeline working directory.
        strategy:  Strategy name (e.g. ``'slow+h265-aq'``).

    Returns:
        Path to the strategy subdirectory inside ``<work_dir>/encoded/``.
    """
    safe = strategy.replace("+", "_").replace(":", "_")
    return work_dir / _ENCODED_DIR / safe


def _load_metrics_sidecar(attempt_path: Path) -> MetricsSidecar | None:
    """Load a per-attempt YAML metrics sidecar for *attempt_path*.

    Args:
        attempt_path: Path to the encoded attempt ``.mkv`` file.

    Returns:
        ``MetricsSidecar`` on success, ``None`` if the sidecar is absent or
        cannot be parsed.
    """
    sidecar_path = attempt_path.with_suffix(".yaml")
    if not sidecar_path.exists():
        return None
    try:
        with sidecar_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return MetricsSidecar.from_yaml_dict(data or {})
    except Exception as exc:
        _logger.debug("Could not load metrics sidecar %s: %s", sidecar_path, exc)
        return None


def _load_encoding_result_sidecar(
    strategy_dir: Path,
    chunk_id:     str,
    resolution:   str,
) -> EncodingResultSidecar | None:
    """Load the encoding result sidecar for a ``(chunk_id, resolution)`` pair.

    The sidecar filename is ``<chunk_id>.<resolution>.yaml`` inside the
    strategy directory.

    Args:
        strategy_dir: Path to the strategy output directory.
        chunk_id:     Chunk identifier.
        resolution:   Resolution string (e.g. ``'1920x800'``).

    Returns:
        ``EncodingResultSidecar`` on success, ``None`` if absent or invalid.
    """
    sidecar_path = strategy_dir / f"{chunk_id}.{resolution}.yaml"
    if not sidecar_path.exists():
        return None
    try:
        with sidecar_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return EncodingResultSidecar.from_yaml_dict(data or {})
    except Exception as exc:
        _logger.debug("Could not load encoding result sidecar %s: %s", sidecar_path, exc)
        return None


def _targets_met(metrics: dict[str, float], quality_targets: list[QualityTarget]) -> bool:
    """Re-evaluate whether *metrics* satisfy all *quality_targets*.

    Always re-evaluates from raw metrics — never trusts the persisted
    ``targets_met`` field (Req 6a.2).

    Args:
        metrics:         Measured metric values keyed by ``<metric>_<statistic>``.
        quality_targets: Current quality targets to evaluate against.

    Returns:
        ``True`` if all targets are met, ``False`` otherwise.
    """
    for target in quality_targets:
        key    = f"{target.metric}_{target.statistic}"
        actual = metrics.get(key)
        if actual is None or actual < target.value:
            return False
    return True


def _recover_pair(
    chunk_id:        str,
    strategy:        str,
    strategy_dir:    Path,
    quality_targets: list[QualityTarget],
) -> EncodingRecovery:
    """Classify a single ``(chunk_id, strategy)`` pair and reconstruct its history.

    Flow (Req 6.2, 6.3, 6.4, 6.5):
    1. Scan for attempt ``.mkv`` files belonging to *chunk_id*.
    2. For each attempt, check for a per-attempt YAML sidecar.
    3. Check for an encoding result sidecar (``<chunk_id>.<res>.yaml``).
       If present and valid → ``COMPLETE``.
    4. If no valid result sidecar but ≥1 attempt file → ``ARTIFACT_ONLY``;
       reconstruct ``CRFHistory`` from per-attempt sidecars.
    5. If no attempt files → ``ABSENT``.

    Args:
        chunk_id:        Chunk identifier.
        strategy:        Strategy name.
        strategy_dir:    Path to the strategy output directory.
        quality_targets: Current quality targets for re-evaluation.

    Returns:
        ``EncodingRecovery`` for this pair.
    """
    if not strategy_dir.exists():
        return EncodingRecovery(chunk_id=chunk_id, strategy=strategy, state=ArtifactState.ABSENT)

    # --- Step 1: discover all attempt .mkv files for this chunk_id ---
    attempt_recoveries: list[AttemptRecovery] = []
    history = CRFHistory()

    for attempt_file in sorted(strategy_dir.glob(ENCODED_ATTEMPT_GLOB_PATTERN)):
        m = ENCODED_ATTEMPT_NAME_PATTERN.match(attempt_file.name)
        if m is None or m.group("chunk_id") != chunk_id:
            continue

        # Validate the file still exists (Req 6.5)
        if not attempt_file.exists():
            _logger.warning(
                "Attempt file listed but missing on disk — skipping: %s", attempt_file
            )
            continue

        try:
            crf = float(m.group("crf"))
        except ValueError:
            _logger.debug("Could not parse CRF from attempt filename: %s", attempt_file.name)
            continue

        sidecar = _load_metrics_sidecar(attempt_file)
        if sidecar is not None:
            history.add_attempt(crf, sidecar.metrics)
            attempt_recoveries.append(AttemptRecovery(
                path=attempt_file,
                crf=crf,
                state=ArtifactState.COMPLETE,
                metrics=sidecar.metrics,
            ))
        else:
            # Sidecar absent — file present but metadata unknown (Req 6a.4)
            attempt_recoveries.append(AttemptRecovery(
                path=attempt_file,
                crf=crf,
                state=ArtifactState.ARTIFACT_ONLY,
                metrics=None,
            ))

    # --- Step 2: check for encoding result sidecar (COMPLETE marker) ---
    # The result sidecar filename encodes the resolution; scan for any matching file.
    result_sidecar:    EncodingResultSidecar | None = None
    result_sidecar_path: Path | None               = None

    for candidate in strategy_dir.glob(f"{chunk_id}.*.yaml"):
        # Must match <chunk_id>.<resolution>.yaml — not an attempt sidecar
        stem_parts = candidate.stem.split(".")
        if len(stem_parts) < 2:
            continue
        resolution_candidate = stem_parts[-1]
        # Resolution looks like NNNNxNNNN
        if not (resolution_candidate.count("x") == 1 and all(
            part.isdigit() for part in resolution_candidate.split("x")
        )):
            continue

        loaded = _load_encoding_result_sidecar(strategy_dir, chunk_id, resolution_candidate)
        if loaded is not None:
            result_sidecar      = loaded
            result_sidecar_path = candidate
            break

    if result_sidecar is not None and result_sidecar_path is not None:
        # Verify the referenced attempt file still exists (Req 6b.4)
        winning_path = strategy_dir / result_sidecar.winning_attempt
        if not winning_path.exists():
            _logger.warning(
                "Encoding result sidecar %s references missing attempt file %s — "
                "deleting sidecar and treating pair as ARTIFACT_ONLY",
                result_sidecar_path.name,
                result_sidecar.winning_attempt,
            )
            try:
                result_sidecar_path.unlink()
            except OSError as exc:
                _logger.warning("Could not delete stale result sidecar: %s", exc)
            result_sidecar = None

        elif not _targets_met(result_sidecar.metrics, quality_targets):
            # Targets changed since the sidecar was written — downgrade (Req 6.4)
            _logger.info(
                "Encoding result sidecar for %s/%s no longer meets current quality targets — "
                "treating pair as ARTIFACT_ONLY",
                chunk_id, strategy,
            )
            result_sidecar = None

    if result_sidecar is not None:
        _logger.debug("Pair %s/%s: COMPLETE (encoding result sidecar valid)", chunk_id, strategy)
        return EncodingRecovery(
            chunk_id=chunk_id,
            strategy=strategy,
            state=ArtifactState.COMPLETE,
            history=history,
            attempts=attempt_recoveries,
        )

    if attempt_recoveries:
        _logger.debug(
            "Pair %s/%s: ARTIFACT_ONLY (%d attempt(s) found, CRF search in progress)",
            chunk_id, strategy, len(attempt_recoveries),
        )
        return EncodingRecovery(
            chunk_id=chunk_id,
            strategy=strategy,
            state=ArtifactState.ARTIFACT_ONLY,
            history=history,
            attempts=attempt_recoveries,
        )

    _logger.debug("Pair %s/%s: ABSENT (no attempt files found)", chunk_id, strategy)
    return EncodingRecovery(
        chunk_id=chunk_id,
        strategy=strategy,
        state=ArtifactState.ABSENT,
    )


def recover_attempts(
    work_dir:        Path,
    chunk_ids:       list[str],
    strategies:      list[str],
    quality_targets: list[QualityTarget],
) -> PhaseRecovery:
    """Shared artifact recovery for the optimization and encoding phases.

    For each ``(chunk_id, strategy)`` pair, determines the ``ArtifactState``
    and reconstructs ``CRFHistory`` from per-attempt sidecars so the CRF
    search can resume from where it left off (Req 6.1, 6.2, 6.3, 6.4, 6.5).

    Classification per pair (Req 6.2):
    - ``COMPLETE``      — encoding result sidecar present, referenced attempt
                          file exists, and metrics still meet current targets.
    - ``ARTIFACT_ONLY`` — no valid encoding result sidecar, but ≥1 attempt
                          ``.mkv`` file exists; CRF search is in progress.
    - ``ABSENT``        — no attempt files at all; search has not started.

    Args:
        work_dir:        Pipeline working directory.
        chunk_ids:       Chunk identifiers to recover.
        strategies:      Strategy names to recover.
        quality_targets: Current quality targets used to re-evaluate whether
                         persisted results are still valid.

    Returns:
        ``PhaseRecovery`` with per-pair recovery state and the list of pairs
        that still need work.
    """
    pairs:   dict[tuple[str, str], EncodingRecovery] = {}
    pending: list[tuple[str, str]]                   = []

    complete_count      = 0
    artifact_only_count = 0
    absent_count        = 0

    for chunk_id in chunk_ids:
        for strategy in strategies:
            strat_dir = _strategy_dir(work_dir, strategy)
            recovery  = _recover_pair(chunk_id, strategy, strat_dir, quality_targets)
            pairs[(chunk_id, strategy)] = recovery

            if recovery.state == ArtifactState.COMPLETE:
                complete_count += 1
            elif recovery.state == ArtifactState.ARTIFACT_ONLY:
                artifact_only_count += 1
                pending.append((chunk_id, strategy))
            else:
                absent_count += 1
                pending.append((chunk_id, strategy))

    _logger.info(
        "Attempts recovery: %d pair(s) total — %d COMPLETE, %d ARTIFACT_ONLY, %d ABSENT",
        len(pairs),
        complete_count,
        artifact_only_count,
        absent_count,
    )

    return PhaseRecovery(pairs=pairs, pending=pending, did_work=False)
