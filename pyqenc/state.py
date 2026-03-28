"""Job state management and artifact classification for the pyqenc pipeline.

This module provides:

- ``ArtifactState`` — four-value enum classifying each artifact's recovery
  state (``ABSENT`` / ``ARTIFACT_ONLY`` / ``STALE`` / ``COMPLETE``).
- Data models: ``JobState``, ``ExtractionParams``, ``ChunkingParams``,
  ``OptimizationParams``, ``EncodingParams``, ``MetricsSidecar``,
  ``EncodingResultSidecar``, ``ChunkSidecar``.

Each model is self-sufficient: call ``Model.load(path)`` to load from a YAML
file and ``instance.save(path)`` to persist atomically.
"""
# CHerSun 2026

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, Field

from pyqenc.models import (
    ChunkMetadata,
    CropParams,
    SceneBoundary,
    Strategy,
    VideoMetadata,
)
from pyqenc.utils.yaml_utils import write_yaml_atomic

logger = logging.getLogger(__name__)


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
        STALE:         The artifact file and sidecar are present and internally
                       consistent, but the parameters under which they were
                       produced no longer match the current run parameters.
                       Recovery action: re-produce if still needed, or leave on
                       disk if cleanup level does not permit deletion.
        COMPLETE:      The artifact file is present and its sidecar is present
                       and contains all required data.  Recovery action: skip —
                       no work needed.
    """

    ABSENT        = "absent"
    ARTIFACT_ONLY = "artifact_only"
    STALE         = "stale"
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

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``JobState`` from *path*.

        Returns:
            ``JobState`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.from_yaml_dict(data)
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``JobState`` to *path* atomically.

        Creates parent directories as needed.

        Args:
            path: Destination YAML file path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(path, self.to_yaml_dict())
        logger.debug("Saved %s", path.name)


class ExtractionParams(BaseModel):
    """Phase parameter file model for extraction (``extraction.yaml``).

    Stores the include/exclude stream filter patterns that were active when
    extraction last ran.  Used to detect filter changes on subsequent runs
    and trigger re-extraction when they differ.
    """

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

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``ExtractionParams`` from *path*.

        Returns:
            ``ExtractionParams`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.from_yaml_dict(data or {})
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``ExtractionParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.to_yaml_dict())
        logger.debug("Saved %s", path.name)


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

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``ChunkingParams`` from *path*.

        Returns:
            ``ChunkingParams`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.from_yaml_dict(data or {})
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``ChunkingParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.to_yaml_dict())
        logger.debug("Saved %s", path.name)


class StrategyTestResult(BaseModel):
    """Per-strategy test result stored in ``optimization.yaml``.

    Attributes:
        strategy:    The encoding strategy that was tested.
        total_size:  Total encoded size across all test chunks in bytes.
        avg_crf:     Average CRF value used across test chunks.
    """

    strategy:   Strategy
    total_size: int
    avg_crf:    float

    model_config = {"arbitrary_types_allowed": True}

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        return {
            "strategy":   self.strategy.name,
            "total_size": self.total_size,
            "avg_crf":    self.avg_crf,
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "StrategyTestResult":
        """Restore from a dict loaded from ``optimization.yaml``."""
        return cls(
            strategy   = Strategy.from_name(data["strategy"]),
            total_size = int(data["total_size"]),
            avg_crf    = float(data["avg_crf"]),
        )


class OptimizationParams(BaseModel):
    """Phase parameter file model for optimization (``optimization.yaml``).

    Stores crop params active when optimization ran, selected test chunk IDs,
    per-strategy test results, the tolerance used, the selected strategies, and
    the quality targets active when the last run wrote this file.

    Attributes:
        crop:             Crop params active when optimization ran.
        test_chunks:      Chunk IDs used for test encodes.
        strategy_results: Per-strategy test results ordered by increasing total size.
        tolerance_pct:    Tolerance percentage used when ``selected`` was computed.
        selected:         Strategies selected as optimal at time of last run.
        quality_targets:  Quality targets active when test encodes ran, serialised as
                          ``"metric-statistic:value"`` strings (e.g. ``"vmaf-min:93.0"``).
                          Written in both optimization mode and all-strategies mode so
                          ``OptimizationPhase`` can detect target changes on the next run
                          regardless of mode.
    """

    model_config = {"arbitrary_types_allowed": True}

    crop:             CropParams | None        = None
    test_chunks:      list[str]                = Field(default_factory=list)
    strategy_results: list[StrategyTestResult] = Field(default_factory=list)
    tolerance_pct:    float                    = 0.0
    selected:         list[Strategy]           = Field(default_factory=list)
    quality_targets:  list[str]                = Field(default_factory=list)

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        d: dict = {
            "test_chunks":      self.test_chunks,
            "tolerance_pct":    self.tolerance_pct,
            "strategy_results": [r.to_yaml_dict() for r in self.strategy_results],
            "selected":         [s.name for s in self.selected],
            "quality_targets":  self.quality_targets,
        }
        if self.crop is not None:
            d["crop"] = {
                "top":    self.crop.top,
                "bottom": self.crop.bottom,
                "left":   self.crop.left,
                "right":  self.crop.right,
            }
        return d

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "OptimizationParams":
        """Restore from a dict loaded from ``optimization.yaml``."""
        crop = CropParams(**data["crop"]) if data.get("crop") else None
        strategy_results = [
            StrategyTestResult.from_yaml_dict(r)
            for r in data.get("strategy_results", [])
        ]
        selected = [
            Strategy.from_name(name)
            for name in data.get("selected", [])
        ]
        return cls(
            crop             = crop,
            test_chunks      = data.get("test_chunks", []),
            strategy_results = strategy_results,
            tolerance_pct    = float(data.get("tolerance_pct", 0.0)),
            selected         = selected,
            quality_targets  = data.get("quality_targets", []),
        )

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``OptimizationParams`` from *path*.

        Returns:
            ``OptimizationParams`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.from_yaml_dict(data or {})
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``OptimizationParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.to_yaml_dict())
        logger.debug("Saved %s", path.name)


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

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``EncodingParams`` from *path*.

        Returns:
            ``EncodingParams`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.from_yaml_dict(data or {})
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``EncodingParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.to_yaml_dict())
        logger.debug("Saved %s", path.name)


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

    Quality-target tracking is owned exclusively by ``OptimizationPhase`` via
    ``optimization.yaml``.  ``OptimizationPhase`` deletes stale result sidecars
    before ``EncodingPhase`` runs, so ``EncodingPhase._recover()`` simply sees
    ``ARTIFACT_ONLY`` pairs naturally when targets change.
    """

    winning_attempt: str              # filename of the winning attempt .mkv
    crf:             float
    metrics:         dict[str, float] # only the targeted metric values

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        return {
            "winning_attempt": self.winning_attempt,
            "crf":             self.crf,
            "metrics":         self.metrics,
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "EncodingResultSidecar":
        """Restore from a dict loaded from an encoding result sidecar YAML."""
        return cls(
            winning_attempt=data["winning_attempt"],
            crf=float(data["crf"]),
            metrics={k: float(v) for k, v in data.get("metrics", {}).items()},
        )


class AudioParams(BaseModel):
    """Phase parameter file model for audio processing (``audio.yaml``).

    Stores the codec and base bitrate that were active when audio processing
    last ran.  These are Type B config values — they affect the *content* of
    produced AAC files and must be tracked across runs so that a codec or
    bitrate change triggers re-processing.

    ``audio_convert`` (the convert filter) is intentionally excluded: it is a
    Type A input — ``AudioEngine.build_plan()`` with the current filter already
    defines the expected terminal outputs, so no cross-run tracking is needed.
    """

    audio_codec:        str | None = None
    audio_base_bitrate: str | None = None

    def to_yaml_dict(self) -> dict:
        """Serialise to a YAML-friendly dict."""
        return {
            "audio_codec":        self.audio_codec,
            "audio_base_bitrate": self.audio_base_bitrate,
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "AudioParams":
        """Restore from a dict loaded from ``audio.yaml``."""
        return cls(
            audio_codec        = data.get("audio_codec"),
            audio_base_bitrate = data.get("audio_base_bitrate"),
        )

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``AudioParams`` from *path*.

        Returns:
            ``AudioParams`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.from_yaml_dict(data or {})
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``AudioParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.to_yaml_dict())
        logger.debug("Saved %s", path.name)


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


