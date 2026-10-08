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

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, Field, model_serializer

from pyqenc.models import (
    CropParams,
    Fingerprint,
)
from pyqenc.stream_model import DecimalYaml, LongPathYaml
from pyqenc.utils.yaml_utils import load_model, save_model

if TYPE_CHECKING:
    from pyqenc.phases.probe import ProbePhaseResult


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

class ProbeFacet(BaseModel):
    """The probe facet as a structured comparison key (spec Req 23/10c).

    Exactly the two fields downstream identity depends on — the source frame
    count and the crop values — compared field-wise, never by whole-model
    equality (human-facing provenance like ``crop_source`` must never leak
    into a key). ``frame_count=0`` is the unknown sentinel: unknown on
    either side contributes nothing (unknown-not-mismatch, Req 32).

    Attributes:
        frame_count: Frames of the source (0 = unknown).
        crop:        The committed crop values.
    """

    frame_count: int = 0
    crop:        CropParams = CropParams()

    @classmethod
    def from_probe(cls, probe_result: ProbePhaseResult) -> Self:
        """Snapshot the live facet from the probe phase's result."""
        frame_count = (
            probe_result.stream.payload.frame_count
            if probe_result.stream is not None else 0
        )
        return cls(frame_count=frame_count, crop=probe_result.crop)

    def matches(self, other: ProbeFacet) -> bool:
        """Field-wise facet comparison: frame count (0 = unknown) and crop."""
        if self.frame_count and other.frame_count and self.frame_count != other.frame_count:
            return False
        return self.crop == other.crop


class ProbeState(BaseModel):
    """Sidecar model for ``probe.yaml`` (the slow facet).

    Written by ``ProbePhase`` after resolving frame count and crop.  Contains
    only the delta over the extraction inventory: ``frame_count``, ``crop``,
    the crop's provenance, and the source identity key.

    ``frame_count=0`` is the sentinel for "could not be determined" — no valid
    video has zero frames.  ``crop`` is non-optional: an empty
    :class:`CropParams` means "no crop" and the key is omitted from the file
    when empty (serialization compactness only); loading always materializes
    a concrete crop — ``None`` ("auto") never appears past config.

    ``crop_source`` is HUMAN-FACING provenance (was the committed crop a
    manual override or a detection?) — it never participates in any
    comparison (Req 23); downstream facet keys compare the frame count and
    the crop values only. ``None`` for sidecars written before the field.

    ``source`` is the identity key (the fingerprint pair, no path — Req 31);
    ``None`` for legacy sidecars is unknown, never a mismatch (Req 32).
    """

    frame_count: int
    crop:        CropParams = CropParams()
    source:      Fingerprint | None = None
    crop_source: str | None         = None

    @model_serializer
    def _serialize(self) -> dict:
        """Dump with the crop key omitted when empty (compactness only)."""
        data: dict = {"frame_count": self.frame_count}
        if not self.crop.is_empty():
            data["crop"] = self.crop.model_dump()
        return data

    @classmethod
    def from_probe(cls, probe_result: ProbePhaseResult) -> Self:
        """Snapshot a probe phase result as the comparable ``ProbeState``.

        The single composition site for parameter-invalidations: phases that
        persist the active probe facet (``encoding.yaml``, ``optimization.yaml``,
        ``merge.yaml``) build their ``ProbeState`` from the live
        ``ProbePhaseResult`` here.  An unknown facet maps to the sentinels —
        frame count 0, empty crop.

        Args:
            probe_result: The probe phase's result (guaranteed present —
                          callers reach this only after the dependency walk).

        Returns:
            The snapshot.
        """
        frame_count = (
            probe_result.stream.payload.frame_count
            if probe_result.stream is not None else 0
        )
        return cls(frame_count=frame_count, crop=probe_result.crop)

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``ProbeState`` from *path* (an absent crop materializes empty).

        Returns:
            ``ProbeState`` if the file exists and is valid, ``None`` otherwise.
        """
        return load_model(path, cls)

    def save(self, path: Path) -> None:
        """Write this ``ProbeState`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        save_model(path, self)


class MetricsSidecar(BaseModel):
    """Per-attempt sidecar (``<chunk>.q<quality>.yaml`` — the attempt stem swap).

    Stores ALL measured metric values — not filtered to current targets.
    Facts of the attempt only: pass/fail against quality targets is a
    comparison with foreign state (which targets, which sampling) and is
    always re-evaluated from ``metrics`` where it is decided — never stored
    here (the WINNING attempt's conclusion is recorded on
    :class:`EncodingResultSidecar`). This is the re-judging substrate:
    cache-hits re-judge under new targets without re-encoding.

    ``resolution`` is the attempt's actual output dimensions (``'WxH'``) —
    a fact probed after the encode (the attempt's name carries no
    resolution, Req 9/9b); ``None`` for sidecars written before the field.

    ``sampling`` records the frame subsampling factor used when the metrics
    were measured.  On recovery, if this differs from the current config the
    sidecar is treated as stale and the attempt is re-measured (without
    re-encoding).  ``None`` for sidecars written before this field was added
    — treated as unknown, no staleness check triggered.

    The ``metrics`` field uses the flat ``{metric_stat: value}`` format
    (e.g. ``vmaf_min``, ``ssim_median``) consistent with ``ChunkQualityStats``
    serialisation.

    ``frame_count`` is a fact of the attempt file, from the encode run that
    produced it — measurement passes never change it (a re-measured attempt
    carries its previously persisted count over).  ``0`` is the sentinel for
    "could not be determined" (no valid video has zero frames): the attempt
    was re-measured with no prior count on record, or the sidecar predates
    the field.
    """

    crf:         DecimalYaml         # exact string round-trip (no float drift)
    resolution:  str | None = None   # actual output dimensions ("WxH")
    sampling:    int | None = None   # subsampling factor used when metrics were measured
    frame_count: int         = 0     # frames of the attempt file (0 = unknown)
    metrics:     dict[str, float]    # all measured values, e.g. vmaf_min, ssim_median


class EncodingResultSidecar(BaseModel):
    """Winner result sidecar (``<chunk_id>.yaml`` — the winner stem swap).

    Written when the quality search for a ``(chunk_id, strategy)`` pair
    concludes and the winning attempt is promoted to the static winner name.
    Its presence means the pair is ``COMPLETE``.  ``chunk_id`` and
    ``strategy`` are derived from the file name and directory — not stored
    here; the winning attempt's identity is fully derivable from the pair,
    so no back-pointer to the attempt is persisted (Req 25).

    Quality-target tracking is owned exclusively by ``OptimizationPhase`` via
    ``optimization.yaml``.  ``OptimizationPhase`` deletes stale result sidecars
    before ``EncodingPhase`` runs, so ``EncodingPhase._recover()`` simply sees
    ``PARTIAL`` pairs naturally when targets change.

    ``metrics`` carries the TARGETED subset only — the keys the judging
    targets read (full measured sets live on attempt sidecars, the re-judging
    substrate, and on merged-output sidecars; phase-sidecar summaries carry
    targeted metrics as well).

    ``frame_count`` is the winning attempt's frame count carried over from
    its attempt sidecar at finalize — the durable per-pair record the
    end-of-run scan sums into the frame-preservation invariant.  ``0`` is
    the "could not be determined" sentinel (the winner was accepted from a
    re-measured attempt with no count on record).

    ``crf`` and ``resolution`` are facts of this winner; recovery-time
    classification and composition never read them — only processing-path
    consumers (the re-merge CRF graph, the winner scan, the fixed ruler)
    may.
    """

    crf:         DecimalYaml        # exact string round-trip (no float drift)
    resolution:  str | None = None  # actual output dimensions ("WxH")
    metrics:     dict[str, float]   # the TARGETED metric subset
    frame_count: int         = 0    # frames of the winning attempt (0 = unknown)
    targets_met: bool        = True # False when search exhausted without a passing attempt


class MeasureSidecar(BaseModel):
    """Standalone measure sidecar (``<target_stem>.yaml``).

    Written by the ``measure`` command alongside the quality graph and
    screenshots.  Records source/target paths, durations, crop parameters,
    the frame subsampling factor, and per-metric statistics.

    The ``metrics`` field uses the same flat ``{metric_stat: value}`` format
    as ``MetricsSidecar`` (e.g. ``vmaf_min``, ``ssim_median``), with all
    ten statistics: min, p05, p10, p25, median, p75, p90, p95, max, std.
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

    Stores the run's merge invalidation keys plus the per-strategy summary
    rows so the merge summary table can be replayed on rerun without
    re-reading every per-output sidecar. Keys are mode-honest: search runs
    key on the configured quality targets; fixed runs key on the ruler basis
    (anchor identity) — config targets drive nothing in fixed mode, so a
    fixed merge is neither invalidated nor re-measured by their change.

    Replay-only facts (source stem/size) are deliberately NOT persisted:
    they render live from the JobPhase result on the fast-exit path, and
    persisting them beside the keys would break whole-model comparisons.

    Attributes:
        quality_targets:    Search-run key: quality targets serialised as
                            ``"metric-statistic:value"`` strings. Empty in
                            fixed runs (not a key there).
        sampling:           Frame subsampling factor used during quality
                            measurement. ``None`` for files written before
                            this field was added (treated as unknown — no
                            mismatch triggered).
        probe:              Probe state (crop + frame count) active when
                            merge ran. ``None`` for files written before this
                            field was added (treated as unknown — no mismatch
                            triggered).
        anchor:             Fixed-run key: the optimization anchor's display
                            name — the ruler basis. ``None`` in search runs.
        strategy_summaries: Per-strategy summary rows for summary replay on
                            rerun. Only the stats the summary table renders.
        source:             The source identity key (fingerprint pair, no
                            path) — a mismatch against the live source is the
                            phase-level catastrophic condition (Req 33/60);
                            ``None`` for legacy files is unknown, never a
                            mismatch (Req 32).
    """

    quality_targets:    list[str]                  = Field(default_factory=list)
    sampling:           int | None                 = None
    probe:              ProbeState | None          = None
    anchor:             str | None                 = None
    strategy_summaries: list[MergeStrategySummary] = Field(default_factory=list)
    source:             Fingerprint | None         = None

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``MergeParams`` from *path*.

        Returns:
            ``MergeParams`` if the file exists and is valid, ``None`` otherwise.
        """
        return load_model(path, cls)

    def save(self, path: Path) -> None:
        """Write this ``MergeParams`` to *path* atomically.

        Args:
            path: Destination YAML file path.
        """
        save_model(path, self)


