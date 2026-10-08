"""
Merging phase for the quality-based encoding pipeline.

This module handles concatenation of encoded video chunks to produce the
merged MKV outputs.  It also measures final quality metrics and generates
visual plots for verification.

Audio muxing is intentionally omitted — the final output is video-only.
Audio delivery files are kept alongside the output for the user to mux
manually or in a downstream step.
"""
# CHerSun 2026

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pyqenc.constants import (
    FAILURE_SYMBOL_MINOR,
    MERGED_OUTPUT_DIR,
    METRIC_KEY_QUALITY_MEASURE,
    SUCCESS_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
    WARNING_SYMBOL,
)
from pyqenc.metrics import MetricKey
from pyqenc.models import (
    CropParams,
    EncodingPlan,
    Fingerprint,
    PhaseOutcome,
    QualityTarget,
    Strategy,
    id_set_fingerprint,
    identity_changed,
    targets_as_strings,
)
from pyqenc.phase import (
    Artifact,
    ArtifactState,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
    RecoveryError,
)
from pyqenc.phases.encoding import EncodingPhase, read_winner_sidecar
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.optimization import OptimizationPhase
from pyqenc.phases.probe import ProbePhase
from pyqenc.quality import QualitySearchBase, flatten_metric_stats
from pyqenc.state import ProbeFacet
from pyqenc.stream_model import (
    DecimalYaml,
    EncodedChunk,
    ExtendedVideoStream,
    File,
    LongPathYaml,
    MergedVideo,
)
from pyqenc.utils.ffmpeg_runner import FrameCountError, get_frame_count
from pyqenc.utils.fs import remove_stale_tmp_files, safe_stat_size
from pyqenc.utils.log_format import (
    fmt_key_value_table,
    fmt_metric_value,
    fmt_size_mb,
)
from pyqenc.utils.long_path import LongPath
from pyqenc.utils.visualization import QualityEvaluator, create_crf_plot
from pyqenc.utils.yaml_utils import load_model, save_model

# ---------------------------------------------------------------------------
# Sidecar models (re-homed, Req 21): per-output acceptance records + the
# merge.yaml replay aggregate
# ---------------------------------------------------------------------------

class MergedProvenance(BaseModel):
    """What exactly one merged output was produced from (Req 27).

    Every dimension is compared per-file at recovery (Req 49) except
    ``source`` — the one dimension that escalates to the phase-level
    catastrophic condition (Req 60). Field names follow the design layout:
    named for WHAT they identify, never for the fingerprint mechanism
    (Req 61) — ``strategy`` IS the strategy's resolved-args fingerprint;
    the strategy's name is already the output's own file name.

    Attributes:
        source:   The source content identity — ESCALATES on mismatch.
        strategy: The producing strategy's resolved-args fingerprint.
        mode:     The run mode tag (``fixed`` | ``search``).
        pinned:   Fixed runs: the strategy's quantized pinned quality.
        targets:  Search runs: the targets basis (serialised strings).
        anchor:   Fixed compared runs: the anchor (ruler) basis.
        probe:    The probe facet the concat was produced under.
        sampling: The measurement sampling factor.
        winners:  The winner-set fingerprint (count + hash over sorted ids).
    """

    model_config = ConfigDict(frozen=True)

    source:   Fingerprint
    strategy: Fingerprint
    mode:     Literal["fixed", "search"]
    pinned:   DecimalYaml | None = None
    targets:  list[str]          = Field(default_factory=list)
    anchor:   str | None         = None
    probe:    ProbeFacet
    sampling: int
    winners:  Fingerprint


class MergedOutputSidecar(BaseModel):
    """The per-output acceptance record — facts + provenance, no verdict (Req 27).

    Written next to each merged output (stem swap). Its presence PLUS a
    provenance match makes the output COMPLETE (Req 49); ``metrics`` is the
    FULL measured set in both modes (the re-judgeable end-user record —
    verdicts are always live, Req 51). The plot path derives from the stem
    and is never persisted.

    Attributes:
        frame_count: Measured frame count (``None`` = not determined).
        metrics:     The full measured metric set.
        provenance:  The production-basis record (acceptance compares it).
    """

    frame_count: int | None               = None
    metrics:     dict[str, float]         = Field(default_factory=dict)
    provenance:  MergedProvenance


class MergeSummaryRow(BaseModel):
    """One per-strategy row of ``merge.yaml``'s replay summary.

    Verdicts are never persisted (Req 51): the replay renders marks live
    from ``metrics`` against the current targets.

    Attributes:
        strategy_name:   Display name of the encoding strategy.
        output_path:     Absolute path to the merged output file.
        file_size_bytes: Size of the output file in bytes at time of merge.
        metrics:         The rendered metric subset keyed by ``"{metric}_{statistic}"``.
    """

    strategy_name:   str
    output_path:     LongPathYaml
    file_size_bytes: int              = 0
    metrics:         dict[str, float] = Field(default_factory=dict)


class MergeBasis(BaseModel):
    """The basis marker of the persisted summary (Req 28): what the table
    was rendered under.

    Attributes:
        mode:    The run mode tag.
        targets: Search runs: the targets the table judged against.
        anchor:  Fixed compared runs: the anchor basis.
    """

    mode:    Literal["fixed", "search"] = "search"
    targets: list[str]                  = Field(default_factory=list)
    anchor:  str | None                 = None


class MergeSidecar(BaseModel):
    """``merge.yaml`` — the summary-replay aggregate and its basis ONLY.

    The per-output sidecars are the acceptance records (Req 28): this file
    carries no identity and no invalidation keys; its absence costs summary
    replay only, never correctness (M-7 made structural).

    Attributes:
        summary: Per-strategy replay rows (empty until a processing pass).
        basis:   The marker of what the summary was rendered under.
    """

    summary: list[MergeSummaryRow] = Field(default_factory=list)
    basis:   MergeBasis | None     = None

    @classmethod
    def load(cls, path: Path) -> Self | None:
        """Load ``merge.yaml``; ``None`` when absent or unparseable."""
        return load_model(path, cls)

    def save(self, path: Path) -> None:
        """Write this sidecar to *path* atomically."""
        save_model(path, self)


if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Sidecar model
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _ProvenanceBuilder:
    """The current run's provenance template + the per-file acceptance rule.

    One builder per recovery; ``for_strategy`` composes the expected
    provenance of one output, ``acceptance_mismatch`` names the first
    recorded dimension that contradicts it (``None`` = accepted).
    """

    def __init__(
        self,
        source:   Fingerprint,
        probe:    ProbeFacet,
        plan:     EncodingPlan,
        anchor:   str | None,
        sampling: int,
        winners:  Fingerprint,
    ) -> None:
        self._source   = source
        self._probe    = probe
        self._plan     = plan
        self._anchor   = anchor
        self._sampling = sampling
        self._winners  = winners

    def for_strategy(self, strategy: Strategy) -> MergedProvenance:
        """Compose the expected provenance for one output."""
        codec = strategy.codec
        collapsed = codec.quality_better == codec.quality_worse
        if self._plan.fixed_quality:
            return MergedProvenance(
                source   = self._source,
                strategy = strategy.fingerprint,
                mode     = "fixed",
                pinned   = (
                    codec.quality_better.quantize(codec.quality_granularity)
                    if collapsed else None
                ),
                anchor   = self._anchor,
                probe    = self._probe,
                sampling = self._sampling,
                winners  = self._winners,
            )
        return MergedProvenance(
            source   = self._source,
            strategy = strategy.fingerprint,
            mode     = "search",
            targets  = targets_as_strings(self._plan.targets),
            probe    = self._probe,
            sampling = self._sampling,
            winners  = self._winners,
        )

    def acceptance_mismatch(
        self,
        recorded: MergedProvenance,
        strategy: Strategy,
    ) -> str | None:
        """The first recorded dimension contradicting the current inputs.

        Returns ``None`` when the output is accepted; the mismatching
        dimension's name otherwise (``"sampling"` re-measures in place;
        everything else re-merges — M-8). An unverifiable dimension counts
        as a mismatch (conservative).
        """
        expected = self.for_strategy(strategy)
        if not recorded.strategy.matches(expected.strategy):
            return "strategy fingerprint"
        if recorded.mode != expected.mode:
            return "mode"
        if recorded.mode == "fixed" and recorded.pinned != expected.pinned:
            return "pinned quality"
        if recorded.mode == "fixed" and recorded.anchor != expected.anchor:
            return "anchor"
        if recorded.mode == "search" and recorded.targets != expected.targets:
            return "targets"
        if not recorded.probe.matches(expected.probe):
            return "probe facet"
        # The winner-set fingerprint is compared as a whole (Req 11a):
        # count + hash over the sorted winner ids; unverifiable = mismatch.
        if not recorded.winners.matches(expected.winners):
            return "winner set"
        if recorded.sampling != expected.sampling:
            return "sampling"
        return None


# ---------------------------------------------------------------------------
# MergePhaseResult
# ---------------------------------------------------------------------------

@dataclass
class MergePhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying merge-specific payload.

    Attributes:
        merged: The merged-output rows — one ``Artifact[MergedVideo]`` per
                expected output; consumed by the runner as deliverables.
    """

    merged: list[Artifact[MergedVideo]] = field(default_factory=list)

    @property
    def output_paths(self) -> list[Path]:
        """Deliverable paths of the ``COMPLETE`` merged outputs.

        Every ``COMPLETE`` row's payload path is a pipeline output file (no
        directory sniffing).
        """
        return [
            row.payload.output_path
            for row in self.merged
            if row.state == ArtifactState.COMPLETE
        ]


# ---------------------------------------------------------------------------
# MergePhase
# ---------------------------------------------------------------------------

class MergePhase(Phase[MergePhaseResult]):
    """Phase object for final video merging.

    Owns artifact enumeration, recovery, execution, and logging for the merge
    phase. The uniform run footprint is inherited from :class:`Phase`.

    In pipeline mode encoded chunks are read directly from
    ``EncodingPhase.result`` without rescanning the filesystem.  In standalone
    mode the phase scans ``encoded/<strategy.safe_name()>/`` for each strategy.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "merge"
    SIDECAR_NAME = "merge.yaml"
    _NS_PER_SECOND       = 1_000_000_000
    _MKVPROPEDIT_VIDEO_TRACK = "track:v1"
    _pending_reasons: dict[str, str]
    """Classification-derived pending reason per strategy display name
    (``'sampling'`` re-measures in place; anything else re-merges — M-8).
    Assigned by ``_recover``; consumed by ``_execute``."""

    _OUTPUT_SUFFIX       = ".mkv"
    # Optimization is a declared (direct) dependency because the fixed-run
    # ruler and anchor flow from its result; it is transitively guaranteed
    # via Encoding anyway. Audio is NOT a dependency: merge is video-only and
    # never reads the audio result — `auto` schedules audio via its terminal
    # position, not via this tuple.
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (
        JobPhase, ExtractionPhase, ProbePhase, OptimizationPhase,
        EncodingPhase,
    )
    _METRIC_KEY: MetricKey = MetricKey.MERGE

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry,
        *,
        collector: MetricsCollector,
    ) -> None:
        """Store the shared constructor state and the per-run stash."""
        super().__init__(config, phases, collector=collector)
        self._pending_reasons: dict[str, str] = {}

    def _log_key_params(self) -> None:
        """Log the source stem and quality targets (key parameters)."""
        logger.info("Source stem:  %s", self._deps[JobPhase].source.stem)
        plan = self._deps[ProbePhase].plan

        if plan.targets:
            logger.info("Targets:      %s", ", ".join(
                f"{t.metric}-{t.statistic}≥{t.value}" for t in plan.targets
            ))

    def _post_dependency_check(self) -> MergePhaseResult | None:
        """Encoding-completeness guard: refuse to merge incomplete encodes.

        Every encoded artifact must be COMPLETE before merging, even though
        the phase itself reports is_complete. This only triggers when encoding
        reported is_complete (COMPLETED / REUSED) yet still holds incomplete
        encoded artifacts — a genuine inconsistency; failed/pending encodes
        are already handled by the shared dependency walk.

        Returns:
            A typed ``FAILED`` result, or ``None`` when the phase may proceed.
        """
        incomplete = [
            a for a in self._deps[EncodingPhase].winners
            if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)
        ]
        if incomplete:
            err = f"EncodingPhase has {len(incomplete)} incomplete artifact(s) — cannot merge"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        return None

    def _invalidate(self) -> None:
        """The ONE deliverable-layer invalidation: the identity condition.

        Reads the PER-OUTPUT records (they are the acceptance keys —
        ``merge.yaml`` carries no identity; an empty ``merged/`` carries no
        identity and nothing to invalidate, Req 27/60/M-7): any output's
        recorded source identity contradicting the live source is
        catastrophic — fatal without the ``--force`` permission; with it,
        wipe ``merged/`` and ``merge.yaml``. Every other production-basis
        change is a per-file provenance mismatch handled by the
        classification (re-merge in place — deliverables are retained until
        replaced, Req 60).

        Raises:
            RecoveryError: On an identity mismatch without ``--force``.
        """
        job_result = self._deps[JobPhase]
        merged_dir = job_result.work_dir / MERGED_OUTPUT_DIR
        merge_yaml = job_result.work_dir / MergePhase.SIDECAR_NAME

        merged_dir.mkdir(parents=True, exist_ok=True)
        for existing in sorted(merged_dir.glob("*.yaml")):
            record = MergePhase._load_merge_sidecar(existing)
            if record is not None and identity_changed(
                record.provenance.source, job_result.source_fingerprint,
            ):
                if not job_result.force:
                    raise RecoveryError(
                        "Source content identity mismatch (a merged output's "
                        "provenance) — the deliverables belong to a different source.  "
                        "Re-run with --force to grant permission to wipe merged/ and "
                        "re-merge the new source."
                    )
                logger.warning(
                    "Source identity mismatch (--force granted — wiping merged/ "
                    "and merge.yaml)"
                )
                shutil.rmtree(merged_dir, ignore_errors=True)
                merge_yaml.unlink(missing_ok=True)
                return

    def _recover(self) -> Recovery:
        """Per-file acceptance over the merged outputs (Req 49).

        Steps:
        1. Identity key (Req 27/60): any output's recorded source identity
           contradicting the live source is the ONE deliverable-layer
           catastrophic condition — fatal without the ``--force`` permission;
           with it, wipe ``merged/`` and ``merge.yaml``.
        2. Clean up leftover ``.tmp`` files.
        3. Determine expected strategies from the encoding winners.
        4. Per-file acceptance: an output is COMPLETE only when the file is
           present AND its sidecar's provenance matches the current inputs
           on every recorded dimension (strategy fingerprint, mode/q or
           targets/anchor basis, probe facet, sampling, winner-set
           fingerprint); any mismatch or an unverifiable winner-set means
           re-merge — never a silent acceptance, never a re-measure of a
           stale concat (M-8: a targets/anchor change always re-searched
           winners upstream). A sampling-ONLY difference re-measures in
           place (the concat is current). File present without a sidecar is
           PARTIAL (re-merge in place — a crash leaves old outputs present).
        5. Output files not matching any expected strategy surface as
           ``wanted=False`` rows (retained deliverables; deletion only via
           explicit cleanup).
        """

        job_result = self._deps[JobPhase]
        work_dir   = job_result.work_dir
        merged_dir = work_dir / MERGED_OUTPUT_DIR

        # The identity scan lives in _invalidate (disk effects); the
        # classification below reads post-invalidation disk truth.
        merged_dir.mkdir(parents=True, exist_ok=True)
        remove_stale_tmp_files(merged_dir)

        # Expected strategies from the typed winners field — the
        # already-cached EncodingPhase winners, distinct by safe name, in
        # first-seen order. Two DISTINCT strategies claiming one safe name
        # would consume one output name — a naming collision, failed loudly
        # at the site (Req 5); the identical strategy appearing twice is one
        # expected output (deduplicated).
        winners = self._deps[EncodingPhase].winners
        seen: dict[str, Strategy] = {}
        for row in winners:
            strategy = row.payload.strategy
            existing = seen.get(strategy.safe_name())
            if existing is not None and existing != strategy:
                raise RecoveryError(
                    f"Naming collision: merged output name for "
                    f"'{strategy.safe_name()}' is claimed by two distinct "
                    f"strategies ({existing.display_name()} and "
                    f"{strategy.display_name()})."
                )
            seen[strategy.safe_name()] = strategy
        strategies = list(seen.values())
        if not strategies:
            return Recovery()

        source_stem = job_result.source.stem

        # Per-file acceptance (Req 49): the current provenance each output
        # must match. The winner-set fingerprint derives live from the
        # encoding winners (in memory — no disk reads).
        current_provenance = self._current_provenance_builder()
        self._pending_reasons = {}

        rows: list[Artifact] = []
        expected_names: set[str] = set()
        for strategy in strategies:
            output_file = merged_dir / MergedVideo.output_file_name(source_stem, strategy)
            expected_names.add(output_file.name)
            record = MergePhase._load_merge_sidecar(output_file)

            if output_file.exists() and record is not None:
                reason = current_provenance.acceptance_mismatch(
                    record.provenance, strategy,
                )
                if reason is None:
                    # COMPLETE — accepted: facts load for the live-verdict
                    # summary; the verdict itself is never persisted (Req 51).
                    metrics = {k: float(v) for k, v in record.metrics.items()}
                    rows.append(Artifact(
                        payload = MergedVideo(
                            source_stem = source_stem,
                            strategy    = strategy,
                            output_path = LongPath(output_file),
                            frame_count = record.frame_count,
                            metrics     = metrics,
                            targets_met = self._live_verdict(metrics),
                            plot_path   = LongPath(output_file.with_suffix(".png"))
                            if output_file.with_suffix(".png").exists() else None,
                        ),
                        state   = ArtifactState.COMPLETE,
                    ))
                    continue
                # Not accepted. A sampling-only difference keeps the concat
                # (re-measure in place); anything else re-merges (M-8).
                self._pending_reasons[strategy.display_name()] = reason
                logger.info(
                    "Merged output %s not accepted (%s) — %s",
                    output_file.name, reason,
                    "re-measuring" if reason == "sampling" else "re-merging",
                )

            state = (
                ArtifactState.PARTIAL if output_file.exists()
                else ArtifactState.ABSENT
            )
            rows.append(Artifact(
                payload = MergedVideo(
                    source_stem = source_stem,
                    strategy    = strategy,
                    output_path = LongPath(output_file),
                ),
                state   = state,
            ))
            if state is not ArtifactState.PARTIAL:
                self._pending_reasons[strategy.display_name()] = "absent"

        # Surface present-but-unwanted surplus outputs (a strategy absent from
        # the selection whose merged file still exists — its Strategy object
        # is gone, so the on-disk product itself, a File, is the payload).
        # Retained in place, never pending; deletion only via explicit
        # cleanup.
        if merged_dir.exists():
            for output_file in sorted(merged_dir.glob(f"*{MergePhase._OUTPUT_SUFFIX}")):
                if output_file.name in expected_names:
                    continue
                state = (
                    ArtifactState.COMPLETE
                    if MergePhase._load_merge_sidecar(output_file) is not None
                    else ArtifactState.PARTIAL
                )
                rows.append(Artifact(
                    payload = File(path=LongPath(output_file), file_size_bytes=safe_stat_size(output_file)),
                    state   = state,
                    wanted  = False,
                ))
                logger.debug("Merged output %s is surplus (strategy no longer selected) — unwanted", output_file.name)

        return Recovery.from_artifacts(rows)

    def _live_verdict(self, metrics: dict[str, float]) -> bool:
        """The live pass/fail against the CURRENT presentation targets (Req 51).

        Fixed runs without a ruler (uncompared) carry no verdict basis —
        ``True`` with empty targets is the vacuous truth.
        """
        plan = self._deps[ProbePhase].plan
        bar = (
            self._deps[OptimizationPhase].synthetic_targets
            if plan.fixed_quality else plan.targets
        )
        return not QualitySearchBase.failed_targets(metrics, bar)

    def _current_provenance_builder(self) -> _ProvenanceBuilder:
        """The current run's provenance template for per-file acceptance."""
        return _ProvenanceBuilder(
            source    = self._deps[JobPhase].source_fingerprint,
            probe     = ProbeFacet.from_probe(self._deps[ProbePhase]),
            plan      = self._deps[ProbePhase].plan,
            anchor    = self._deps[OptimizationPhase].anchor,
            sampling  = self._config.measurement.sampling,
            winners   = id_set_fingerprint(
                row.payload.chunk.safe_name()
                for row in self._deps[EncodingPhase].winners
            ),
        )

    def _summary_rendered_keys(self) -> set[str]:
        """Metric keys the summary table renders — what summaries persist.

        The current renderer judges and displays the configured quality
        targets in both modes (the anchor-relative table is the unified-
        summaries spec's work), so the rendered set is the target-key set.
        """
        plan = self._deps[ProbePhase].plan

        return {
            f"{t.metric}_{t.statistic}"
            for t in plan.targets
        }

    def _reused_result(self, wanted: list[Artifact], message: str) -> MergePhaseResult:
        """Build the reused result, replaying the persisted merge summary."""
        job_result = self._deps[JobPhase]
        plan = self._deps[ProbePhase].plan
        merge_yaml = job_result.work_dir / MergePhase.SIDECAR_NAME
        persisted  = MergeSidecar.load(merge_yaml)
        if persisted is not None:
            logger.info(THICK_LINE)
            logger.info("MERGE SUMMARY")
            logger.info(THICK_LINE)
            MergePhase._log_merge_summary_from_sidecar(
                sidecar           = persisted,
                quality_targets   = plan.targets,
                source_stem       = job_result.source.stem,
                source_size_bytes = safe_stat_size(job_result.source) or 0,
                metrics_sampling  = self._config.measurement.sampling,
            )
        return self._make_result(PhaseOutcome.REUSED, wanted, message)

    def _make_result(
        self,
        outcome:   PhaseOutcome,
        artifacts: list[Artifact[MergedVideo]],
        message:   str,
    ) -> MergePhaseResult:
        """Assemble a ``MergePhaseResult`` from the merged-output rows.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted merged-output rows.
            message:   Human-readable summary — on ``FAILED``, the error
                       description (count plus identifiers).

        Returns:
            The populated result (``merged`` is the single storage).
        """
        return MergePhaseResult(
            outcome   = outcome,
            message   = message,
            merged    = artifacts,
        )

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def _execute(
        self,
        wanted:  list[Artifact[MergedVideo]],
        dry_run: bool,
    ) -> MergePhaseResult:
        """Merge pending strategies by concatenating encoded chunks.

        The top-level ``merge`` span belongs to the template; the dotted
        ``merge.concat`` / ``merge.quality_measure`` spans are recorded around
        the individual sub-actions below. ``dry_run`` is never ``True`` here
        (merge is not a readonly-execute phase; the template previews
        instead).

        Every pending row re-merges from the current winners — ABSENT,
        provenance-mismatched, and the sidecar-less PARTIAL alike (the
        unverifiable case, Req 49/50) — EXCEPT the sampling-only difference,
        which keeps the concat and re-measures in place.

        Args:
            wanted:  Wanted merged-output rows from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``MergePhaseResult`` after merging.
        """

        rows = wanted
        work_dir   = self._deps[JobPhase].work_dir
        merged_dir = work_dir / MERGED_OUTPUT_DIR
        merged_dir.mkdir(parents=True, exist_ok=True)

        job_result = self._deps[JobPhase]
        probe_result = self._deps[ProbePhase]
        plan = probe_result.plan
        crop: CropParams = probe_result.crop
        # The dependency walk guarantees a completed probe with a resolved stream.
        assert probe_result.stream is not None, "probe guaranteed complete by the dependency walk"
        source_stream: ExtendedVideoStream = probe_result.stream.payload
        source_frame_count: int            = probe_result.stream.payload.frame_count
        source_stem = job_result.source.stem

        # Build encoded_chunks dict from EncodingPhase result
        encoded_chunks = self._collect_encoded_chunks()
        provenance_builder = self._current_provenance_builder()

        final_rows: list[Artifact[MergedVideo]] = []
        failed_strategies: list[str] = []

        for artifact in rows:
            payload      = artifact.payload
            strategy     = payload.strategy
            strategy_name = strategy.display_name()

            if artifact.state == ArtifactState.COMPLETE:
                final_rows.append(artifact)
                continue

            # The output name derives at the entity's single owning site.
            output_file = merged_dir / MergedVideo.output_file_name(source_stem, strategy)
            assert output_file == payload.output_path, "recovery derived the same location"

            try:
                reason = self._pending_reasons.get(strategy_name)
                if reason == "sampling":
                    # Sampling-only difference (Req 49): the concat is
                    # current — re-measure in place, never re-merge.
                    logger.info("Re-measuring (sampling changed): %s", strategy_name)
                elif not self._concat_and_promote(
                    strategy        = strategy,
                    output_file     = output_file,
                    source_stream   = source_stream,
                    encoded_chunks  = encoded_chunks,
                ):
                    # Everything else re-merges — ABSENT rows, provenance
                    # mismatches (M-8: a targets/anchor change re-searched
                    # winners upstream; the old concat is never current),
                    # and the UNVERIFIABLE case (file present, sidecar
                    # missing — Req 49/50): nothing vouches for what
                    # produced the file, so it is re-concatenated from the
                    # current winners in place; a crash leaves the old
                    # output present until the promotion rename replaces it.
                    failed_strategies.append(strategy_name)
                    continue

                # Verify frame count
                frame_count:       int | None       = None
                frame_count_ok:    bool             = False
                try:
                    frame_count = get_frame_count(output_file)
                    if source_frame_count > 0:
                        if frame_count != source_frame_count:
                            diff = frame_count - source_frame_count
                            logger.warning(
                                "  Frame count mismatch: expected %d, got %d (%+d)",
                                source_frame_count, frame_count, diff,
                            )
                        else:
                            frame_count_ok = True
                except (OSError, FrameCountError) as exc:
                    logger.warning("  Could not verify frame count: %s", exc)

                # Measure quality UNCONDITIONALLY (Req 52): measurement needs
                # the file, the reference, and the sampling — no target set
                # gates or parameterizes it. Verdicts are live (Req 51).
                metrics_dict: dict[str, float] = {}
                plot_path:    Path | None       = None
                try:
                    with self._collector.time(MetricKey.MERGE, METRIC_KEY_QUALITY_MEASURE):
                        metrics_dict, plot_path = self._measure_quality(
                            final_result  = output_file,
                            source_stream = source_stream,
                            ref_crop      = crop,
                            output_dir    = merged_dir,
                        )
                except (OSError, ValueError) as exc:
                    logger.warning("  Could not measure quality: %s", exc)

                # CRF distribution plot — reads the typed winners field
                crf_data = MergePhase._collect_crf_data(
                    self._deps[JobPhase].work_dir,
                    self._deps[EncodingPhase].winners,
                    strategy_name,
                )
                if crf_data:
                    crf_plot_path = merged_dir / f"{output_file.stem}.crf.png"
                    try:
                        qlabel = self._deps[EncodingPhase].quality_labels.get(strategy_name, "CRF")
                        create_crf_plot(
                            chunks        = crf_data,
                            output_path   = crf_plot_path,
                            title         = f"{qlabel}\n{output_file.stem.replace(TIME_SEPARATOR_MS, ".").replace(TIME_SEPARATOR_SAFE, ":")}",
                            quality_label = qlabel,
                        )
                        logger.debug("  CRF plot saved: %s", crf_plot_path.name)
                    except (OSError, ValueError) as exc:
                        logger.warning("  Could not generate CRF plot: %s", exc)
                else:
                    logger.warning("No CRF data available for strategy %s — skipping CRF plot", strategy_name)

                # Write the per-output acceptance record (facts + provenance,
                # no verdict — Req 27).
                self._write_merge_sidecar(
                    output_file = output_file,
                    strategy    = strategy,
                    frame_count = frame_count,
                    all_metrics = metrics_dict,
                    provenance  = provenance_builder.for_strategy(strategy),
                )

                targets_met = self._live_verdict(metrics_dict)
                frames_sym  = SUCCESS_SYMBOL_MINOR if frame_count_ok else FAILURE_SYMBOL_MINOR
                frames_str  = str(frame_count) if frame_count is not None else "unknown"
                metrics_str = self._fmt_inline_metrics(metrics_dict)
                logger.info(
                    "%s Merged %s:  frames=%s %s%s",
                    SUCCESS_SYMBOL_MAJOR, strategy_name, frames_str, frames_sym,
                    f"  {metrics_str}" if metrics_str else "",
                )
                if metrics_dict and not targets_met:
                    self._log_missed_targets_warning(
                        plan.targets,
                        fixed_quality = plan.fixed_quality,
                        strategy_name = strategy_name,
                        metrics_dict  = metrics_dict,
                    )

                final_rows.append(Artifact(
                    payload = MergedVideo(
                        source_stem = source_stem,
                        strategy    = strategy,
                        output_path = LongPath(output_file),
                        frame_count = frame_count,
                        metrics     = metrics_dict,
                        targets_met = targets_met,
                        plot_path   = LongPath(plot_path) if plot_path is not None else None,
                    ),
                    state   = ArtifactState.COMPLETE,
                ))

            except Exception:  # one bad strategy must not kill the rest
                logger.exception("Merging strategy %s error", strategy_name)
                failed_strategies.append(strategy_name)

        # Phase completion summary
        complete_count = sum(1 for a in final_rows if a.state == ArtifactState.COMPLETE)
        logger.info(THICK_LINE)
        logger.info("MERGE SUMMARY")
        logger.info(THICK_LINE)
        if failed_strategies:
            logger.error("  Failed strategies: %s", ", ".join(failed_strategies))
        rendered_keys = self._summary_rendered_keys()
        _, strategy_summaries = MergePhase._build_strategy_summaries(
            final_rows,
            source_stream.stream.file.path,
            rendered_keys,
        )
        MergePhase._log_merge_summary(
            summaries         = strategy_summaries,
            source_stem       = source_stem,
            source_size_bytes = safe_stat_size(source_stream.stream.file.path) or 0,
            quality_targets   = plan.targets,
            metrics_sampling  = self._config.measurement.sampling,
        )
        if failed_strategies and not final_rows:
            return self._make_result(PhaseOutcome.FAILED, [], "All strategy merges failed")

        # Persist merge.yaml: the summary replay aggregate + its basis marker
        # ONLY (Req 28) — the per-output sidecars are the acceptance records.
        if complete_count > 0:
            _, strategy_summaries = MergePhase._build_strategy_summaries(
                final_rows,
                source_stream.stream.file.path,
                rendered_keys,
            )
            MergeSidecar(
                summary = strategy_summaries,
                basis   = MergeBasis(
                    mode    = "fixed" if plan.fixed_quality else "search",
                    targets = [] if plan.fixed_quality else targets_as_strings(plan.targets),
                    anchor  = self._deps[OptimizationPhase].anchor if plan.fixed_quality else None,
                ),
            ).save(self._deps[JobPhase].work_dir / MergePhase.SIDECAR_NAME)

        if failed_strategies:
            return self._make_result(
                PhaseOutcome.FAILED, final_rows,
                f"{len(failed_strategies)} strategy(ies) failed: {', '.join(failed_strategies[:5])}",
            )

        did_work = any(a.state == ArtifactState.COMPLETE for a in final_rows)
        return self._make_result(
            PhaseOutcome.COMPLETED if did_work else PhaseOutcome.REUSED,
            final_rows,
            f"{complete_count} output file(s) complete",
        )

    def _concat_and_promote(
        self,
        strategy:       Strategy,
        output_file:    Path,
        source_stream:  ExtendedVideoStream,
        encoded_chunks: dict[str, list[EncodedChunk]],
    ) -> bool:
        """Concatenate one strategy's encoded chunks into *output_file*.

        The production path for an ABSENT output: chunk collection, mkvmerge
        (via an options file, writing the ``.tmp`` twin), the mkvpropedit
        frame-rate header patch, and the promotion rename to the final name.
        Failures are logged here and reported as ``False`` for the caller to
        record the strategy as failed.

        Args:
            strategy:       The strategy being merged; keys the
                            ``encoded_chunks`` rows by display name, owns the
                            options-file name via safe name.
            output_file:    The final output location; the tmp twin derives.
            source_stream:  The source stream (its true fps feeds propedit).
            encoded_chunks: Encoding winners grouped by strategy display name.

        Returns:
            ``True`` when *output_file* is ready at its final name.
        """
        strategy_name = strategy.display_name()
        logger.info("Merging: %s", strategy_name)

        # Collect the strategy's winners in timeline order — the start
        # timestamp is the quantity itself (sorting on the formatted file
        # name would depend on zero-padded rendering).
        strategy_chunks: list[Path] = [
            winner.stream.stream.file.path
            for winner in sorted(
                encoded_chunks.get(strategy_name, []),
                key=lambda w: w.chunk.start_timestamp,
            )
        ]

        if not strategy_chunks:
            logger.error("No encoded chunks found for strategy %s — skipping", strategy_name)
            return False

        logger.info("  Starting concatenation of %d chunks...", len(strategy_chunks))

        # Resolve timestamps path from ExtractionPhase result
        timestamps_path: Path | None = (
            self._deps[ExtractionPhase].timestamps_path
        )

        if timestamps_path is None or not timestamps_path.exists():
            logger.critical(
                "timestamps.txt not found — cannot restore PTS. "
                "Re-run the extraction phase to generate it."
            )
            return False

        merged_dir = output_file.parent
        tmp_output = MergePhase._tmp_output_path(output_file)

        # Write mkvmerge options file
        options_file = merged_dir / f"concat_{strategy.safe_name()}.json"
        args = MergePhase._build_mkvmerge_options(strategy_chunks, tmp_output, timestamps_path)
        MergePhase._write_mkvmerge_options_file(options_file, args)

        # Run mkvmerge via options file (avoids OS command-line length limits).
        # The "@<file>" option embeds the path in a sub-string mkvmerge
        # parses itself — plain form only, no extended-length prefix.
        cmd_mkvmerge: list[str | os.PathLike] = ["mkvmerge", f"@{options_file}"]
        logger.debug("mkvmerge command: %s", " ".join(str(a) for a in cmd_mkvmerge))

        with self._collector.time(MetricKey.MERGE, "concat"):
            mkvmerge_result = subprocess.run(
                cmd_mkvmerge, capture_output=True, text=True, check=False,
            )

        if mkvmerge_result.returncode != 0:
            logger.error(
                "mkvmerge failed for strategy %s (exit %d)",
                strategy_name, mkvmerge_result.returncode,
            )
            for line in mkvmerge_result.stderr.splitlines()[-20:]:
                logger.error("mkvmerge stderr: %s", line)
            # Leave options file on disk for debugging
            return False

        # Delete options file on success
        options_file.unlink(missing_ok=True)

        logger.debug("  Concatenation complete: %s", tmp_output.name)

        # Restore the true frame rate in the track header —
        # see _build_mkvpropedit_args for why mkvmerge cannot do it.
        fps = source_stream.stream.info.fps_fraction
        assert fps is not None, "a probed video stream reaching merge carries fps"
        propedit_cmd: list[str | os.PathLike] = MergePhase._build_mkvpropedit_args(
            tmp_output, fps,
        )
        propedit_result = subprocess.run(propedit_cmd, capture_output=True, text=True, check=False)
        if propedit_result.returncode != 0:
            logger.error(
                "mkvpropedit failed for strategy %s (exit %d) — frame-rate header not restored",
                strategy_name, propedit_result.returncode,
            )
            for line in propedit_result.stderr.splitlines()[-20:]:
                logger.error("mkvpropedit stderr: %s", line)
            return False

        # Promote the finished concat to its final name — the tmp twin
        # exists only while mkvmerge/propedit write it; a crash after the
        # rename leaves a sidecar-less PARTIAL that recovery re-measures,
        # never a swept-away investment.
        tmp_output.replace(output_file)
        return True

    def _collect_encoded_chunks(self) -> dict[str, list[EncodedChunk]]:
        """Read the winning ``EncodedChunk`` objects from ``EncodingPhase.result``.

        The composed objects are resolved once by the shared dependency walk —
        path via ``stream.file.path`` (no duplicated fields).

        Returns:
            Dict mapping strategy display names to their winner payloads.
        """
        return self._deps[EncodingPhase].encoded_chunks


# ---------------------------------------------------------------------------
# MergePhase module-level helpers
# ---------------------------------------------------------------------------


    @staticmethod
    def _tmp_output_path(output_file: Path) -> Path:
        """The pre-rename destination a merge writes into (``<name>.tmp``).

        The standard temp spelling, swept by :func:`remove_stale_tmp_files`
        like every other remnant.  mkvmerge writes partial data directly at
        its destination name, so the concat and the mkvpropedit header patch
        run on the twin; the rename to *output_file* right after propedit is
        the atomicity boundary — everything downstream (verification,
        measurement, sidecar) targets the final path.
        """
        return output_file.with_name(f"{output_file.stem}{TEMP_SUFFIX}")

    @staticmethod
    def _sidecar_path(output_file: Path) -> Path:
        """Return the sidecar YAML path for a merged output file."""
        return output_file.with_suffix(".yaml")

    @staticmethod
    def _load_merge_sidecar(output_file: Path) -> MergedOutputSidecar | None:
        """Load the per-output acceptance record, or ``None`` if absent/invalid."""
        return load_model(MergePhase._sidecar_path(output_file), MergedOutputSidecar)

    @staticmethod
    def _build_strategy_summaries(
        rows:              list[Artifact[MergedVideo]],
        source_video_path: Path | None,
        rendered_keys:     set[str],
    ) -> tuple[int, list[MergeSummaryRow]]:
        """Build per-strategy summary rows and source size from complete rows.

        Summary metrics carry ONLY the stats the summary table renders — the
        phase sidecar is a presentation surface (full measured stats live on
        the per-video sidecars, the re-measure-avoidance record).

        Args:
            rows:              The merged-output rows (complete ones are summarized).
            source_video_path: Path to the source video for size capture; ``None`` if unavailable.
            rendered_keys:     Metric keys the summary table displays; others are dropped.

        Returns:
            Tuple of ``(source_size_bytes, strategy_summaries)``.
        """
        source_size = (
            (safe_stat_size(source_video_path) or 0)
            if source_video_path is not None else 0
        )

        summaries: list[MergeSummaryRow] = []
        for row in rows:
            if row.state != ArtifactState.COMPLETE:
                continue
            payload = row.payload
            summaries.append(MergeSummaryRow(
                strategy_name   = payload.strategy.display_name(),
                output_path     = payload.output_path,
                file_size_bytes = safe_stat_size(payload.output_path) or 0,
                metrics         = {k: v for k, v in payload.metrics.items() if k in rendered_keys},
            ))
        return source_size, summaries

    def _write_merge_sidecar(
        self,
        output_file:  Path,
        strategy:     Strategy,
        frame_count:  int | None,
        all_metrics:  dict[str, float],
        provenance:   MergedProvenance,
    ) -> None:
        """Atomically write the per-output acceptance record (Req 27).

        Facts (frame count, the FULL measured metric set in both modes — the
        re-judgeable end-user record) plus the production provenance; no
        verdict (verdicts are live, Req 51). The plot path derives from the
        stem and is never persisted.
        """
        record = MergedOutputSidecar(
            frame_count = frame_count,
            metrics     = {k: float(v) for k, v in all_metrics.items()},
            provenance  = provenance,
        )
        try:
            save_model(MergePhase._sidecar_path(output_file), record)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("Could not write merge sidecar for %s: %s", output_file.name, exc)

    def _measure_quality(
        self,
        final_result:  Path,
        source_stream: ExtendedVideoStream,
        ref_crop:      CropParams,
        output_dir:    Path,
    ) -> tuple[dict[str, float], Path | None]:
        """Measure the final output against the source (measurement only).

        The evaluator takes no target set (Req 52): measurement requires the
        file, the reference, and the sampling — nothing else. Verdicts are
        applied by the caller as pure functions over the returned facts
        (Req 53). Raw metric ``.tmp`` files are written to ``output_dir``
        and deleted after parsing; the quality plot PNG is kept.

        Returns:
            Tuple of ``(metrics_dict, plot_path)``.
        """
        evaluator = QualityEvaluator(output_dir)
        plot_path = output_dir / f"{final_result.stem}.png"

        evaluation = evaluator.evaluate_chunk(
            encoded            = final_result,
            reference          = source_stream.stream.as_input(),
            ref_crop           = ref_crop,
            output_dir         = output_dir,
            duration_seconds   = source_stream.stream.info.duration_seconds or 0.0,
            fps_value          = source_stream.stream.info.fps_fraction,
            metrics_output_dir = output_dir,
            subsample_factor   = self._config.measurement.sampling,
            show_progress      = True,
            plot_path          = plot_path,
        )

        metrics_dict = flatten_metric_stats(evaluation.metrics)
        return metrics_dict, evaluation.logs.plot if evaluation.logs.plot else None

    def _fmt_inline_metrics(
        self,
        metrics_dict: dict[str, float],
    ) -> str:
        """Return a compact single-line metrics string for the completion log line.

        Example: ``"vmaf-min=94.1 ✔  vmaf-median=97.4 ✔  psnr-min=41.5 ✘  ssim-min=95.7 ✔"``

        Args:
            metrics_dict:    Measured metric values keyed by ``"{metric}_{statistic}"``.
            quality_targets: Targets used to determine pass/fail symbols.

        Returns:
            Space-separated metric readings, or empty string if no targets.
        """
        parts: list[str] = []
        plan = self._deps[ProbePhase].plan

        for target in plan.targets:
            key   = f"{target.metric}_{target.statistic}"
            value = metrics_dict.get(key)
            if value is None:
                continue
            symbol = SUCCESS_SYMBOL_MINOR if value >= target.value else FAILURE_SYMBOL_MINOR
            parts.append(f"{target.metric}-{target.statistic}={fmt_metric_value(value)} {symbol}")
        return "  ".join(parts)

    def _log_missed_targets_warning(
        self,
        quality_targets: list[QualityTarget],
        *,
        fixed_quality:   bool,
        strategy_name:   str,
        metrics_dict:    dict[str, float],
    ) -> None:
        """Log a WARNING naming every target this strategy missed, with wanted vs actual.

        The completion line and the summary table stay neutral; this is the single
        place a missed target is escalated to warning level so the reason is
        immediately visible where the merge happened.

        Suppressed on fixed-quality runs: the config targets are search-tuned
        vocabulary and would read as all-miss noise at a pinned knob — the
        merged-output measurement itself remains the final check.

        Args:
            quality_targets: The run's resolved quality targets (the plan's).
            fixed_quality:   Whether the run pins the quality knob.
            strategy_name:   The merged strategy.
            metrics_dict:    Measured metrics keyed by ``"{metric}_{statistic}"``.
        """
        if fixed_quality:
            return
        missed: list[str] = []
        for target in quality_targets:
            value = metrics_dict.get(f"{target.metric}_{target.statistic}")
            if value is not None and value < target.value:
                missed.append(
                    f"{target.metric}-{target.statistic} = {fmt_metric_value(value)} "
                    f"(target ≥ {fmt_metric_value(target.value)})"
                )
        if missed:
            logger.warning(
                "%s %s missed quality targets: %s",
                WARNING_SYMBOL, strategy_name, ";  ".join(missed),
            )

    @staticmethod
    def _log_merge_summary(
        summaries:         list[MergeSummaryRow],
        source_stem:       str,
        source_size_bytes: int,
        quality_targets:   list[QualityTarget],
        metrics_sampling:  int,
    ) -> None:
        """Log the merge summary: source row + strategy table with sizes, % of source,
        and quality pass/miss marks; followed by a targets reminder and per-miss details.

        Args:
            summaries:         Per-strategy summary rows, sorted by file size ascending.
            source_stem:       Source video stem (filename without extension).
            source_size_bytes: Size of the source video in bytes; ``0`` if unavailable.
            quality_targets:   Quality targets that were checked.
            metrics_sampling:  Frame subsampling factor used during measurement.
        """
        if not summaries:
            logger.info("  No output files produced.")
            return

        source_size = source_size_bytes
        has_targets = bool(quality_targets)

        sorted_summaries = sorted(summaries, key=lambda r: safe_stat_size(r.output_path) or 0)

        def _pct_str(size: int) -> str:
            if source_size <= 0:
                return "  N/A"
            return f"{size / source_size * 100:5.1f}%"

        # --- Table header ---
        if has_targets:
            logger.info("  %-25s  %12s  %7s  %s", "Strategy", "Size (MB)", "vs src", "Quality")
            logger.info("  %-25s  %12s  %7s  %s", "-" * 25, "-" * 12, "-" * 7, "-" * 7)
        else:
            logger.info("  %-25s  %12s  %7s", "Strategy", "Size (MB)", "vs src")
            logger.info("  %-25s  %12s  %7s", "-" * 25, "-" * 12, "-" * 7)

        # --- Source row ---
        if source_size > 0:
            src_str = fmt_size_mb(source_size)
            if has_targets:
                logger.info("  %-25s  %12s  %7s  %s", source_stem[:25], src_str, "100.0%", "")
            else:
                logger.info("  %-25s  %12s  %7s", source_stem[:25], src_str, "100.0%")

        # --- Strategy rows ---
        any_miss = False
        for summary in sorted_summaries:
            size_bytes = safe_stat_size(summary.output_path) or 0
            size_str   = fmt_size_mb(size_bytes)
            pct        = _pct_str(size_bytes)

            if has_targets:
                met = not QualitySearchBase.failed_targets(summary.metrics, quality_targets)
                if summary.metrics:
                    mark = SUCCESS_SYMBOL_MINOR if met else FAILURE_SYMBOL_MINOR
                    if not met:
                        any_miss = True
                else:
                    mark = "-"
                logger.info("  %-25s  %12s  %7s  %s", summary.strategy_name[:25], size_str, pct, mark)
            else:
                logger.info("  %-25s  %12s  %7s", summary.strategy_name[:25], size_str, pct)

        # --- Output location note ---
        output_dir = sorted_summaries[0].output_path.parent
        logger.info("")
        logger.info("  Files named: %s *.mkv  (where * is the strategy)", source_stem)
        logger.info("  Location: %s", output_dir)

        if not has_targets:
            return

        # --- Targets reminder ---
        targets_str = "  Targets: " + ",  ".join(
            f"{t.metric}-{t.statistic} ≥ {t.value:.2f}"
            for t in quality_targets
        )
        logger.info("")
        logger.info(targets_str)

        if not any_miss:
            logger.info("  %s All quality targets met.", SUCCESS_SYMBOL_MINOR)
            return

        # --- Per-miss details as key-value table ---
        miss_table: dict[str, str | list] = {}

        for summary in sorted_summaries:
            met = not QualitySearchBase.failed_targets(summary.metrics, quality_targets)
            if met or not summary.metrics:
                continue
            missed_lines = []
            for target in quality_targets:
                key   = f"{target.metric}_{target.statistic}"
                value = summary.metrics.get(key)
                if value is None:
                    missed_lines.append(f"{target.metric}-{target.statistic}: not measured (target: {target.value:.2f})")
                elif value < target.value:
                    missed_lines.append(f"{target.metric}-{target.statistic}: {value:.2f} (target: {target.value:.2f})")
            if missed_lines:
                miss_table[f"{WARNING_SYMBOL} {summary.strategy_name}"] = missed_lines if len(missed_lines) > 1 else missed_lines[0]

        fmt_key_value_table(miss_table)

        if miss_table and metrics_sampling > 1:
            logger.info("  (Subsampling 1:%d — with fewer frames measured, there's a higher chance to miss outliers, making quality targeting less reliable)", metrics_sampling)

    @staticmethod
    def _log_merge_summary_from_sidecar(
        sidecar:           MergeSidecar,
        quality_targets:   list[QualityTarget],
        source_stem:       str,
        source_size_bytes: int,
        metrics_sampling:  int,
    ) -> None:
        """Replay the merge summary table from the persisted replay aggregate.

        Called on the REUSED path so the user sees the same table as on the
        original run — with LIVE verdict marks (Req 51): the rows carry
        metrics only, judged here against the CURRENT targets. The source
        row renders live from the JobPhase result.

        Args:
            sidecar:           Loaded ``MergeSidecar`` from ``merge.yaml``.
            quality_targets:   Current quality targets (the live verdict bar).
            source_stem:       Source filename stem (live from the job result).
            source_size_bytes: Source size in bytes (live from the job result).
            metrics_sampling:  Current sampling (miss-detail footnote).
        """
        if not sidecar.summary:
            logger.info("  No summary data saved — re-run to generate.")
            return

        MergePhase._log_merge_summary(
            summaries         = sidecar.summary,
            source_stem       = source_stem,
            source_size_bytes = source_size_bytes,
            quality_targets   = quality_targets,
            metrics_sampling  = metrics_sampling,
        )

    @staticmethod
    def _collect_crf_data(
        work_dir: Path,
        winners:  list[Artifact[EncodedChunk]],
        strategy: str,
    ) -> list[tuple[float, float, Decimal]]:
        """Extract ``(start_seconds, end_seconds, crf)`` tuples for a strategy's winners.

        The winning quality is a fact of the winner result sidecar (Req 3/4) —
        read here on this rare re-merge processing path, never at recovery.
        The window comes through ``payload.chunk``.

        Args:
            work_dir: The run's work dir (locates ``encoded/<strategy>/``).
            winners:  The encoding phase's winner rows.
            strategy: Strategy display name to filter by.

        Returns:
            List of ``(start_s, end_s, crf)`` sorted by start time.
        """
        result: list[tuple[float, float, Decimal]] = []
        for row in winners:
            payload = row.payload
            if payload.strategy.display_name() != strategy:
                continue
            sidecar = read_winner_sidecar(work_dir, payload)
            if sidecar is None or sidecar.get("crf") is None:
                logger.warning(
                    "Winner %s has no readable crf on its sidecar — excluded "
                    "from the CRF plot",
                    payload.chunk.safe_name(),
                )
                continue
            result.append((
                payload.chunk.start_timestamp,
                payload.chunk.end_timestamp,
                Decimal(str(sidecar["crf"])),
            ))
        result.sort(key=lambda t: t[0])
        return result

    @staticmethod
    def _build_mkvmerge_options(
        chunks:          list[Path],
        output:          Path,
        timestamps_path: Path,
    ) -> list[str]:
        """Build the mkvmerge argument list for chunk concatenation with PTS restoration.

        The first chunk is listed without a prefix; each subsequent chunk is
        preceded by ``"+"`` as a separate element (mkvmerge append syntax).
        ``--timestamps`` is applied to track 0 of the first chunk only.  The
        output's track-header ``DefaultDuration`` is restored afterwards by
        ``mkvpropedit`` (see :func:`_build_mkvpropedit_args`): mkvmerge derives it
        from the ms-rounded restored timestamps, and ``--default-duration`` cannot
        override that — it only reinterprets *input* tracks that lack timing.

        Paths converted for the JSON file are standalone argv elements there, so
        they use ``os.fspath`` — the same string a subprocess would resolve for a
        path-like. The ``--timestamps`` value is a sub-string argument (track
        spec) and keeps the plain form: mkvmerge parses it itself and may not
        accept an extended-length prefix inside it.

        Args:
            chunks:          Ordered list of encoded chunk paths.
            output:          Destination output path — the pre-rename tmp
                             twin during a run.
            timestamps_path: Path to the timestamps.txt file.

        Returns:
            List of strings suitable for writing to a JSON options file.
        """
        args: list[str] = [
            "-o",              os.fspath(output),
            "--timestamps", f"0:{timestamps_path}",
            os.fspath(chunks[0]),
        ]
        for chunk in chunks[1:]:
            args.append(f"+{os.fspath(chunk)}")
        return args

    @staticmethod
    def _build_mkvpropedit_args(output: Path, fps: Fraction) -> list[str | os.PathLike]:
        """Build the mkvpropedit argument list restoring the video track's frame-rate header.

        mkvmerge derives ``DefaultDuration`` from the ms-rounded timestamps that
        ``--timestamps`` restores (observed 42 ms → 500/21 for a 24000/1001
        stream).  Header-only consumers then misdeclare the frame rate and flag
        the output VFR (MediaInfo: "Frame rate mode: Variable").  The edit is
        instant, does not touch block data, and takes an integer ns value.

        The ns value uses exact rational arithmetic: the float path drifts at
        NTSC rates (24000/1001 → 41 708 333.33 ns).

        Args:
            output: The merged MKV whose track header is patched in place.
            fps:    The source stream's true frame rate.

        Returns:
            The mkvpropedit command; *output* is passed as a path-like so the
            extended-length ``\\?`` prefix is injected only when the runner
            resolves it.
        """
        return [
            "mkvpropedit", output,
            "--edit", MergePhase._MKVPROPEDIT_VIDEO_TRACK,
            "--set", f"default-duration={round(MergePhase._NS_PER_SECOND / fps)}",
        ]

    @staticmethod
    def _write_mkvmerge_options_file(path: Path, args: list[str]) -> None:
        """Write mkvmerge arguments to a JSON options file atomically.

        Uses the ``.tmp``-then-rename protocol for consistency.

        Args:
            path: Destination path for the options file.
            args: List of mkvmerge argument strings.
        """
        tmp = path.parent / f"{path.stem}{TEMP_SUFFIX}"
        tmp.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
