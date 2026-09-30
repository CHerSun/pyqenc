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

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import yaml

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
from pyqenc.models import CropParams, PhaseOutcome, QualityTarget, Strategy
from pyqenc.phase import (
    Artifact,
    ArtifactState,
    Phase,
    PhaseRegistry,
    PhaseResult,
    Recovery,
)
from pyqenc.phases.audio import AudioPhase
from pyqenc.phases.encoding import EncodingPhase
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.probe import ProbePhase
from pyqenc.state import MergeParams, MergeStrategySummary, ProbeState
from pyqenc.stream_model import EncodedChunk, ExtendedVideoStream, MergedVideo
from pyqenc.utils.ffmpeg_runner import get_frame_count
from pyqenc.utils.log_format import (
    fmt_key_value_table,
    fmt_metric_value,
)
from pyqenc.utils.long_path import LongPath
from pyqenc.utils.visualization import QualityEvaluator, create_crf_plot
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MERGE_YAML = "merge.yaml"

_NS_PER_SECOND = 1_000_000_000
_MKVPROPEDIT_VIDEO_TRACK = "track:v1"


def _targets_as_strings(targets: list[QualityTarget]) -> list[str]:
    """Serialise quality targets to ``"metric-statistic:value"`` strings."""
    return [f"{t.metric}-{t.statistic}:{t.value}" for t in targets]


def _expected_output_path(merged_dir: Path, source_stem: str, strategy: Strategy) -> Path:
    """The merged output location — the single derivation site (Req 15.8).

    ``<file stem> <strategy.safe_name()>.mkv`` below ``merged/``; names are
    safe by construction (Req 15.6).
    """
    return merged_dir / f"{source_stem} {strategy.safe_name()}.mkv"


# ---------------------------------------------------------------------------
# Sidecar model
# ---------------------------------------------------------------------------

def _sidecar_path(output_file: Path) -> Path:
    """Return the sidecar YAML path for a merged output file."""
    return output_file.with_suffix(".yaml")


def _load_merge_sidecar(output_file: Path) -> dict | None:
    """Load the merge sidecar for *output_file*, or ``None`` if absent/invalid."""
    path = _sidecar_path(output_file)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception as exc:
        logger.debug("Could not load merge sidecar %s: %s", path.name, exc)
        return None


def _targeted_metrics(
    all_metrics:     dict[str, float],
    quality_targets: list[QualityTarget],
) -> dict[str, float]:
    """Return only the metric keys that correspond to user-requested quality targets.

    Keys are in ``{metric}-{statistic}`` form (e.g. ``vmaf-min``), matching the
    CLI input format and optimization.yaml convention.
    Values are coerced to plain Python ``float`` to avoid numpy scalar serialisation artefacts.
    """
    return {
        f"{t.metric}-{t.statistic}": float(all_metrics[f"{t.metric}_{t.statistic}"])
        for t in quality_targets
        if f"{t.metric}_{t.statistic}" in all_metrics
    }


def _safe_file_size(path: Path) -> int:
    """Return the file size of *path* in bytes, or ``0`` on any OS error."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _build_strategy_summaries(
    rows:              list[Artifact[MergedVideo]],
    source_video_path: Path | None,
) -> tuple[int, list[MergeStrategySummary]]:
    """Build per-strategy summary rows and source size from complete rows.

    Args:
        rows:              The merged-output rows (complete ones are summarized).
        source_video_path: Path to the source video for size capture; ``None`` if unavailable.

    Returns:
        Tuple of ``(source_size_bytes, strategy_summaries)``.
    """
    source_size = 0
    if source_video_path is not None:
        try:
            source_size = source_video_path.stat().st_size
        except OSError:
            pass

    summaries: list[MergeStrategySummary] = []
    for row in rows:
        if row.state != ArtifactState.COMPLETE:
            continue
        payload = row.payload
        summaries.append(MergeStrategySummary(
            strategy_name   = payload.strategy.display_name(),
            output_path     = payload.output_path,
            file_size_bytes = _safe_file_size(payload.output_path),
            metrics         = payload.metrics,
            targets_met     = payload.targets_met,
        ))
    return source_size, summaries


def _write_merge_sidecar(
    output_file:     Path,
    frame_count:     int | None,
    all_metrics:     dict[str, float],
    quality_targets: list[QualityTarget],
    targets_met:     bool,
    plot_path:       Path | None,
) -> None:
    """Atomically write a merge sidecar alongside *output_file*.

    Only metrics for user-requested quality targets are persisted.
    Quality target values are written before measured metrics so the user
    can directly compare target vs. actual in the YAML.
    Keys use ``{metric}-{statistic}`` form (e.g. ``vmaf-min``) matching the CLI convention.
    """
    targets_section = {f"{t.metric}-{t.statistic}": t.value for t in quality_targets}
    metrics_section = _targeted_metrics(all_metrics, quality_targets)

    data: dict = {
        "frame_count":   frame_count,
        "targets_met":   targets_met,
        "targets":       targets_section,
        "metrics":       metrics_section,
    }
    if plot_path is not None:
        data["plot"] = str(plot_path)
    try:
        write_yaml_atomic(_sidecar_path(output_file), data)
    except Exception as exc:
        logger.warning("Could not write merge sidecar for %s: %s", output_file.name, exc)




# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _measure_quality(
    final_result:     Path,
    source_stream:    ExtendedVideoStream,
    ref_crop:         CropParams | None,
    quality_targets:  list[QualityTarget],
    output_dir:       Path,
    metrics_sampling: int,
) -> tuple[dict[str, float], bool, Path | None]:
    """Measure final quality metrics for *final_result* against *source_stream*.

    Raw metric ``.tmp`` files are written directly to ``output_dir`` and deleted
    immediately after parsing.  The quality plot PNG is written to
    ``output_dir / f"{final_result.stem}.png"`` and kept.

    Returns:
        Tuple of ``(metrics_dict, targets_met, plot_path)``.
    """
    evaluator = QualityEvaluator(output_dir)
    plot_path = output_dir / f"{final_result.stem}.png"

    evaluation = evaluator.evaluate_chunk(
        encoded            = final_result,
        reference          = source_stream.stream.as_input(),
        ref_crop           = ref_crop,
        targets            = quality_targets,
        output_dir         = output_dir,
        duration_seconds   = source_stream.stream.info.duration_seconds or 0.0,
        fps_value          = source_stream.stream.info.fps_fraction,
        metrics_output_dir = output_dir,
        subsample_factor   = metrics_sampling,
        show_progress      = True,
        plot_path          = plot_path,
    )

    metrics_dict: dict[str, float] = {}
    for metric_name, metric_stats in evaluation.metrics.items():
        for stat_name, stat_value in metric_stats.items():
            metrics_dict[f"{metric_name.value}_{stat_name}"] = stat_value

    plot_path = evaluation.logs.plot if evaluation.logs.plot else None
    return metrics_dict, evaluation.targets_met, plot_path


def _fmt_inline_metrics(
    metrics_dict:    dict[str, float],
    quality_targets: list[QualityTarget],
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
    for target in quality_targets:
        key   = f"{target.metric}_{target.statistic}"
        value = metrics_dict.get(key)
        if value is None:
            continue
        symbol = SUCCESS_SYMBOL_MINOR if value >= target.value else FAILURE_SYMBOL_MINOR
        parts.append(f"{target.metric}-{target.statistic}={fmt_metric_value(value)} {symbol}")
    return "  ".join(parts)


def _log_missed_targets_warning(
    strategy_name:  str,
    metrics_dict:   dict[str, float],
    quality_targets: list[QualityTarget],
) -> None:
    """Log a WARNING naming every target this strategy missed, with wanted vs actual.

    The completion line and the summary table stay neutral; this is the single
    place a missed target is escalated to warning level so the reason is
    immediately visible where the merge happened.

    Args:
        strategy_name:   The merged strategy.
        metrics_dict:    Measured metrics keyed by ``"{metric}_{statistic}"``.
        quality_targets: The targets that were checked.
    """
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


def _log_merge_summary(
    summaries:         list[MergeStrategySummary],
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

    sorted_summaries = sorted(summaries, key=lambda r: _safe_file_size(r.output_path))

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
        src_mb  = source_size / (1024 * 1024)
        src_str = f"{src_mb:,.1f}".replace(",", "\u202f")
        if has_targets:
            logger.info("  %-25s  %12s  %7s  %s", source_stem[:25], src_str, "100.0%", "")
        else:
            logger.info("  %-25s  %12s  %7s", source_stem[:25], src_str, "100.0%")

    # --- Strategy rows ---
    any_miss = False
    for summary in sorted_summaries:
        size_bytes = _safe_file_size(summary.output_path)
        size_mb    = size_bytes / (1024 * 1024)
        size_str   = f"{size_mb:,.1f}".replace(",", "\u202f")
        pct        = _pct_str(size_bytes)

        if has_targets:
            if summary.metrics:
                mark = SUCCESS_SYMBOL_MINOR if summary.targets_met else FAILURE_SYMBOL_MINOR
                if not summary.targets_met:
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
        if summary.targets_met or not summary.metrics:
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



def _log_merge_summary_from_params(
    params:          MergeParams,
    quality_targets: list[QualityTarget],
) -> None:
    """Replay the merge summary table from persisted ``MergeParams``.

    Delegates to ``_log_merge_summary`` over the persisted summary rows.
    Called on the REUSED path so the user sees the same table as on the
    original run.

    Args:
        params:          Loaded ``MergeParams`` from ``merge.yaml``.
        quality_targets: Current quality targets (for miss-detail rendering).
    """
    if not params.strategy_summaries:
        logger.info("  No summary data saved — re-run to generate.")
        return

    _log_merge_summary(
        summaries         = params.strategy_summaries,
        source_stem       = params.source_stem,
        source_size_bytes = params.source_size_bytes,
        quality_targets   = quality_targets,
        metrics_sampling  = params.sampling or 1,
    )


def _collect_crf_data(
    winners:  list[Artifact[EncodedChunk]],
    strategy: str,
) -> list[tuple[float, float, Decimal]]:
    """Extract ``(start_seconds, end_seconds, crf)`` tuples for a strategy's winners.

    Reads the winning attempts via their payloads — ``payload.crf`` and the
    window through ``payload.chunk`` (the chunk id re-parser is gone: chunk-id
    parsing belongs to :meth:`VideoStreamChunk.parse_chunk_id`).

    Args:
        winners:  The encoding phase's winner rows.
        strategy: Strategy display name to filter by.

    Returns:
        List of ``(start_s, end_s, crf)`` sorted by start time.
    """
    result: list[tuple[float, float, Decimal]] = [
        (payload.chunk.start_timestamp, payload.chunk.end_timestamp, payload.crf)
        for payload in (row.payload for row in winners)
        if payload.strategy.display_name() == strategy
    ]
    result.sort(key=lambda t: t[0])
    return result





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


# ---------------------------------------------------------------------------
# MergePhase
# ---------------------------------------------------------------------------

class MergePhase(Phase[MergePhaseResult]):
    """Phase object for final video merging.

    Owns artifact enumeration, recovery, execution, and logging for the merge
    phase.  Wraps the existing ``merge_final_video`` helper. The uniform run
    footprint is inherited from :class:`Phase`.

    In pipeline mode encoded chunks are read directly from
    ``EncodingPhase.result`` without rescanning the filesystem.  In standalone
    mode the phase scans ``encoded/<strategy.safe_name()>/`` for each strategy.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name:        str       = "merge"
    DEPENDS_ON:  ClassVar[tuple[type[Phase], ...]] = (
        JobPhase, ExtractionPhase, ProbePhase, EncodingPhase, AudioPhase,
    )
    _METRIC_KEY: MetricKey = MetricKey.MERGE

    def __init__(
        self,
        config:    AppConfig,
        phases:    PhaseRegistry | None = None,
        *,
        collector: MetricsCollector,
    ) -> None:
        super().__init__(config, phases, collector=collector)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def params(self) -> MergeParams:
        """Current merge params derived from the job result config and probe result.

        Built at runtime from ``self._dep(JobPhase).result`` and ``self._dep(ProbePhase).result``
        so the values are always current (e.g. after CLI overrides) rather than
        snapshotted at construction time.
        """
        probe: ProbeState | None = None
        probe_result = self._dep_result(ProbePhase)
        if probe_result is not None:
            probe = ProbeState(
                frame_count = probe_result.stream.payload.frame_count if probe_result.stream is not None else 0,
                crop        = probe_result.crop,
            )

        job_result = self._dep_result(JobPhase)
        if job_result is not None:
            return MergeParams(
                quality_targets  = _targets_as_strings(job_result.config.encoding.resolved_targets),
                sampling = job_result.config.measurement.sampling,
                probe            = probe,
            )
        # Fallback: empty params before job result is available
        return MergeParams(quality_targets=[], sampling=1)

    # ------------------------------------------------------------------
    # Phase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the source stem and quality targets (key parameters)."""
        logger.info("Source stem:  %s", self._dep_result(JobPhase).source.stem)
        if self._config.encoding.resolved_targets:
            logger.info("Targets:      %s", ", ".join(
                f"{t.metric}-{t.statistic}≥{t.value}" for t in self._config.encoding.resolved_targets
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
            a for a in self._dep_result(EncodingPhase).winners
            if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)
        ]
        if incomplete:
            err = f"EncodingPhase has {len(incomplete)} incomplete artifact(s) — cannot merge"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err)

        return None

    def _recover(self) -> Recovery:
        """Classify merge rows and handle force-wipe / param invalidation.

        Steps:
        1. If ``force_wipe``: delete ``merged/`` and ``merge.yaml``.
        2. Detect quality-target / metrics_sampling change — delete per-output
           sidecars so stale COMPLETE rows are reclassified as PARTIAL
           and the merge re-runs with fresh metrics. A probe change deletes
           the whole ``merged/`` (outputs are re-merged).
        3. Clean up leftover ``.tmp`` files.
        4. Determine expected strategies from the encoding winners.
        5. Classify each expected output: COMPLETE (output + sidecar; the
           measured facts load into the payload), PARTIAL (output without its
           sidecar), ABSENT (not yet produced). Output files not matching any
           expected strategy surface as ``wanted=False`` rows (kept in place;
           deletion only via explicit cleanup).

        Returns:
            The :class:`Recovery` single source of truth.
        """
        from pyqenc.stream_model import File

        job_result = self._dep_result(JobPhase)
        work_dir   = job_result.work_dir
        merged_dir = work_dir / MERGED_OUTPUT_DIR
        merge_yaml = work_dir / _MERGE_YAML
        force_wipe = job_result.force_wipe

        # Step 1: force-wipe
        if force_wipe:
            if merged_dir.exists():
                shutil.rmtree(merged_dir)
                logger.debug("force_wipe: deleted %s", merged_dir)
            merge_yaml.unlink(missing_ok=True)
            logger.debug("force_wipe: deleted %s", merge_yaml)

        # Step 2: quality-target / metrics_sampling change detection.
        # When params change, delete all per-output sidecars so every row
        # is reclassified as PARTIAL and the merge re-runs with fresh metrics.
        if not force_wipe and merged_dir.exists():
            persisted = MergeParams.load(merge_yaml)
            if persisted is not None and persisted != self.params:
                targets_changed  = bool(persisted.quality_targets) and persisted.quality_targets != self.params.quality_targets
                sampling_changed = persisted.sampling is not None and persisted.sampling != self.params.sampling
                probe_changed    = (
                    persisted.probe is not None
                    and self.params.probe is not None
                    and persisted.probe != self.params.probe
                )
                if targets_changed or sampling_changed:
                    logger.info(
                        "Merge params changed (%s) — deleting merge sidecars to re-measure quality",
                        "quality targets" if targets_changed else "sampling",
                    )
                    for sidecar in merged_dir.glob("*.yaml"):
                        try:
                            sidecar.unlink()
                            logger.debug("Deleted stale merge sidecar: %s", sidecar.name)
                        except OSError as exc:
                            logger.warning("Could not delete merge sidecar %s: %s", sidecar.name, exc)
                    merge_yaml.unlink(missing_ok=True)
                elif probe_changed:
                    logger.warning(
                        "Probe params changed since last merge run "
                        "(persisted=%s, current=%s) — deleting merge artifacts to re-merge",
                        persisted.probe, self.params.probe,
                    )
                    if merged_dir.exists():
                        shutil.rmtree(merged_dir)
                        logger.debug("Probe mismatch: deleted %s", merged_dir)
                    merge_yaml.unlink(missing_ok=True)

        # Step 3: clean up .tmp files
        if merged_dir.exists():
            for tmp in merged_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 4: determine expected strategies from the typed winners field
        strategies = self._get_expected_strategies()
        if not strategies:
            return Recovery()

        source_stem = job_result.source.stem

        # Step 5: classify each expected output — the output name derives at
        # the single site from File.path.stem + the strategy's safe name.
        rows: list[Artifact] = []
        expected_names: set[str] = set()
        for strategy in strategies:
            output_file = _expected_output_path(merged_dir, source_stem, strategy)
            expected_names.add(output_file.name)
            sidecar = _load_merge_sidecar(output_file)

            if output_file.exists() and sidecar is not None:
                # COMPLETE — output and sidecar both present
                frame_count = sidecar.get("frame_count")
                metrics     = {k: float(v) for k, v in sidecar.get("metrics", {}).items()}
                targets_met = bool(sidecar.get("targets_met", False))
                plot_path: Path | None = None
                if sidecar.get("plot"):
                    p = Path(sidecar["plot"])
                    if p.exists():
                        plot_path = p

                rows.append(Artifact(
                    payload = MergedVideo(
                        source_stem = source_stem,
                        strategy    = strategy,
                        output_path = LongPath(output_file),
                        frame_count = int(frame_count) if frame_count is not None else None,
                        metrics     = metrics,
                        targets_met = targets_met,
                        plot_path   = LongPath(plot_path) if plot_path is not None else None,
                    ),
                    state   = ArtifactState.COMPLETE,
                ))
            elif output_file.exists():
                # PARTIAL — output present but its sidecar missing
                rows.append(Artifact(
                    payload = MergedVideo(
                        source_stem = source_stem,
                        strategy    = strategy,
                        output_path = LongPath(output_file),
                    ),
                    state   = ArtifactState.PARTIAL,
                ))
            else:
                # ABSENT — not yet produced
                rows.append(Artifact(
                    payload = MergedVideo(
                        source_stem = source_stem,
                        strategy    = strategy,
                        output_path = LongPath(output_file),
                    ),
                    state   = ArtifactState.ABSENT,
                ))

        # Surface present-but-unwanted surplus outputs (a strategy dropped from
        # the selection whose merged file still exists). The producing entity
        # no longer exists — the on-disk product itself (a File) is the
        # payload. Retained in place, never pending; deletion only via
        # explicit cleanup.
        if merged_dir.exists():
            for output_file in sorted(merged_dir.glob("*.mkv")):
                if output_file.name in expected_names:
                    continue
                state = (
                    ArtifactState.COMPLETE
                    if _load_merge_sidecar(output_file) is not None
                    else ArtifactState.PARTIAL
                )
                rows.append(Artifact(
                    payload = File(path=LongPath(output_file), file_size_bytes=_safe_file_size(output_file)),
                    state   = state,
                    wanted  = False,
                ))
                logger.debug("Merged output %s is surplus (strategy no longer selected) — unwanted", output_file.name)

        return Recovery.from_artifacts(rows)

    def _reused_result(self, wanted: list[Artifact], message: str) -> MergePhaseResult:
        """Build the reused result, replaying the persisted merge summary."""
        merge_yaml = self._dep_result(JobPhase).work_dir / _MERGE_YAML
        persisted  = MergeParams.load(merge_yaml)
        if persisted is not None:
            logger.info(THICK_LINE)
            logger.info("MERGE SUMMARY")
            logger.info(THICK_LINE)
            _log_merge_summary_from_params(persisted, self._config.encoding.resolved_targets)
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

    def _get_expected_strategies(self) -> list[Strategy]:
        """The strategies expected to have merged outputs.

        Reads the already-cached ``EncodingPhase.result`` winners — the
        winning :class:`~pyqenc.stream_model.EncodedChunk` payloads composed
        with their :class:`~pyqenc.models.Strategy` — resolved once by the
        shared dependency walk.

        Returns:
            The distinct strategies, in first-seen order.
        """
        winners = self._dep_result(EncodingPhase).winners
        seen: dict[str, Strategy] = {}
        for row in winners:
            strategy = row.payload.strategy
            seen.setdefault(strategy.safe_name(), strategy)
        return list(seen.values())

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

        Args:
            wanted:  Wanted merged-output rows from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``MergePhaseResult`` after merging.
        """
        from pyqenc.metrics import MetricKey

        rows = wanted
        work_dir   = self._dep_result(JobPhase).work_dir
        merged_dir = work_dir / MERGED_OUTPUT_DIR
        merged_dir.mkdir(parents=True, exist_ok=True)

        job_result = self._dep_result(JobPhase)
        probe_result = self._dep_result(ProbePhase)
        crop: CropParams | None = probe_result.crop if probe_result is not None else None
        source_stream: ExtendedVideoStream | None = (
            probe_result.stream.payload if (probe_result is not None and probe_result.stream is not None) else None
        )
        source_frame_count: int = (
            probe_result.stream.payload.frame_count
            if (probe_result is not None and probe_result.stream is not None) else 0
        )
        source_stem = job_result.source.stem

        # Build encoded_chunks dict from EncodingPhase result
        encoded_chunks = self._collect_encoded_chunks()

        final_rows: list[Artifact[MergedVideo]] = []
        failed_strategies: list[str] = []

        for artifact in rows:
            payload      = artifact.payload
            strategy     = payload.strategy
            strategy_name = strategy.display_name()

            if artifact.state == ArtifactState.COMPLETE:
                final_rows.append(artifact)
                continue

            # The merge output name derives at the single site (Req 15.8).
            output_file = _expected_output_path(merged_dir, source_stem, strategy)
            assert output_file == payload.output_path, "recovery derived the same location"
            logger.info("Merging: %s", strategy_name)

            try:
                # Collect and sort chunks for this strategy
                strategy_chunks: list[Path] = sorted(
                    (
                        encoded_chunks[chunk_id][strategy_name].stream.stream.file.path
                        for chunk_id in sorted(encoded_chunks.keys())
                        if strategy_name in encoded_chunks[chunk_id]
                    ),
                    key=lambda p: p.name,
                )

                if not strategy_chunks:
                    logger.error("No encoded chunks found for strategy %s — skipping", strategy_name)
                    failed_strategies.append(strategy_name)
                    continue

                logger.info("  Starting concatenation of %d chunks...", len(strategy_chunks))

                # Resolve timestamps path from ExtractionPhase result
                timestamps_path: Path | None = (
                    self._dep(ExtractionPhase).result.timestamps_path
                )

                if timestamps_path is None or not timestamps_path.exists():
                    logger.critical(
                        "timestamps.txt not found — cannot restore PTS. "
                        "Re-run the extraction phase to generate it."
                    )
                    failed_strategies.append(strategy_name)
                    continue

                # Write mkvmerge options file
                options_file = merged_dir / f"concat_{strategy_name}.json"
                args = _build_mkvmerge_options(strategy_chunks, output_file, timestamps_path)
                _write_mkvmerge_options_file(options_file, args)

                # Run mkvmerge via options file (avoids OS command-line length limits).
                # The "@<file>" option embeds the path in a sub-string mkvmerge
                # parses itself — plain form only, no extended-length prefix.
                cmd_mkvmerge: list[str | os.PathLike] = ["mkvmerge", f"@{options_file}"]
                logger.debug("mkvmerge command: %s", " ".join(str(a) for a in cmd_mkvmerge))

                with self._collector.time(MetricKey.MERGE, "concat"):
                    mkvmerge_result = subprocess.run(
                        cmd_mkvmerge, capture_output=True, text=True
                    )

                if mkvmerge_result.returncode != 0:
                    logger.error(
                        "mkvmerge failed for strategy %s (exit %d)",
                        strategy_name, mkvmerge_result.returncode,
                    )
                    for line in mkvmerge_result.stderr.splitlines()[-20:]:
                        logger.error("mkvmerge stderr: %s", line)
                    # Leave options file on disk for debugging
                    failed_strategies.append(strategy_name)
                    continue

                # Delete options file on success
                options_file.unlink(missing_ok=True)

                logger.debug("  Concatenation complete: %s", output_file.name)

                # Restore the true frame rate in the track header —
                # see _build_mkvpropedit_args for why mkvmerge cannot do it.
                propedit_cmd: list[str | os.PathLike] = _build_mkvpropedit_args(
                    output_file, source_stream.stream.info.fps_fraction,
                )
                propedit_result = subprocess.run(propedit_cmd, capture_output=True, text=True, check=False)
                if propedit_result.returncode != 0:
                    logger.error(
                        "mkvpropedit failed for strategy %s (exit %d) — frame-rate header not restored",
                        strategy_name, propedit_result.returncode,
                    )
                    for line in propedit_result.stderr.splitlines()[-20:]:
                        logger.error("mkvpropedit stderr: %s", line)
                    failed_strategies.append(strategy_name)
                    continue

                # Verify frame count
                frame_count:       int | None = None
                frame_count_ok:    bool       = False
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
                except Exception as exc:
                    logger.warning("  Could not verify frame count: %s", exc)

                # Measure quality
                metrics_dict: dict[str, float] = {}
                targets_met:  bool             = False
                plot_path:    Path | None       = None

                if source_stream is not None and job_result.config.encoding.resolved_targets:
                    try:
                        with self._collector.time(MetricKey.MERGE, METRIC_KEY_QUALITY_MEASURE):
                            metrics_dict, targets_met, plot_path = _measure_quality(
                                final_result     = output_file,
                                source_stream    = source_stream,
                                ref_crop         = crop,
                                quality_targets  = job_result.config.encoding.resolved_targets,
                                output_dir       = merged_dir,
                                metrics_sampling = self._dep_result(JobPhase).config.measurement.sampling,
                            )
                    except Exception as exc:
                        logger.warning("  Could not measure quality: %s", exc)

                # CRF distribution plot — reads the typed winners field
                crf_data = _collect_crf_data(
                    self._dep_result(EncodingPhase).winners,
                    strategy_name,
                )
                if crf_data:
                    crf_plot_path = merged_dir / f"{output_file.stem}.crf.png"
                    try:
                        qlabel = self._dep(EncodingPhase).quality_labels.get(strategy_name, "CRF")
                        create_crf_plot(
                            chunks        = crf_data,
                            output_path   = crf_plot_path,
                            title         = f"{qlabel}\n{output_file.stem.replace(TIME_SEPARATOR_MS, ".").replace(TIME_SEPARATOR_SAFE, ":")}",
                            quality_label = qlabel,
                        )
                        logger.debug("  CRF plot saved: %s", crf_plot_path.name)
                    except Exception as exc:
                        logger.warning("  Could not generate CRF plot: %s", exc)
                else:
                    logger.warning("No CRF data available for strategy %s — skipping CRF plot", strategy_name)

                # Write sidecar (marks this output as COMPLETE)
                _write_merge_sidecar(
                    output_file     = output_file,
                    frame_count     = frame_count,
                    all_metrics     = metrics_dict,
                    quality_targets = self._dep_result(JobPhase).config.encoding.resolved_targets,
                    targets_met     = targets_met,
                    plot_path       = plot_path,
                )

                frames_sym  = SUCCESS_SYMBOL_MINOR if frame_count_ok else FAILURE_SYMBOL_MINOR
                frames_str  = str(frame_count) if frame_count is not None else "unknown"
                metrics_str = _fmt_inline_metrics(metrics_dict, self._dep_result(JobPhase).config.encoding.resolved_targets)
                logger.info(
                    "%s Merged %s:  frames=%s %s%s",
                    SUCCESS_SYMBOL_MAJOR, strategy_name, frames_str, frames_sym,
                    f"  {metrics_str}" if metrics_str else "",
                )
                if metrics_dict and not targets_met:
                    _log_missed_targets_warning(
                        strategy_name, metrics_dict,
                        self._dep_result(JobPhase).config.encoding.resolved_targets,
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

            except Exception as exc:  # one bad strategy must not kill the rest
                logger.error("Merging strategy %s error: %s", strategy_name, exc, exc_info=True)
                failed_strategies.append(strategy_name)

        # Phase completion summary
        complete_count = sum(1 for a in final_rows if a.state == ArtifactState.COMPLETE)
        logger.info(THICK_LINE)
        logger.info("MERGE SUMMARY")
        logger.info(THICK_LINE)
        if failed_strategies:
            logger.error("  Failed strategies: %s", ", ".join(failed_strategies))
        _, strategy_summaries = _build_strategy_summaries(
            final_rows,
            source_stream.stream.file.path if source_stream is not None else None,
        )
        _log_merge_summary(
            summaries          = strategy_summaries,
            source_stem        = source_stem,
            source_size_bytes  = (
                _safe_file_size(source_stream.stream.file.path) if source_stream is not None else 0
            ),
            quality_targets    = self._dep_result(JobPhase).config.encoding.resolved_targets,
            metrics_sampling   = self._dep_result(JobPhase).config.measurement.sampling,
        )
        if failed_strategies and not final_rows:
            return self._make_result(PhaseOutcome.FAILED, [], "All strategy merges failed")

        # Persist merge params (with summary) so quality-target / sampling changes are
        # detected next run and the summary table can be replayed on rerun.
        if complete_count > 0:
            source_size_bytes, strategy_summaries = _build_strategy_summaries(
                final_rows,
                source_stream.stream.file.path if source_stream is not None else None,
            )
            MergeParams(
                quality_targets    = self.params.quality_targets,
                sampling           = self.params.sampling,
                probe              = self.params.probe,
                source_stem        = source_stem,
                source_size_bytes  = source_size_bytes,
                strategy_summaries = strategy_summaries,
            ).save(self._dep_result(JobPhase).work_dir / _MERGE_YAML)

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

    def _collect_encoded_chunks(self) -> dict[str, dict[str, EncodedChunk]]:
        """Read the winning ``EncodedChunk`` objects from ``EncodingPhase.result``.

        The composed objects are resolved once by the shared dependency walk —
        path via ``stream.file.path`` (Req 14: no duplicated fields).

        Returns:
            Nested dict mapping chunk IDs to strategy-name-to-``EncodedChunk``.
        """
        return self._dep_result(EncodingPhase).encoded_chunks


# ---------------------------------------------------------------------------
# MergePhase module-level helpers
# ---------------------------------------------------------------------------

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
        output:          Destination output MKV path.
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


def _default_duration_ns(fps: Fraction) -> int:
    """Convert *fps* to a track-header ``DefaultDuration`` in nanoseconds.

    Exact rational arithmetic: the float path drifts at NTSC rates
    (24000/1001 → 41 708 333.33 ns).
    """
    return round(_NS_PER_SECOND / fps)


def _build_mkvpropedit_args(output: Path, fps: Fraction) -> list[str | os.PathLike]:
    """Build the mkvpropedit argument list restoring the video track's frame-rate header.

    mkvmerge derives ``DefaultDuration`` from the ms-rounded timestamps that
    ``--timestamps`` restores (observed 42 ms → 500/21 for a 24000/1001
    stream).  Header-only consumers then misdeclare the frame rate and flag
    the output VFR (MediaInfo: "Frame rate mode: Variable").  The edit is
    instant, does not touch block data, and takes an integer ns value.

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
        "--edit", _MKVPROPEDIT_VIDEO_TRACK,
        "--set", f"default-duration={_default_duration_ns(fps)}",
    ]


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
