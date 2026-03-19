"""Job state management and artifact classification for the pyqenc pipeline.

This module provides:

- ``ArtifactState`` — three-value enum classifying each artifact's recovery
  state (``ABSENT`` / ``ARTIFACT_ONLY`` / ``COMPLETE``).
- Data models: ``JobState``, ``ChunkingParams``, ``OptimizationParams``,
  ``EncodingParams``, ``MetricsSidecar``, ``EncodingResultSidecar``,
  ``ChunkSidecar``.
- ``JobStateManager`` — replaces ``ProgressTracker``; typed load/save methods
  for ``job.yaml`` and all phase parameter YAML files.
"""

from __future__ import annotations

import logging
import shutil
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from pyqenc.models import (
    ChunkMetadata,
    CropParams,
    SceneBoundary,
    VideoMetadata,
)
from pyqenc.utils.yaml_utils import write_yaml_atomic

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ArtifactState
# ---------------------------------------------------------------------------

class ArtifactState(Enum):
    """Recovery state of a single pipeline artifact.

    Used throughout recovery logic to classify each artifact and decide what
    work remains for the current run.

    Attributes:
        ABSENT:        The artifact file does not exist — not yet produced, or
                       invalidated by a parameter change.  Recovery action:
                       produce the artifact and its sidecar.
        ARTIFACT_ONLY: The artifact file is present and consistent (written via
                       the ``.tmp`` protocol), but its sidecar is missing or
                       incomplete.  Recovery action: produce the sidecar only.
        COMPLETE:      The artifact file is present and its sidecar is present
                       and contains all required data.  Recovery action: skip —
                       no work needed.
    """

    ABSENT        = "absent"
    ARTIFACT_ONLY = "artifact_only"
    COMPLETE      = "complete"


# ---------------------------------------------------------------------------
# Phase parameter / sidecar data models
# ---------------------------------------------------------------------------

class JobState(BaseModel):
    """Stable source video parameters stored in ``job.yaml``.

    Contains only run-invariant metadata — no phase status, no chunk tracking.
    ``crop_params`` is ``None`` when crop detection has not yet run.
    """

    source: VideoMetadata
    crop:   CropParams | None = None

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict using ``model_dump_full``."""
        data = self.source.model_dump_full()
        # Convert Path to str for YAML serialisation
        data["path"] = str(data["path"])
        result: dict = {"source": data}
        if self.crop is not None:
            result["crop"] = {
                "top":    self.crop.top,
                "bottom": self.crop.bottom,
                "left":   self.crop.left,
                "right":  self.crop.right,
            }
        return result

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "JobState":
        """Restore from a dict loaded from ``job.yaml``."""
        source_data = data["source"]
        source_data = {**source_data, "path": Path(source_data["path"])}
        source = VideoMetadata.model_validate_full(source_data)
        raw_crop = data.get("crop")
        crop = CropParams(**raw_crop) if isinstance(raw_crop, dict) else None
        return cls(source=source, crop=crop)


class ExtractionParams(BaseModel):
    """Phase parameter file model for extraction (``extraction.yaml``).

    Stores the include/exclude stream filter patterns that were active when
    extraction last ran.  Used to detect filter changes on subsequent runs
    and trigger re-extraction when they differ.
    """

    stream_filter: StreamFilterConfig
    include: str | None = None
    exclude: str | None = None

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        return {
            "include": self.include,
            "exclude": self.exclude,
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "ExtractionParams":
        """Restore from a dict loaded from ``extraction.yaml``."""
        return cls(
            include=data.get("include"),
            exclude=data.get("exclude"),
        )


class ChunkingParams(BaseModel):
    """Phase parameter file model for chunking (``chunking.yaml``).

    Stores detected scene boundaries.  Crop params are NOT stored here
    since chunking does not apply or depend on cropping.
    """

    scenes: list[SceneBoundary] = Field(default_factory=list)

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        return {
            "scenes": [
                {"frame": s.frame, "timestamp_seconds": s.timestamp_seconds}
                for s in self.scenes
            ]
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "ChunkingParams":
        """Restore from a dict loaded from ``chunking.yaml``."""
        scenes = [SceneBoundary(**s) for s in data.get("scenes", [])]
        return cls(scenes=scenes)


class OptimizationParams(BaseModel):
    """Phase parameter file model for optimization (``optimization.yaml``).

    Stores crop params active when optimization ran, selected test chunk IDs,
    and (once determined) the optimal strategy name.
    """

    crop:             CropParams | None = None
    test_chunks:      list[str]         = Field(default_factory=list)
    optimal_strategy: str | None        = None

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        d: dict = {"test_chunks": self.test_chunks}
        if self.crop is not None:
            d["crop"] = {"top": self.crop.top, "bottom": self.crop.bottom,
                         "left": self.crop.left, "right": self.crop.right}
        if self.optimal_strategy is not None:
            d["optimal_strategy"] = self.optimal_strategy
        return d

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "OptimizationParams":
        """Restore from a dict loaded from ``optimization.yaml``."""
        crop = CropParams(**data["crop"]) if data.get("crop") else None
        return cls(
            crop=crop,
            test_chunks=data.get("test_chunks", []),
            optimal_strategy=data.get("optimal_strategy"),
        )


class EncodingParams(BaseModel):
    """Phase parameter file model for encoding (``encoding.yaml``).

    Stores crop params active when encoding ran.
    """

    crop: CropParams | None = None

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        d: dict = {}
        if self.crop is not None:
            d["crop"] = {"top": self.crop.top, "bottom": self.crop.bottom,
                         "left": self.crop.left, "right": self.crop.right}
        return d

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "EncodingParams":
        """Restore from a dict loaded from ``encoding.yaml``."""
        crop = CropParams(**data["crop"]) if data.get("crop") else None
        return cls(crop=crop)


class MetricsSidecar(BaseModel):
    """Per-attempt metrics sidecar (``<attempt_stem>.yaml``).

    Stores ALL measured metric values — not filtered to current targets.
    ``targets_met`` is for human inspection only; the algorithm always
    re-evaluates pass/fail from ``metrics`` against current quality targets.
    """

    crf:         float
    targets_met: bool                # for human inspection only
    metrics:     dict[str, float]    # all measured values, e.g. vmaf_min, ssim_median

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        return {
            "crf":         self.crf,
            "targets_met": self.targets_met,
            "metrics":     self.metrics,
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "MetricsSidecar":
        """Restore from a dict loaded from an attempt sidecar YAML."""
        return cls(
            crf=float(data["crf"]),
            targets_met=bool(data["targets_met"]),
            metrics={k: float(v) for k, v in data.get("metrics", {}).items()},
        )


class EncodingResultSidecar(BaseModel):
    """Encoding result sidecar (``<chunk_id>.<res>.yaml``).

    Written when the CRF search for a ``(chunk_id, strategy)`` pair concludes.
    Its presence means the pair is ``COMPLETE``.  ``chunk_id`` and ``strategy``
    are derived from the filename and directory — not stored here.
    """

    winning_attempt: str              # filename of the winning attempt .mkv
    crf:             float
    quality_targets: list[str]        # targets active when this result was written
    metrics:         dict[str, float] # only the targeted metric values

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        return {
            "winning_attempt": self.winning_attempt,
            "crf":             self.crf,
            "quality_targets": self.quality_targets,
            "metrics":         self.metrics,
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "EncodingResultSidecar":
        """Restore from a dict loaded from an encoding result sidecar YAML."""
        return cls(
            winning_attempt=data["winning_attempt"],
            crf=float(data["crf"]),
            quality_targets=data.get("quality_targets", []),
            metrics={k: float(v) for k, v in data.get("metrics", {}).items()},
        )


class ChunkSidecar(BaseModel):
    """Chunk sidecar (``<chunk_stem>.yaml``).

    Written atomically alongside each chunk ``.mkv`` file.  Its presence
    (combined with the ``.tmp`` protocol) means the chunk was written
    successfully and its metadata is known without re-probing.

    ``chunk_id`` is NOT stored — it is derived from the filename stem.
    """

    chunk: ChunkMetadata

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict (excludes chunk_id and path)."""
        data = self.chunk.model_dump_full()
        # chunk_id and path are derived from the filename — omit from sidecar
        data.pop("chunk_id", None)
        data.pop("path", None)
        data.pop("crop_params", None)
        data.pop("pix_fmt", None)
        data.pop("file_size_bytes", None)
        return data

    @classmethod
    def from_yaml_dict(cls, data: dict, chunk_id: str, path: Path) -> "ChunkSidecar":
        """Restore from a dict loaded from a chunk sidecar YAML.

        Args:
            data:     Dict loaded from the sidecar YAML file.
            chunk_id: Chunk identifier derived from the filename stem.
            path:     Path to the chunk ``.mkv`` file.
        """
        chunk = ChunkMetadata.model_validate_full({
            **data,
            "chunk_id": chunk_id,
            "path":     path,
        })
        return cls(chunk=chunk)


# ---------------------------------------------------------------------------
# JobStateManager
# ---------------------------------------------------------------------------

_JOB_YAML_FILENAME          = "job.yaml"
_EXTRACTION_YAML_FILENAME   = "extraction.yaml"
_CHUNKING_YAML_FILENAME     = "chunking.yaml"
_OPTIMIZATION_YAML_FILENAME = "optimization.yaml"
_ENCODING_YAML_FILENAME     = "encoding.yaml"

# Phase parameter files that should be deleted on --force wipe
_PHASE_PARAM_FILENAMES: list[str] = [
    _EXTRACTION_YAML_FILENAME,
    _CHUNKING_YAML_FILENAME,
    _OPTIMIZATION_YAML_FILENAME,
    _ENCODING_YAML_FILENAME,
]


class JobStateManager:
    """Manages ``job.yaml`` and all phase parameter YAML files.

    Replaces ``ProgressTracker`` as the single source of truth for job and
    phase state.  Provides typed load/save methods for each YAML file; no
    generic string-keyed accessor is exposed.

    Args:
        work_dir:      Working directory where all YAML files are stored.
        source_video:  Path to the source video file for this job.
        force:         When ``True``, a source-file mismatch in execute mode
                       triggers a wipe-and-continue rather than a hard stop.
    """

    def __init__(
        self,
        work_dir:     Path,
        source_video: Path,
        force:        bool = False,
    ) -> None:
        self._work_dir     = work_dir
        self._source_video = source_video
        self._force        = force

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def work_dir(self) -> Path:
        """Working directory managed by this instance."""
        return self._work_dir

    # ------------------------------------------------------------------
    # job.yaml
    # ------------------------------------------------------------------

    def load_job(self) -> JobState | None:
        """Load ``job.yaml`` from the work directory.

        Returns:
            ``JobState`` if the file exists and is valid, ``None`` otherwise.
        """
        path = self._work_dir / _JOB_YAML_FILENAME
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return JobState.from_yaml_dict(data)
        except Exception as exc:
            _logger.warning("Could not load %s: %s", path, exc)
            return None

    def save_job(self, state: JobState) -> None:
        """Write ``job.yaml`` atomically.

        Creates the work directory if it does not yet exist.

        Args:
            state: Job state to persist.
        """
        self._work_dir.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(self._work_dir / _JOB_YAML_FILENAME, state.to_yaml_dict())
        _logger.debug("Saved job.yaml")

    def validate(self, dry_run: bool) -> bool:
        """Validate the source video against the persisted ``job.yaml``.

        Implements the dry-run / execute / ``--force`` source-binding logic
        (Requirement 1.2):

        - If ``job.yaml`` does not exist: nothing to validate; returns ``True``.
        - If all checked fields match: returns ``True``.
        - If any field differs:
          - **dry-run**: logs a warning, returns ``True`` (no action taken).
          - **execute without --force**: logs a critical message, returns ``False``.
          - **execute with --force**: logs a warning, wipes all intermediate
            artifacts and phase parameter files, returns ``True``.

        Args:
            dry_run: ``True`` when running in dry-run mode.

        Returns:
            ``True`` if the pipeline may proceed, ``False`` if it must stop.
        """
        existing = self.load_job()
        if existing is None:
            return True

        mismatches = self._find_source_mismatches(existing)
        if not mismatches:
            return True

        mismatch_desc = "; ".join(
            f"{field}: persisted={old!r}, current={new!r}"
            for field, old, new in mismatches
        )

        if dry_run:
            _logger.warning(
                "Source file mismatch detected (dry-run — no action taken): %s",
                mismatch_desc,
            )
            return True

        if self._force:
            _logger.warning(
                "Source file mismatch detected (--force — wiping artifacts and continuing): %s",
                mismatch_desc,
            )
            self._wipe_artifacts()
            return True

        _logger.critical(
            "Source file mismatch detected — stopping execution.  "
            "Re-run with --force to wipe existing artifacts and continue with the new source.  "
            "Mismatch: %s",
            mismatch_desc,
        )
        return False

    # ------------------------------------------------------------------
    # extraction.yaml
    # ------------------------------------------------------------------

    def load_extraction(self) -> "ExtractionParams | None":
        """Load ``extraction.yaml`` from the work directory.

        Returns:
            ``ExtractionParams`` if the file exists and is valid, ``None`` otherwise.
        """
        return self._load_phase_params(
            _EXTRACTION_YAML_FILENAME,
            ExtractionParams.from_yaml_dict,
        )

    def save_extraction(self, params: "ExtractionParams") -> None:
        """Write ``extraction.yaml`` atomically.

        Args:
            params: Extraction parameters to persist.
        """
        write_yaml_atomic(self._work_dir / _EXTRACTION_YAML_FILENAME, params.to_yaml_dict())
        _logger.debug("Saved extraction.yaml")

    # ------------------------------------------------------------------
    # chunking.yaml
    # ------------------------------------------------------------------

    def load_chunking(self) -> ChunkingParams | None:
        """Load ``chunking.yaml`` from the work directory.

        Returns:
            ``ChunkingParams`` if the file exists and is valid, ``None`` otherwise.
        """
        return self._load_phase_params(
            _CHUNKING_YAML_FILENAME,
            ChunkingParams.from_yaml_dict,
        )

    def save_chunking(self, params: ChunkingParams) -> None:
        """Write ``chunking.yaml`` atomically.

        Args:
            params: Chunking parameters to persist.
        """
        write_yaml_atomic(self._work_dir / _CHUNKING_YAML_FILENAME, params.to_yaml_dict())
        _logger.debug("Saved chunking.yaml")

    # ------------------------------------------------------------------
    # optimization.yaml
    # ------------------------------------------------------------------

    def load_optimization(self) -> OptimizationParams | None:
        """Load ``optimization.yaml`` from the work directory.

        Returns:
            ``OptimizationParams`` if the file exists and is valid, ``None`` otherwise.
        """
        return self._load_phase_params(
            _OPTIMIZATION_YAML_FILENAME,
            OptimizationParams.from_yaml_dict,
        )

    def save_optimization(self, params: OptimizationParams) -> None:
        """Write ``optimization.yaml`` atomically.

        Args:
            params: Optimization parameters to persist.
        """
        write_yaml_atomic(self._work_dir / _OPTIMIZATION_YAML_FILENAME, params.to_yaml_dict())
        _logger.debug("Saved optimization.yaml")

    # ------------------------------------------------------------------
    # encoding.yaml
    # ------------------------------------------------------------------

    def load_encoding(self) -> EncodingParams | None:
        """Load ``encoding.yaml`` from the work directory.

        Returns:
            ``EncodingParams`` if the file exists and is valid, ``None`` otherwise.
        """
        return self._load_phase_params(
            _ENCODING_YAML_FILENAME,
            EncodingParams.from_yaml_dict,
        )

    def save_encoding(self, params: EncodingParams) -> None:
        """Write ``encoding.yaml`` atomically.

        Args:
            params: Encoding parameters to persist.
        """
        write_yaml_atomic(self._work_dir / _ENCODING_YAML_FILENAME, params.to_yaml_dict())
        _logger.debug("Saved encoding.yaml")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_phase_params[T](
        self,
        filename: str,
        factory:  "type[T] | callable[[dict], T]",
    ) -> "T | None":
        """Generic helper to load a phase parameter YAML file.

        Args:
            filename: Filename within the work directory.
            factory:  Callable that accepts a dict and returns the typed model.

        Returns:
            Typed model instance, or ``None`` if the file is absent or invalid.
        """
        path = self._work_dir / filename
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return factory(data or {})
        except Exception as exc:
            _logger.warning("Could not load %s: %s", path, exc)
            return None

    def _find_source_mismatches(
        self,
        existing: JobState,
    ) -> list[tuple[str, object, object]]:
        """Compare persisted source metadata against the current source file.

        Checks path, file size, and resolution.  Duration and fps are not
        checked here because they require a probe call and are less reliable
        as identity signals.

        Args:
            existing: Previously persisted ``JobState``.

        Returns:
            List of ``(field_name, persisted_value, current_value)`` tuples
            for each field that differs.  Empty list means no mismatch.
        """
        mismatches: list[tuple[str, object, object]] = []
        persisted = existing.source

        # Path check
        if persisted.path.resolve() != self._source_video.resolve():
            mismatches.append(("path", str(persisted.path), str(self._source_video)))
            # If path changed, skip size/resolution checks — they're meaningless
            return mismatches

        # File size check (fast, no probe needed)
        try:
            current_size = self._source_video.stat().st_size
        except OSError:
            current_size = None

        if persisted._file_size_bytes is not None and current_size is not None:
            if persisted._file_size_bytes != current_size:
                mismatches.append(("file_size_bytes", persisted._file_size_bytes, current_size))

        # Resolution check (requires probe — only run if size matched)
        if not mismatches and persisted._resolution is not None:
            live_meta = VideoMetadata(path=self._source_video)
            current_res = live_meta.resolution
            if current_res is not None and current_res != persisted._resolution:
                mismatches.append(("resolution", persisted._resolution, current_res))

        return mismatches

    def _wipe_artifacts(self) -> None:
        """Delete all intermediate artifacts and phase parameter files.

        Called when ``--force`` is provided and a source mismatch is detected.
        Removes all subdirectories (extracted, chunks, encoded, audio, final)
        and all phase parameter YAML files.  ``job.yaml`` itself is NOT deleted
        here — it will be overwritten by the caller after validation.
        """
        # Delete phase parameter files
        for filename in _PHASE_PARAM_FILENAMES:
            path = self._work_dir / filename
            if path.exists():
                path.unlink()
                _logger.debug("Deleted phase parameter file: %s", path)

        # Delete intermediate artifact subdirectories
        artifact_dirs = ["extracted", "chunks", "encoded", "audio", "final"]
        for dirname in artifact_dirs:
            dir_path = self._work_dir / dirname
            if dir_path.exists():
                shutil.rmtree(dir_path)
                _logger.debug("Deleted artifact directory: %s", dir_path)

        _logger.info("Wiped all intermediate artifacts from %s", self._work_dir)
