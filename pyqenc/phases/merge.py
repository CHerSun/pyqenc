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
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pyqenc.app_metrics import AppMetricKey
from pyqenc.constants import (
    APP_METRIC_KEY_MEASURE,
    BRACKET_LEFT,
    BRACKET_RIGHT,
    FAILURE_SYMBOL_MINOR,
    MERGED_OUTPUT_DIR,
    SUCCESS_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
    WARNING_SYMBOL,
)
from pyqenc.models import (
    CropParams,
    EncodingPlan,
    FingerprintYaml,
    PhaseOutcome,
    QualityTarget,
    SizedFingerprint,
    SizedFingerprintYaml,
    Strategy,
    fingerprinter,
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
from pyqenc.quality import QualitySearchBase
from pyqenc.quality_metrics import (
    MeasuredMetrics,
    MeasuredMetricsYaml,
)
from pyqenc.state import ProbeFacet
from pyqenc.stream_model import (
    DecimalYaml,
    EncodedChunk,
    ExtendedVideoStream,
    File,
    LongPathYaml,
    MergedVideo,
)
from pyqenc.summary_tables import (
    ComparisonTable,
    Mark,
    SourceInfo,
    StrategyRow,
    render_comparison_table,
)
from pyqenc.utils.alive import AdvanceState, BarAdvance, ProgressBar
from pyqenc.utils.ffmpeg_runner import FrameCountError, get_frame_count
from pyqenc.utils.fs import remove_stale_tmp_files, safe_stat_size
from pyqenc.utils.log_format import fmt_key_value_table, fmt_metric_value
from pyqenc.utils.visualization import QualityMeasurer, create_quality_plot
from pyqenc.utils.yaml_utils import load_model, save_model

# ---------------------------------------------------------------------------
# Sidecar models (re-homed, Req 21): per-output acceptance records + the
# merge.yaml replay aggregate
# ---------------------------------------------------------------------------

class _MergedSidecarBase(BaseModel):
    """The common fields of both merged-output sidecar variants (Req 27).

    The per-output acceptance record, written next to each merged output
    (stem swap): its presence PLUS a match on every identity dimension
    makes the output COMPLETE (Req 49). Field names follow the design
    layout: named for WHAT they identify, never for the fingerprint
    mechanism (Req 61) — ``strategy`` IS the strategy's resolved-args
    fingerprint; the strategy's name is already the output's own file
    name. Every dimension is compared per-file at recovery except
    ``source`` — the one dimension that escalates to the phase-level
    catastrophic condition (Req 60). The anchor is never a dimension: it
    is a runtime-elected presentation basis (the least-size strategy
    with measurements), re-derived per run by optimization — never
    persisted, never compared.

    ``frame_count`` is the measured frame count of the merged output
    (``0`` = the unknown sentinel — no valid video has zero frames; set
    at the fetch site, never a model default). ``metrics`` is the FULL
    measured set in both modes — the re-judgeable end-user record
    (verdicts are always live, Req 51, so none is ever persisted). The
    plot path derives from the stem and is never persisted.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    source:      SizedFingerprintYaml
    strategy:    FingerprintYaml
    probe:       ProbeFacet
    sampling:    int
    winners:     SizedFingerprintYaml
    frame_count: int
    metrics:     MeasuredMetricsYaml = Field(default_factory=MeasuredMetrics)


class FixedMergedOutputSidecar(_MergedSidecarBase):
    """Fixed runs: the strategy's quantized quality is the mode key.

    A quality change re-merges automatically — the collapsed range puts
    the value in the output's own name (``<LABEL>=<q>``), so a different
    quality means a different expected file. Fixed runs carry NO targets:
    the judging reference is the anchor-derived anchor target set, computed live.
    """

    mode:    Literal["fixed"] = "fixed"
    quality: DecimalYaml


class SearchMergedOutputSidecar(_MergedSidecarBase):
    """Search runs: the targets basis is the mode key (targets steer the
    search — a change re-searches and re-merges; Req 50)."""

    mode:    Literal["search"] = "search"
    targets: list[str]         = Field(default_factory=list)


MergedOutputSidecar = FixedMergedOutputSidecar | SearchMergedOutputSidecar
"""The per-mode sidecar union; loading dispatches on the ``mode`` tag."""

_MERGED_OUTPUT_ADAPTER: TypeAdapter[MergedOutputSidecar] = TypeAdapter(
    Annotated[MergedOutputSidecar, Field(discriminator="mode")]
)


def load_merged_output_sidecar(sidecar_path: Path) -> MergedOutputSidecar | None:
    """Load a merged-output acceptance record dispatching on the mode tag.

    Args:
        sidecar_path: The sidecar YAML path.

    Returns:
        The tagged record, or ``None`` when absent or unparsable.
    """
    return load_model(sidecar_path, _MERGED_OUTPUT_ADAPTER)


class MergeSidecar(BaseModel):
    """``merge.yaml`` — the summary-replay aggregate ONLY.

    The per-output sidecars are the acceptance records (Req 28): this file
    carries no identity and no invalidation keys; its absence costs summary
    replay only, never correctness. The rows are ``StrategyRow`` — THE
    shared row shape — carrying the rendered basis (the presentation
    keys: target statistics in search, the fixed comparison statistics);
    full retention lives on the per-video sidecars, and discrepancies
    re-actualize through the invalidation ladder.

    Attributes:
        output_dir: The directory holding the merged outputs (the
                    ``Location`` line renders from it).
        summary:    Per-strategy replay rows (empty until a processing pass).
    """

    output_dir: LongPathYaml
    summary:    list[StrategyRow] = Field(default_factory=list)


if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.app_metrics import AppMetricsCollector

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


class _SidecarBuilder:
    """The current run's acceptance inputs + the per-file record composer.

    One builder per recovery; ``compose`` builds the acceptance record to
    persist for one output (the mode's own variant — fixed carries the
    quality, search the targets), ``acceptance_mismatch`` names the first
    recorded dimension that contradicts the current inputs (``None`` =
    accepted).
    """

    def __init__(
        self,
        source:   SizedFingerprint,
        probe:    ProbeFacet,
        plan:     EncodingPlan,
        sampling: int,
        winners:  SizedFingerprint,
    ) -> None:
        self._source   = source
        self._probe    = probe
        self._plan     = plan
        self._sampling = sampling
        self._winners  = winners

    def compose(
        self,
        strategy:    Strategy,
        frame_count: int,
        all_metrics: MeasuredMetrics,
    ) -> MergedOutputSidecar:
        """Build the per-output acceptance record (the mode's variant).

        Facts (frame count, the FULL measured metric set in both modes —
        the re-judgeable end-user record) plus the production-basis
        identity; no verdict (verdicts are live, Req 51).
        """
        codec = strategy.codec
        if self._plan.fixed_quality:
            return FixedMergedOutputSidecar(
                source      = self._source,
                strategy    = strategy.fingerprint,
                probe       = self._probe,
                sampling    = self._sampling,
                winners     = self._winners,
                frame_count = frame_count,
                metrics     = all_metrics,
                quality     = codec.quality_better.quantize(codec.quality_granularity),
            )
        return SearchMergedOutputSidecar(
            source      = self._source,
            strategy    = strategy.fingerprint,
            probe       = self._probe,
            sampling    = self._sampling,
            winners     = self._winners,
            frame_count = frame_count,
            metrics     = all_metrics,
            targets     = targets_as_strings(self._plan.targets),
        )

    def acceptance_mismatch(
        self,
        recorded: MergedOutputSidecar,
        strategy: Strategy,
    ) -> str | None:
        """The first recorded dimension contradicting the current inputs.

        Returns ``None`` when the output is accepted; the mismatching
        dimension's name otherwise (``"sampling"`` re-measures in place;
        everything else re-merges — M-8). An unverifiable dimension counts
        as a mismatch (conservative). The variant kinds must agree — a
        mode switch between runs is itself a mismatch.
        """
        codec = strategy.codec
        if recorded.strategy != strategy.fingerprint:
            return "strategy fingerprint"
        if self._plan.fixed_quality:
            if not isinstance(recorded, FixedMergedOutputSidecar):
                return "mode"
            if recorded.quality != codec.quality_better.quantize(codec.quality_granularity):
                return "quality"
        else:
            if not isinstance(recorded, SearchMergedOutputSidecar):
                return "mode"
            if recorded.targets != targets_as_strings(self._plan.targets):
                return "targets"
        if not recorded.probe.matches(self._probe):
            return "probe facet"
        # The winner-set fingerprint is compared as a whole (Req 11a):
        # count + hash over the sorted winner ids; unverifiable = mismatch.
        if recorded.winners != self._winners:
            return "winner set"
        if recorded.sampling != self._sampling:
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
    # anchor and its anchor targets flow from its result; it is transitively
    # guaranteed via Encoding anyway. Audio is NOT a dependency: merge is
    # video-only and never reads the audio result — `auto` schedules audio
    # via its terminal position, not via this tuple.
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (
        JobPhase, ExtractionPhase, ProbePhase, OptimizationPhase,
        EncodingPhase,
    )
    _METRIC_KEY: AppMetricKey = AppMetricKey.MERGE

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry,
        *,
        collector: AppMetricsCollector,
    ) -> None:
        """Store the shared constructor state and the per-run stash."""
        super().__init__(config, phases, collector=collector)
        self._pending_reasons: dict[str, str] = {}

    def _log_key_params(self) -> None:
        """Log the merge phase key input params."""
        plan = self._deps[ProbePhase].plan
        strategies = self._deps[OptimizationPhase].selected_strategies

        kv: dict[str, object] = {
        }

        if plan.fixed_quality:
            kv["Quality:"] = [f"{strategy.codec.quality_label}={strategy.codec.quality_range[0]} for {strategy.display_name()}" for strategy in strategies]
        else:
            kv["Strategies:"] = [s.display_name() for s in strategies]
            kv["Targets:"] = [str(t) for t in plan.targets]

        fmt_key_value_table(kv)


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
        change is a per-file record mismatch handled by the
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
            if record is not None and record.source != job_result.file.payload.fingerprint:
                if not job_result.force:
                    raise RecoveryError(
                        "Source content identity mismatch (a merged output's "
                        "sidecar) — the deliverables belong to a different source.  "
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
           present AND its sidecar matches the current inputs
           on every recorded dimension (strategy fingerprint, mode + the
           mode's key — quality (fixed) / targets (search) —, probe facet,
           sampling, winner-set fingerprint); any mismatch or an
           unverifiable winner-set means re-merge — never a silent
           acceptance, never a re-measure of a stale concat (M-8: a
           key change always re-searched winners upstream; the anchor is
           runtime-only and never compared). A sampling-ONLY difference
           re-measures in place (the concat is current). File present
           without a sidecar is
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

        source_stem = job_result.file.payload.path.stem

        # Per-file acceptance (Req 49): the current inputs each output's
        # record must match. The winner-set fingerprint derives live from the
        # encoding winners (in memory — no disk reads).
        sidecar_builder = self._sidecar_builder()
        self._pending_reasons = {}

        rows: list[Artifact] = []
        expected_names: set[str] = set()
        for strategy in strategies:
            output_file = merged_dir / MergedVideo.output_file_name(source_stem, strategy)
            expected_names.add(output_file.name)
            record = MergePhase._load_merge_sidecar(output_file)

            if output_file.exists() and record is not None:
                reason = sidecar_builder.acceptance_mismatch(
                    record, strategy,
                )
                if reason is None:
                    # COMPLETE — accepted: facts load for the live-verdict
                    # summary; the verdict itself is never persisted (Req 51).
                    metrics = record.metrics
                    rows.append(Artifact(
                        payload = MergedVideo(
                            source_stem = source_stem,
                            strategy    = strategy,
                            output_path = output_file,
                            frame_count = record.frame_count,
                            metrics     = metrics,
                            targets_met = self._live_verdict(metrics),
                            plot_path   = output_file.with_suffix(".png")
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
                    output_path = output_file,
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
                    payload = File(path=output_file),
                    state   = state,
                    wanted  = False,
                ))
                logger.debug("Merged output %s is surplus (strategy no longer selected) — unwanted", output_file.name)

        return Recovery.from_artifacts(rows)

    def _verdict_targets(self) -> list[QualityTarget]:
        """The live verdict targets for SEARCH runs: the configured targets.

        Fixed runs carry no interim verdict targets — the merge anchor
        exists only once the full-movie population completes, and the test
        anchor's values participate nowhere at merge (Req 3.2); the table is
        the fixed verdict surface.
        """
        plan = self._deps[ProbePhase].plan
        if plan.fixed_quality:
            return []
        return list(plan.targets)

    def _live_verdict(self, metrics: MeasuredMetrics) -> bool:
        """The live pass/fail against the verdict targets (Req 51).

        Fixed runs carry no verdict reference — ``True`` with
        empty targets is the vacuous truth.
        """
        return not QualitySearchBase.failed_targets(metrics, self._verdict_targets())

    def _sidecar_builder(self) -> _SidecarBuilder:
        """The current run's acceptance inputs for per-file record checks."""
        return _SidecarBuilder(
            source    = self._deps[JobPhase].file.payload.fingerprint,
            probe     = ProbeFacet.from_probe(self._deps[ProbePhase]),
            plan      = self._deps[ProbePhase].plan,
            sampling  = self._config.measurement.sampling,
            winners   = fingerprinter.of_ids(
                row.payload.chunk.safe_name()
                for row in self._deps[EncodingPhase].winners
            ),
        )

    def _reused_result(self, wanted: list[Artifact], message: str) -> MergePhaseResult:
        """Build the reused result, emerging the summary from the persisted rows."""
        job_result = self._deps[JobPhase]
        merge_yaml = job_result.work_dir / MergePhase.SIDECAR_NAME
        persisted  = load_model(merge_yaml, MergeSidecar)
        if persisted is not None:
            # The replay re-derives the comparison basis and marks live from
            # the persisted rows (byte-identical to the producing run's
            # table; a changed verdict reference re-renders old rows
            # correctly, Req 6.7).
            self._render_merge_summary(persisted.summary, persisted.output_dir)
        else:
            logger.info("  No summary data saved — re-run to generate.")
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
        ``merge.concat`` / ``merge.measure`` spans are recorded around
        the individual sub-actions below. ``dry_run`` is never ``True`` here
        (merge is not a readonly-execute phase; the template previews
        instead).

        Every pending row re-merges from the current winners — ABSENT,
        record-mismatched, and the sidecar-less PARTIAL alike (the
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
        source_stem = job_result.file.payload.path.stem

        # Build encoded_chunks dict from EncodingPhase result
        encoded_chunks = self._collect_encoded_chunks()
        sidecar_builder = self._sidecar_builder()

        final_rows: list[Artifact[MergedVideo]] = []
        failed_strategies: list[str] = []

        pending_count  = sum(1 for a in rows if a.state != ArtifactState.COMPLETE)
        source_duration = source_stream.stream.info.duration_seconds or 0.0
        total_seconds   = pending_count * source_duration

        pending_idx = 0
        with ProgressBar(total_seconds, title="Merging", total_count=pending_count) as advance:
            for artifact in rows:
                payload       = artifact.payload
                strategy      = payload.strategy
                strategy_name = strategy.display_name()

                if artifact.state == ArtifactState.COMPLETE:
                    advance(0, AdvanceState.SKIPPED)
                    final_rows.append(artifact)
                    continue

                pending_idx += 1

                # The output name derives at the entity's single owning site.
                output_file = merged_dir / MergedVideo.output_file_name(source_stem, strategy)
                assert output_file == payload.output_path, "recovery derived the same location"

                try:
                    reason = self._pending_reasons.get(strategy_name)
                    if reason == "sampling":
                        # Sampling-only difference (Req 49): the concat is
                        # current — re-measure in place, never re-merge.
                        # Under the measurement opt-out there is nothing to
                        # redo for this row — the concat is already current.
                        if plan.measure:
                            logger.info(
                                "%s%s%s (%d of %d): starting quality measurement",
                                BRACKET_LEFT, strategy_name, BRACKET_RIGHT,
                                pending_idx, pending_count,
                            )
                    else:
                        chunk_count = len(encoded_chunks.get(strategy_name, []))
                        logger.info(
                            "%s%s%s (%d of %d): starting merge — %d chunks",
                            BRACKET_LEFT, strategy_name, BRACKET_RIGHT,
                            pending_idx, pending_count, chunk_count,
                        )
                        if not self._concat_and_promote(
                            strategy        = strategy,
                            output_file     = output_file,
                            source_stream   = source_stream,
                            encoded_chunks  = encoded_chunks,
                        ):
                            # Everything else re-merges — ABSENT rows, record
                            # mismatches (M-8: a mode-key change re-searched
                            # winners upstream; the old concat is never current),
                            # and the UNVERIFIABLE case (file present, sidecar
                            # missing — Req 49/50): nothing vouches for what
                            # produced the file, so it is re-concatenated from the
                            # current winners in place; a crash leaves the old
                            # output present until the promotion rename replaces it.
                            advance(0, AdvanceState.FAILED)
                            failed_strategies.append(strategy_name)
                            continue
                        if plan.measure:
                            logger.info(
                                "%s%s%s (%d of %d): starting quality measurement",
                                BRACKET_LEFT, strategy_name, BRACKET_RIGHT,
                                pending_idx, pending_count,
                            )

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

                    # Measure quality (Req 52): measurement needs the file, the
                    # reference, and the sampling — no target set gates or
                    # parameterizes it. Verdicts are live (Req 51). The
                    # `--no-measure` opt-out is the one exception: the user
                    # explicitly traded reporting for speed.
                    metrics_dict: MeasuredMetrics = MeasuredMetrics()
                    plot_path:    Path | None       = None
                    if plan.measure:
                        try:
                            with self._collector.time(AppMetricKey.MERGE, APP_METRIC_KEY_MEASURE):
                                metrics_dict, plot_path = self._measure_quality(
                                    final_result  = output_file,
                                    source_stream = source_stream,
                                    ref_crop      = crop,
                                    output_dir    = merged_dir,
                                    show_progress = advance,
                                )
                        except (OSError, ValueError) as exc:
                            logger.warning("  Could not measure quality: %s", exc)
                    else:
                        logger.info(
                            "  Measurement opted out — merged output unmeasured; "
                            "run 'pyqenc measure <output>' for quality metrics"
                        )

                    # Quality-knob distribution plot — searched runs only: a
                    # fixed run's knob is constant, so the per-chunk plot
                    # carries zero information. Skipping it there is the
                    # design, not a problem: no warning for user intent.
                    quality_data = (
                        MergePhase._collect_quality_data(
                            self._deps[JobPhase].work_dir,
                            self._deps[EncodingPhase].winners,
                            strategy_name,
                        )
                        if not plan.fixed_quality
                        else []
                    )
                    if quality_data:
                        quality_plot_path = merged_dir / f"{output_file.stem}.quality.png"
                        try:
                            qlabel = self._deps[EncodingPhase].quality_labels.get(strategy_name, "quality")
                            create_quality_plot(
                                chunks        = quality_data,
                                output_path   = quality_plot_path,
                                title         = f"{qlabel}\n{output_file.stem.replace(TIME_SEPARATOR_MS, ".").replace(TIME_SEPARATOR_SAFE, ":")}",
                                quality_label = qlabel,
                            )
                            logger.debug("  Quality plot saved: %s", quality_plot_path.name)
                        except (OSError, ValueError) as exc:
                            logger.warning("  Could not generate quality plot: %s", exc)
                    elif not plan.fixed_quality:
                        logger.warning("No quality data available for strategy %s — skipping quality plot", strategy_name)
                    else:
                        logger.debug(
                            "Quality plot skipped (fixed knob — no distribution): %s",
                            strategy_name,
                        )

                    # Write the per-output acceptance record (facts + identity,
                    # no verdict — Req 27).
                    save_model(
                        MergePhase._sidecar_path(output_file),
                        sidecar_builder.compose(
                            strategy    = strategy,
                            frame_count = frame_count or 0,
                            all_metrics = metrics_dict,
                        ),
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
                            output_path = output_file,
                            frame_count = frame_count,
                            metrics     = metrics_dict,
                            targets_met = targets_met,
                            plot_path   = plot_path,
                        ),
                        state   = ArtifactState.COMPLETE,
                    ))
                    advance(source_duration, AdvanceState.SUCCESS)

                except Exception:  # one bad strategy must not kill the rest
                    logger.exception("Merging strategy %s error", strategy_name)
                    advance(0, AdvanceState.FAILED)
                    failed_strategies.append(strategy_name)

        advance(0, AdvanceState.COMPLETE)

        # Phase completion summary
        complete_count = sum(1 for a in final_rows if a.state == ArtifactState.COMPLETE)
        if failed_strategies:
            logger.error("  Failed strategies: %s", ", ".join(failed_strategies))
        strategy_summaries = self._build_strategy_summaries(final_rows)
        output_dir = (
            next(
                (row.payload.output_path.parent for row in final_rows
                 if row.state == ArtifactState.COMPLETE),
                None,
            )
        )
        if output_dir is not None:
            self._render_merge_summary(strategy_summaries, output_dir)
            # Persist merge.yaml: the summary replay aggregate ONLY (Req 28)
            # — the per-output sidecars are the acceptance records; the
            # comparison basis and marks re-derive live at replay.
            save_model(
                self._deps[JobPhase].work_dir / MergePhase.SIDECAR_NAME,
                MergeSidecar(output_dir=output_dir, summary=strategy_summaries),
            )
        if failed_strategies and not final_rows:
            return self._make_result(PhaseOutcome.FAILED, [], "All strategy merges failed")

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

        with self._collector.time(AppMetricKey.MERGE, "concat"):
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
        return load_merged_output_sidecar(MergePhase._sidecar_path(output_file))

    def _build_strategy_summaries(
        self,
        rows: list[Artifact[MergedVideo]],
    ) -> list[StrategyRow]:
        """Build the per-strategy summary rows from the complete merged outputs.

        Sizes are recorded at merge time (persisted sizes drive the table and
        the replay — no live re-stat, Req 6.7); metrics carry ONLY the
        rendered basis (the verdict reference's keys) — full measured stats
        live on the per-video sidecars, the re-measure-avoidance record.

        Args:
            rows: The merged-output rows (complete ones are summarized).

        Returns:
            The ``StrategyRow`` population for the comparison table.
        """
        summaries: list[StrategyRow] = []
        for row in rows:
            if row.state != ArtifactState.COMPLETE:
                continue
            payload = row.payload
            summaries.append(StrategyRow(
                strategy   = payload.strategy.display_name(),
                size_bytes = safe_stat_size(payload.output_path) or 0,
                metrics    = self._deps[ProbePhase].plan.targeted(payload.metrics),
            ))
        return summaries

    def _measure_quality(
        self,
        final_result:  Path,
        source_stream: ExtendedVideoStream,
        ref_crop:      CropParams,
        output_dir:    Path,
        show_progress: bool | BarAdvance = False,
    ) -> tuple[MeasuredMetrics, Path | None]:
        """Measure the final output against the source (measurement only).

        The measurer takes no target set (Req 52): measurement requires the
        file, the reference, and the sampling — nothing else. Verdicts are
        applied by the caller as pure functions over the returned facts
        (Req 53). Raw metric ``.tmp`` files are written to ``output_dir``
        and deleted after parsing; the quality plot PNG is kept.

        Args:
            show_progress: ``True`` — open an inner progress bar;
                           ``BarAdvance`` callable — feed an outer bar
                           supplied by the caller (no inner bar created);
                           ``False`` (default) — no progress.

        Returns:
            Tuple of ``(metrics_dict, plot_path)``.
        """
        measurer   = QualityMeasurer(output_dir)
        plot_path = output_dir / f"{final_result.stem}.png"

        measurement = measurer.measure_chunk(
            encoded            = final_result,
            reference          = source_stream.stream.as_input(),
            ref_crop           = ref_crop,
            output_dir         = output_dir,
            duration_seconds   = source_stream.stream.info.duration_seconds or 0.0,
            fps_value          = source_stream.stream.info.fps_fraction,
            metrics_output_dir = output_dir,
            sampling           = self._config.measurement.sampling,
            show_progress      = show_progress,
            plot_path          = plot_path,
        )

        metrics_dict = measurement.metrics
        return metrics_dict, measurement.logs.plot if measurement.logs.plot else None

    def _fmt_inline_metrics(
        self,
        metrics_dict: MeasuredMetrics,
    ) -> str:
        """Return a compact single-line metrics string for the completion log line.

        Search runs mark ``✔``/``✘`` per target (the configured targets are
        the judging reference — available upfront). Fixed runs show the
        comparison values WITHOUT verdicts: the merge anchor exists only
        after the full-movie population completes (the test anchor's values
        participate nowhere, Req 3.2) — the table is the fixed verdict
        surface.

        Args:
            metrics_dict: The full measured set of the merged output.

        Returns:
            Space-separated metric readings, or empty string when nothing renders.
        """
        plan = self._deps[ProbePhase].plan
        parts: list[str] = []

        if plan.fixed_quality:
            for key, value in plan.targeted(metrics_dict).items():
                parts.append(f"{key.cli_key()}={fmt_metric_value(value)}")
            return "  ".join(parts)

        for target in plan.targets:
            value = metrics_dict.get(target.identity)
            if value is None:
                continue
            symbol = SUCCESS_SYMBOL_MINOR if value >= target.value else FAILURE_SYMBOL_MINOR
            parts.append(f"{target.identity.cli_key()}={fmt_metric_value(value)} {symbol}")
        return "  ".join(parts)

    def _log_missed_targets_warning(
        self,
        quality_targets: list[QualityTarget],
        *,
        fixed_quality:   bool,
        strategy_name:   str,
        metrics_dict:    MeasuredMetrics,
    ) -> None:
        """Log a WARNING naming every target this strategy missed, with wanted vs actual.

        The completion line and the summary table stay neutral; this is the single
        place a missed target is escalated to warning level so the reason is
        immediately visible where the merge happened.

        Suppressed on fixed-quality runs: the config targets are search-tuned
        vocabulary and would read as all-miss noise at a fixed knob — the
        merged-output measurement itself remains the final check.

        Args:
            quality_targets: The run's resolved quality targets (the plan's).
            fixed_quality:   Whether the run fixes the quality knob.
            strategy_name:   The merged strategy.
            metrics_dict:    The full measured set of the merged output.
        """
        if fixed_quality:
            return
        missed: list[str] = []
        for target in quality_targets:
            value = metrics_dict.get(target.identity)
            if value is not None and value < target.value:
                missed.append(
                    f"{target.identity.cli_key()} = {fmt_metric_value(value)} "
                    f"(target ≥ {fmt_metric_value(target.value)})"
                )
        if missed:
            logger.warning(
                "%s %s missed quality targets: %s",
                WARNING_SYMBOL, strategy_name, ";  ".join(missed),
            )

    def _render_merge_summary(self, summaries: list[StrategyRow], output_dir: Path) -> None:
        """Render the unified merge comparison table from persisted rows.

        The single rendering path for processing and replay alike (Req 6):
        rows + a live-derived comparison basis and marks. Fixed mode elects
        the MERGE anchor over the full-movie rows (the test anchor's values
        participate nowhere, Req 3.2) and the below-table anchor-audit line
        compares the two elections; search mode rules by the configured
        targets with live all-targets-pass verdicts (no verdict when the
        run opted out of measurement). Below the table: the
        ``Files named``/``Location`` lines and the subsampling footnote.

        Args:
            summaries:  The ``StrategyRow`` population (persisted sizes drive
                        the table — no output re-stat).
            output_dir: The directory holding the merged outputs.
        """
        if not summaries:
            logger.info("  No output files produced.")
            return

        job_result = self._deps[JobPhase]
        plan       = self._deps[ProbePhase].plan
        source     = SourceInfo(
            stem       = job_result.file.payload.path.stem,
            size_bytes = safe_stat_size(job_result.file.payload.path) or 0,
        )

        marks:  dict[str, Mark] = {}
        anchor: str | None      = None
        if plan.fixed_quality:
            tolerance    = self._config.optimization.tolerance
            names        = [r.strategy for r in summaries]
            survivors    = OptimizationPhase._dominance_survivors(summaries, tolerance)
            merge_anchor = OptimizationPhase._select_anchor(summaries, names, survivors)
            anchor       = merge_anchor
        else:
            for row in summaries:
                # The measurement opt-out renders no verdict (nothing was
                # measured); empty metrics under measurement ON stay a miss —
                # the swallowed-measurement row (§132).
                verdict: bool | None = None
                if plan.measure:
                    verdict = all(
                        (value := row.metrics.get(target.identity)) is not None and value >= target.value
                        for target in plan.targets
                    ) if row.metrics else False
                marks[row.strategy] = Mark(verdict=verdict)

        smallest = min(summaries, key=lambda r: (r.size_bytes, r.strategy)).strategy
        marks[smallest] = Mark(
            selected = True,
            verdict  = marks[smallest].verdict if smallest in marks else None,
        )

        table = ComparisonTable(
            rows        = list(summaries),
            targets     = None if plan.fixed_quality else tuple(plan.targets),
            anchor      = anchor,
            source      = source,
            marks       = marks,
            mark_legend = "recommended",
        )
        for line in render_comparison_table(table):
            logger.info(line)

        if plan.fixed_quality and anchor is not None:
            test_anchor = self._deps[OptimizationPhase].anchor
            if test_anchor == anchor:
                logger.info("Anchor stayed the same: %s", anchor)
            elif test_anchor is not None:
                logger.warning(
                    "Anchor has shifted since optimization: %s (test) → %s (full)",
                    test_anchor, anchor,
                )

        logger.info("")
        logger.info("Merged video files Location: %s", output_dir)
        if self._config.measurement.sampling > 1:
            logger.info(
                "  (Subsampling 1:%d — with fewer frames measured, there's a higher"
                " chance to miss outliers, making quality targeting less reliable)",
                self._config.measurement.sampling,
            )

    @staticmethod
    def _collect_quality_data(
        work_dir: Path,
        winners:  list[Artifact[EncodedChunk]],
        strategy: str,
    ) -> list[tuple[float, float, Decimal]]:
        """Extract ``(start_seconds, end_seconds, quality)`` tuples for a strategy's winners.

        The winning quality is a fact of the winner result sidecar (Req 3/4) —
        read here on this rare re-merge processing path, never at recovery.
        The window comes through ``payload.chunk``.

        Args:
            work_dir: The run's work dir (locates ``encoded/<strategy>/``).
            winners:  The encoding phase's winner rows.
            strategy: Strategy display name to filter by.

        Returns:
            List of ``(start_s, end_s, quality)`` sorted by start time.
        """
        result: list[tuple[float, float, Decimal]] = []
        for row in winners:
            payload = row.payload
            if payload.strategy.display_name() != strategy:
                continue
            sidecar = read_winner_sidecar(work_dir, payload)
            if sidecar is None:
                logger.warning(
                    "Winner %s has no readable sidecar — excluded "
                    "from the quality plot",
                    payload.chunk.safe_name(),
                )
                continue
            result.append((
                payload.chunk.start_timestamp,
                payload.chunk.end_timestamp,
                sidecar.quality,
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
