"""Shared recovery-state primitives for the pyqenc pipeline.

Every phase sidecar model lives in its owning phase's module (Req 21);
this module keeps only the genuinely shared primitives:

- ``ArtifactState`` — three-value enum classifying each artifact's
  completeness (``ABSENT`` / ``PARTIAL`` / ``COMPLETE``).
- ``ProbeFacet`` — the structured probe-facet comparison key shared by
  optimization and merge.
"""
# CHerSun 2026

from enum import Enum
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel

from pyqenc.models import (
    CropParams,
)

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


