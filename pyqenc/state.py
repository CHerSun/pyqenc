"""Job state management and artifact classification for the pyqenc pipeline.

This module provides:

- ``ArtifactState`` — three-value enum classifying each artifact's
  completeness (``ABSENT`` / ``PARTIAL`` / ``COMPLETE``).
- Data models:
  ``OptimizationParams``, ``EncodingParams``, ``MetricsSidecar``,
  ``ProbeState``, ``EncodingResultSidecar``, ``MeasureSidecar``.

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
from pydantic import BaseModel, Field, model_serializer

from pyqenc.audio.chain import ResolvedChain, chain_signature
from pyqenc.models import (
    CropParams,
)
from pyqenc.stream_model import DecimalYaml, LongPathYaml
from pyqenc.utils.yaml_utils import write_yaml_atomic

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ArtifactState
# ---------------------------------------------------------------------------

class ArtifactState(Enum):
    """Completeness (readiness) of a single pipeline artifact.

    Completeness answers one question: are all expected components present, so
    the artifact is ready to be worked on by later stages?  It is orthogonal to
    selection — whether the current run *wants* the artifact — which lives on
    ``Artifact.wanted``, not here.

    Attributes:
        ABSENT:   The artifact's components are not present.  Either nothing has
                  been produced yet, or whatever exists is trivially
                  reproducible with no investment worth protecting.  There is no
                  separate state for cheaply reproducible leftovers — they are
                  simply ABSENT.  ``.tmp`` files are NOT PARTIAL — they are
                  transient crash remnants cleaned up at phase startup before
                  recovery runs, leaving the artifact ABSENT.
        PARTIAL:  A protected investment.  Expensive or valuable work is partly
                  done, but the artifact is NOT yet ready to be worked on by
                  later stages because a required component is missing; it is
                  kept to avoid discarding that investment and to allow
                  resuming.  Examples of this principle: the primary file is
                  present but its sidecar is missing, or CRF attempts exist but
                  no winning attempt has been finalised.
        COMPLETE: All of the artifact's expected components are present, so the
                  artifact is fully ready to be worked on by later stages
                  (sidecar present where applicable).
    """

    ABSENT   = "absent"
    PARTIAL  = "partial"    # renamed from ARTIFACT_ONLY
    COMPLETE = "complete"


# ---------------------------------------------------------------------------
# Phase parameter / sidecar data models
# ---------------------------------------------------------------------------

class ProbeState(BaseModel):
    """Sidecar model for ``probe.yaml`` (the slow facet).

    Written by ``ProbePhase`` after resolving frame count and crop.  Contains
    only the delta over the extraction inventory: ``frame_count`` and ``crop``.

    ``frame_count=0`` is the sentinel for "could not be determined" — no valid
    video has zero frames.  ``crop`` is non-optional: an empty
    :class:`CropParams` means "no crop" and the key is omitted from the file
    when empty (serialization compactness only); loading always materializes
    a concrete crop — ``None`` ("auto") never appears past config.
    """

    frame_count: int
    crop:        CropParams = CropParams()

    @model_serializer
    def _serialize(self) -> dict:
        """Dump with the crop key omitted when empty (compactness only)."""
        data: dict = {"frame_count": self.frame_count}
        if not self.crop.is_empty():
            data["crop"] = self.crop.model_dump()
        return data

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``ProbeState`` from *path* (an absent crop materializes empty).

        Returns:
            ``ProbeState`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.model_validate(data or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``ProbeState`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(path, self.model_dump(exclude_none=True))
        logger.debug("Saved %s", path.name)


class StrategyTestResult(BaseModel):
    """Per-strategy test result stored in ``optimization.yaml``.

    Attributes:
        strategy:    Display name of the strategy that was tested (e.g. ``'slow+h265-aq'``).
        total_size:  Total encoded size across all test chunks in bytes.
    """

    strategy:   str
    total_size: int


class OptimizationParams(BaseModel):
    """Phase parameter file model for optimization (``optimization.yaml``).

    Stores the probe state active when optimization ran, selected test chunk IDs,
    per-strategy test results, the tolerance used, the selected strategies,
    the quality targets, and the metrics sampling factor active when the last
    run wrote this file.

    Attributes:
        probe:            Probe state (crop + frame count) active when optimization ran.
        test_chunks:      Chunk IDs used for test encodes.
        strategy_results: Per-strategy test results ordered by increasing total size.
        tolerance_pct:    Tolerance percentage used when ``selected`` was computed.
        selected:         Strategies selected as optimal at time of last run.
        quality_targets:  Quality targets active when test encodes ran, serialised as
                          ``"metric-statistic:value"`` strings (e.g. ``"vmaf-min:93.0"``).
                          Written in both optimization mode and all-strategies mode so
                          ``OptimizationPhase`` can detect target changes on the next run
                          regardless of mode.
        sampling:          Frame subsampling factor used when test encodes ran.
                          ``None`` for files written before this field was added
                          (treated as unknown — no mismatch triggered).
    """

    probe:            ProbeState | None       = None
    test_chunks:      list[str]                = Field(default_factory=list)
    strategy_results: list[StrategyTestResult] = Field(default_factory=list)
    tolerance_pct:    float                    = 0.0
    selected:         list[str]                = Field(default_factory=list)
    quality_targets:  list[str]                = Field(default_factory=list)
    sampling:         int | None               = None

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
            return cls.model_validate(data or {})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``OptimizationParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.model_dump(exclude_none=True))
        logger.debug("Saved %s", path.name)


class EncodingParams(BaseModel):
    """Phase parameter file model for encoding (``encoding.yaml``).

    Stores probe state (crop + frame count) active when encoding ran.
    """

    probe: ProbeState | None = None

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
            return cls.model_validate(data or {})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``EncodingParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.model_dump(exclude_none=True))
        logger.debug("Saved %s", path.name)


class MetricsSidecar(BaseModel):
    """Per-attempt metrics sidecar (``<attempt_stem>.yaml``).

    Stores ALL measured metric values — not filtered to current targets.
    ``targets_met`` is for human inspection only; the algorithm always
    re-evaluates pass/fail from ``metrics`` against current quality targets.

    ``sampling`` records the frame subsampling factor used when the metrics
    were measured.  On recovery, if this differs from the current config the
    sidecar is treated as stale and the attempt is re-measured (without
    re-encoding).  ``None`` for sidecars written before this field was added
    — treated as unknown, no staleness check triggered.

    The ``metrics`` field uses the flat ``{metric_stat: value}`` format
    (e.g. ``vmaf_min``, ``ssim_median``) consistent with ``ChunkQualityStats``
    serialisation.
    """

    crf:         DecimalYaml         # exact string round-trip (no float drift)
    targets_met: bool                # for human inspection only
    sampling:    int | None = None   # subsampling factor used when metrics were measured
    metrics:     dict[str, float]    # all measured values, e.g. vmaf_min, ssim_median


class EncodingResultSidecar(BaseModel):
    """Encoding result sidecar (``<chunk_id>.<res>.yaml``).

    Written when the CRF search for a ``(chunk_id, strategy)`` pair concludes.
    Its presence means the pair is ``COMPLETE``.  ``chunk_id`` and ``strategy``
    are derived from the filename and directory — not stored here.

    Quality-target tracking is owned exclusively by ``OptimizationPhase`` via
    ``optimization.yaml``.  ``OptimizationPhase`` deletes stale result sidecars
    before ``EncodingPhase`` runs, so ``EncodingPhase._recover()`` simply sees
    ``PARTIAL`` pairs naturally when targets change.
    """

    winning_attempt: str                # filename of the winning attempt .mkv
    crf:             DecimalYaml        # exact string round-trip (no float drift)
    metrics:         dict[str, float]   # only the targeted metric values
    targets_met:     bool = True        # False when search exhausted without a passing attempt


class MeasureSidecar(BaseModel):
    """Standalone measure sidecar (``<target_stem>.yaml``).

    Written by the ``measure`` command alongside the quality graph and
    screenshots.  Records source/target paths, durations, crop parameters,
    the frame subsampling factor, and per-metric statistics.

    The ``metrics`` field uses the same flat ``{metric_stat: value}`` format
    as ``MetricsSidecar`` (e.g. ``vmaf_min``, ``ssim_median``), with all
    eight statistics: min, p05, p25, median, p75, p95, max, std.
    The YAML key for the sampling factor is ``sampling``.
    """

    source_video:               LongPathYaml
    target_video:               LongPathYaml
    source_duration_seconds:    float | None          = None
    target_duration_seconds:    float | None          = None
    effective_duration_seconds: float | None          = None
    sampling:                   int
    crop_params:                dict[str, int] | None = None
    metrics:                    dict[str, float]      = Field(default_factory=dict)


class AudioSidecar(BaseModel):
    """Sidecar model for the audio phase (``audio.yaml``).

    Records ONLY a compact, per-chain **signature** for each chain this work-dir
    is committed to, keyed by chain name. ``select`` is deliberately
    NOT persisted: selection is a pure function of the current extracted tracks
    plus the current ``select`` config, recomputed for free every run, so there
    is nothing to track across runs.

    Each signature is the canonical :func:`~pyqenc.audio.chain.chain_signature`
    string (``ResolvedChain.model_dump_json()``) — one deterministic, compact
    line per chain. The sidecar stores these strings verbatim and never
    reconstructs a :class:`~pyqenc.audio.chain.ResolvedChain` from them: that is
    the whole point of the compact form. Invalidation compares the CURRENT
    chain's signature against the persisted one for the same name (equality of
    strings), so the sidecar stays cheap and stable while duplicating none of the
    config's nested structure on disk.

    The sidecar records committed **intent**, decoupled from completion —
    completion is always read from the presence of output files on disk, never
    inferred from this sidecar. A differing or removed chain (detected
    by signature comparison) triggers invalidation of that chain's on-disk
    outputs.

    On-disk shape (``audio.yaml``)::

        chains:
          normal: '{"name":"normal","filters":[...],"encode":{...}}'
          night:  '{"name":"night","filters":[...],"encode":{...}}'

    Attributes:
        chains: Map of chain name → its canonical signature string.
    """

    chains: dict[str, str]

    @classmethod
    def from_resolved(cls, resolved: dict[str, ResolvedChain]) -> AudioSidecar:
        """Build an ``AudioSidecar`` from resolved chains, computing each signature.

        Each signature is the canonical :func:`~pyqenc.audio.chain.chain_signature`
        — the SAME canonical function the audio phase uses (DRY).

        Args:
            resolved: Map of chain name → :class:`ResolvedChain`.

        Returns:
            The sidecar holding one signature string per chain.
        """
        return cls(chains={name: chain_signature(chain) for name, chain in resolved.items()})

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``AudioSidecar`` from *path*.

        Signatures are read back as-is (they stay strings — never reconstructed
        into :class:`ResolvedChain` objects).

        Returns:
            ``AudioSidecar`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.model_validate(data or {})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``AudioSidecar`` to *path* atomically.

        Uses the ``.tmp``-then-rename protocol. Creates parent
        directories as needed.

        Args:
            path: Destination YAML file path.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(path, self.model_dump(exclude_none=True))
        logger.debug("Saved %s", path.name)


class MergeStrategySummary(BaseModel):
    """Per-strategy summary row persisted in ``merge.yaml``.

    Mirrors the data ``_log_merge_summary`` needs for each output file so the
    summary can be replayed on rerun without re-reading every per-output sidecar.

    Attributes:
        strategy_name:   Display name of the encoding strategy.
        output_path:     Absolute path to the merged output file.
        file_size_bytes: Size of the output file in bytes at time of merge.
        metrics:         Measured quality metrics keyed by ``"{metric}_{statistic}"``.
        targets_met:     Whether all quality targets were met.
    """

    strategy_name:   str
    output_path:     LongPathYaml
    file_size_bytes: int              = 0
    metrics:         dict[str, float] = Field(default_factory=dict)
    targets_met:     bool             = False


class MergeParams(BaseModel):
    """Phase parameter file model for merging (``merge.yaml``).

    Stores the quality targets and metrics sampling factor active when the
    last merge run wrote this file.  Used to detect changes on subsequent
    runs and delete stale merge artifacts (sidecar + output) so the merge
    phase re-runs with the new parameters.

    Also stores per-strategy summary rows so the merge summary table can be
    replayed on rerun without re-reading every per-output sidecar.

    Attributes:
        quality_targets:    Quality targets serialised as ``"metric-statistic:value"``
                            strings.  ``None`` / empty means no targets were configured.
        sampling:           Frame subsampling factor used during quality measurement.
                            ``None`` for files written before this field was added
                            (treated as unknown — no mismatch triggered).
        probe:              Probe state (crop + frame count) active when merge ran.
                            ``None`` for files written before this field was added
                            (treated as unknown — no mismatch triggered).
        source_stem:        Source video filename stem (without extension).
        source_size_bytes:  Size of the source video file in bytes; ``0`` if unknown.
        strategy_summaries: Per-strategy summary rows for summary replay on rerun.
    """

    quality_targets:    list[str]                  = Field(default_factory=list)
    sampling:           int | None                 = None
    probe:              ProbeState | None          = None
    source_stem:        str                        = ""
    source_size_bytes:  int                        = 0
    strategy_summaries: list[MergeStrategySummary] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> MergeParams | None:
        """Load ``MergeParams`` from *path*.

        Returns:
            ``MergeParams`` if the file exists and is valid, ``None`` otherwise.
        """
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return cls.model_validate(data or {})
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return None

    def save(self, path: Path) -> None:
        """Write this ``MergeParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        write_yaml_atomic(path, self.model_dump(exclude_none=True))
        logger.debug("Saved %s", path.name)


