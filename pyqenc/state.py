"""Shared recovery-state primitives for the pyqenc pipeline.

Every phase sidecar model lives in its owning phase's module (Req 21);
this module keeps only the genuinely shared primitives:

- ``ArtifactState`` — three-value enum classifying each artifact's
  completeness (``ABSENT`` / ``PARTIAL`` / ``COMPLETE``).
"""
# CHerSun 2026

from enum import Enum

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
                  present but its sidecar is missing, or quality attempts exist
                  but no winning attempt has been finalised.
        COMPLETE: All of the artifact's expected components are present, so the
                  artifact is fully ready to be worked on by later stages
                  (sidecar present where applicable).
    """

    ABSENT   = "absent"
    PARTIAL  = "partial"    # renamed from ARTIFACT_ONLY
    COMPLETE = "complete"
