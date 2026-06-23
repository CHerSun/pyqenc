"""
Merging phase for the quality-based encoding pipeline.

This module handles concatenation of encoded video chunks to produce final
MKV output files.  It also measures final quality metrics and generates
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
from pathlib import Path
from typing import TYPE_CHECKING, cast

import yaml

from pyqenc.constants import (
    ENCODED_ATTEMPT_NAME_PATTERN,
    FAILURE_SYMBOL_MINOR,
    FINAL_OUTPUT_DIR,
    METRIC_KEY_QUALITY_MEASURE,
    RANGE_SEPARATOR,
    SUCCESS_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
    THICK_LINE,
    TIME_SEPARATOR_MS,
    TIME_SEPARATOR_SAFE,
    WARNING_SYMBOL,
)
from pyqenc.models import CropParams, PhaseOutcome, QualityTarget, VideoMetadata
from pyqenc.phase import Artifact, ArtifactState, Phase, PhaseResult
from pyqenc.state import MergeParams, MergeStrategySummary
from pyqenc.utils.ffmpeg_runner import get_frame_count, run_ffmpeg
from pyqenc.utils.log_format import (
    emit_phase_banner,
    fmt_key_value_table,
    fmt_metric_value,
    log_recovery_line,
)
from pyqenc.utils.visualization import QualityEvaluator, create_crf_plot
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.metrics import MetricsCollector
    from pyqenc.models import PipelineConfig
    from pyqenc.phases.audio import AudioPhase
    from pyqenc.phases.encoding import (
        EncodedArtifact,
        EncodingPhase,
    )
    from pyqenc.phases.extraction import ExtractionPhase
    from pyqenc.phases.job import JobPhase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_MERGE_YAML = "merge.yaml"


def _targets_as_strings(targets: list[QualityTarget]) -> list[str]:
    """Serialise quality targets to ``"metric-statistic:value"`` strings."""
    return [f"{t.metric}-{t.statistic}:{t.value}" for t in targets]


# ---------------------------------------------------------------------------
# MergeArtifact
# ---------------------------------------------------------------------------

@dataclass
class MergeArtifact(Artifact):
    """Artifact for a single merged output file.

    Attributes:
        strategy_name: Display name of the encoding strategy (e.g. ``slow+h265-aq``).
        frame_count:   Frame count of the merged output; ``None`` until measured.
        metrics:       Final quality metrics dict; empty until measured.
        targets_met:   Whether quality targets were met; ``False`` until measured.
        plot_path:     Path to the quality plot PNG; ``None`` if not produced.
    """

    strategy_name: str        = ""
    frame_count:   int | None = None
    metrics:       dict[str, float] = field(default_factory=dict)
    targets_met:   bool             = False
    plot_path:     Path | None      = None


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
    artifacts:         list["MergeArtifact"],
    source_video_path: Path | None,
) -> tuple[int, list[MergeStrategySummary]]:
    """Build per-strategy summary rows and source size from completed artifacts.

    Args:
        artifacts:         Completed ``MergeArtifact`` objects.
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
    for artifact in artifacts:
        if artifact.state != ArtifactState.COMPLETE:
            continue
        summaries.append(MergeStrategySummary(
            strategy_name   = artifact.strategy_name,
            output_path     = artifact.path,
            file_size_bytes = _safe_file_size(artifact.path),
            metrics         = artifact.metrics,
            targets_met     = artifact.targets_met,
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
    source_video:     VideoMetadata,
    ref_crop:         CropParams | None,
    quality_targets:  list[QualityTarget],
    output_dir:       Path,
    metrics_sampling: int,
) -> tuple[dict[str, float], bool, Path | None]:
    """Measure final quality metrics for *final_result* against *source_video*.

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
        reference          = source_video.path,
        ref_crop           = ref_crop,
        targets            = quality_targets,
        output_dir         = output_dir,
        metrics_output_dir = output_dir,
        subsample_factor   = metrics_sampling,
        show_progress      = True,
        plot_path          = plot_path,
    )

    metrics_dict: dict[str, float] = {}
    for metric_name, metric_stats in evaluation.metrics.items():
        for stat_name, stat_value in metric_stats.items():
            metrics_dict[f"{metric_name.value}_{stat_name}"] = stat_value

    plot_path = evaluation.artifacts.plot if evaluation.artifacts.plot else None
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


def _log_merge_summary(
    artifacts:        list[MergeArtifact],
    source_stem:      str,
    source_size_bytes: int,
    quality_targets:  list[QualityTarget],
    metrics_sampling: int,
) -> None:
    """Log the final merge summary: source row + strategy table with sizes, % of source,
    and quality pass/miss marks; followed by a targets reminder and per-miss details.

    Args:
        artifacts:         Completed merge artifacts, sorted by file size ascending.
        source_stem:       Source video stem (filename without extension).
        source_size_bytes: Size of the source video in bytes; ``0`` if unavailable.
        quality_targets:   Quality targets that were checked.
        metrics_sampling:  Frame subsampling factor used during measurement.
    """
    if not artifacts:
        logger.info("  No output files produced.")
        return

    source_size = source_size_bytes
    has_targets = bool(quality_targets)

    sorted_artifacts = sorted(artifacts, key=lambda a: _safe_file_size(a.path))

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
    for artifact in sorted_artifacts:
        size_bytes = _safe_file_size(artifact.path)
        size_mb    = size_bytes / (1024 * 1024)
        size_str   = f"{size_mb:,.1f}".replace(",", "\u202f")
        pct        = _pct_str(size_bytes)

        if has_targets:
            if artifact.metrics:
                mark = SUCCESS_SYMBOL_MINOR if artifact.targets_met else FAILURE_SYMBOL_MINOR
                if not artifact.targets_met:
                    any_miss = True
            else:
                mark = "-"
            logger.info("  %-25s  %12s  %7s  %s", artifact.strategy_name[:25], size_str, pct, mark)
        else:
            logger.info("  %-25s  %12s  %7s", artifact.strategy_name[:25], size_str, pct)

    # --- Output location note ---
    output_dir = sorted_artifacts[0].path.parent
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

    for artifact in sorted_artifacts:
        if artifact.targets_met or not artifact.metrics:
            continue
        missed_lines = []
        for target in quality_targets:
            key   = f"{target.metric}_{target.statistic}"
            value = artifact.metrics.get(key)
            if value is None:
                missed_lines.append(f"{target.metric}-{target.statistic}: not measured (target: {target.value:.2f})")
            elif value < target.value:
                missed_lines.append(f"{target.metric}-{target.statistic}: {value:.2f} (target: {target.value:.2f})")
        if missed_lines:
            miss_table[f"{WARNING_SYMBOL} {artifact.strategy_name}"] = missed_lines if len(missed_lines) > 1 else missed_lines[0]

    fmt_key_value_table(miss_table)

    if miss_table and metrics_sampling > 1:
        logger.info("  (Subsampling 1:%d — with fewer frames measured, there's a higher chance to miss outliers, making quality targeting less reliable)", metrics_sampling)



def _log_merge_summary_from_params(
    params:          MergeParams,
    quality_targets: list[QualityTarget],
) -> None:
    """Replay the merge summary table from persisted ``MergeParams``.

    Reconstructs ``MergeArtifact`` objects from ``params.strategy_summaries``
    and delegates to ``_log_merge_summary``.  Called on the REUSED path so the
    user sees the same table as on the original run.

    Args:
        params:          Loaded ``MergeParams`` from ``merge.yaml``.
        quality_targets: Current quality targets (for miss-detail rendering).
    """
    if not params.strategy_summaries:
        logger.info("  No summary data saved — re-run to generate.")
        return

    artifacts: list[MergeArtifact] = [
        MergeArtifact(
            path          = s.output_path,
            state         = ArtifactState.COMPLETE,
            strategy_name = s.strategy_name,
            metrics       = s.metrics,
            targets_met   = s.targets_met,
        )
        for s in params.strategy_summaries
    ]

    _log_merge_summary(
        artifacts         = artifacts,
        source_stem       = params.source_stem,
        source_size_bytes = params.source_size_bytes,
        quality_targets   = quality_targets,
        metrics_sampling  = params.metrics_sampling or 1,
    )


def _collect_crf_data(
    encoded:     "list[EncodedArtifact]",
    strategy:    str,
) -> list[tuple[float, float, Decimal]]:
    """Extract ``(start_seconds, end_seconds, crf)`` tuples for winning chunks of *strategy*.

    Timestamps are parsed from the ``chunk_id`` stem, which encodes the range
    as ``HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm`` using filesystem-safe separators.

    Args:
        encoded:  All ``EncodedArtifact`` objects from the encoding phase.
        strategy: Strategy name to filter by.

    Returns:
        List of ``(start_s, end_s, crf)`` sorted by start time.
        Chunks with missing CRF or unparseable IDs are silently skipped.
    """
    def _parse_ts(ts_str: str) -> float:
        """Parse ``HH꞉MM꞉SS․mmm`` into seconds."""
        parts = ts_str.split(TIME_SEPARATOR_SAFE)
        if len(parts) != 3:
            raise ValueError(f"Unexpected timestamp format: {ts_str!r}")
        h, m, s_ms = parts
        s, ms = s_ms.split(TIME_SEPARATOR_MS)
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    result: list[tuple[float, float, float]] = []
    for artifact in encoded:
        if artifact.strategy != strategy:
            continue

        crf = artifact.crf
        if crf is None:
            # Fallback: parse quality from the artifact filename (e.g. "…q26.5.mkv")
            m = ENCODED_ATTEMPT_NAME_PATTERN.match(artifact.path.name)
            if m:
                try:
                    crf = Decimal(str(m.group("quality")))
                except (ValueError, IndexError):
                    pass

        if crf is None:
            logger.debug("No CRF available for chunk %r — skipping", artifact.chunk_id)
            continue

        try:
            start_str, end_str = artifact.chunk_id.split(RANGE_SEPARATOR, 1)
            start_s = _parse_ts(start_str)
            end_s   = _parse_ts(end_str)
            result.append((start_s, end_s, crf))
        except Exception as exc:
            logger.debug("Could not parse chunk_id %r for CRF plot: %s", artifact.chunk_id, exc)

    result.sort(key=lambda t: t[0])
    return result





# ---------------------------------------------------------------------------
# MergePhaseResult
# ---------------------------------------------------------------------------

@dataclass
class MergePhaseResult(PhaseResult):
    """``PhaseResult`` subclass carrying merge-specific payload.

    Attributes:
        merged: All ``MergeArtifact`` objects produced by this phase.
    """

    merged: list[MergeArtifact] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MergePhase
# ---------------------------------------------------------------------------

class MergePhase:
    """Phase object for final video merging.

    Owns artifact enumeration, recovery, execution, and logging for the merge
    phase.  Wraps the existing ``merge_final_video`` helper.

    In pipeline mode encoded chunks are read directly from
    ``EncodingPhase.result`` without rescanning the filesystem.  In standalone
    mode the phase scans ``encoded/<strategy.safe_name>/`` for each strategy.

    Args:
        config: Full pipeline configuration.
        phases: Phase registry; used to resolve typed dependency references.
    """

    name: str = "merge"

    def __init__(
        self,
        config:    "PipelineConfig",
        phases:    "dict[type[Phase], Phase] | None" = None,
        *,
        collector: "MetricsCollector",
    ) -> None:
        from pyqenc.phases.audio import AudioPhase
        from pyqenc.phases.encoding import EncodingPhase
        from pyqenc.phases.extraction import ExtractionPhase
        from pyqenc.phases.job import JobPhase

        self._config:     "PipelineConfig"         = config
        self._collector:  "MetricsCollector"       = collector
        self._job:        JobPhase | None          = cast(JobPhase,        phases[JobPhase])        if phases else None
        self._extraction: ExtractionPhase | None   = cast(ExtractionPhase, phases[ExtractionPhase]) if phases else None
        self._encoding:   EncodingPhase | None     = cast(EncodingPhase,   phases[EncodingPhase])   if phases else None
        self._audio:      AudioPhase | None        = cast(AudioPhase,      phases[AudioPhase])      if phases else None
        self.params      = MergeParams(
            quality_targets  = _targets_as_strings(config.quality_targets),
            metrics_sampling = config.metrics_sampling,
        )
        self.result:      "MergePhaseResult | None"   = None
        self.dependencies: "list[Phase]"              = [
            d for d in [self._job, self._extraction, self._encoding, self._audio]
            if d is not None
        ]

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def scan(self) -> "MergePhaseResult":
        """Classify existing merge artifacts without executing any work.

        Returns:
            ``MergePhaseResult`` with all artifacts classified.
        """
        if self.result is not None:
            return self.result

        dep_result = self._ensure_dependencies(execute=False)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        artifacts = self._recover(force_wipe=force_wipe, execute=False)
        outcome   = _outcome_from_artifacts(artifacts, did_work=False)

        self.result = MergePhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = _recovery_message(artifacts),
            merged    = artifacts,
        )
        return self.result

    def run(self, dry_run: bool = False) -> "MergePhaseResult":
        """Recover, merge pending strategies, cache and return result.

        Sequence:
        1. Emit phase banner.
        2. Ensure dependencies have results.
        3. Run ``_recover()`` — handles ``force_wipe``.
        4. Log recovery result line.
        5. In dry-run mode: return ``DRY_RUN`` if any artifacts are pending.
        6. Merge pending strategies.
        7. Log completion summary.

        Args:
            dry_run: When ``True``, report what would be done without merging.

        Returns:
            ``MergePhaseResult`` with all artifacts ``COMPLETE`` on success.
        """
        emit_phase_banner("MERGE", logger)

        dep_result = self._ensure_dependencies(execute=True)
        if dep_result is not None:
            self.result = dep_result
            return self.result

        job_result = self._job.result  # type: ignore[union-attr]
        force_wipe = getattr(job_result, "force_wipe", False)

        # Key parameters
        logger.info("Source stem:  %s", self._config.source_video.stem)
        if self._config.quality_targets:
            logger.info("Targets:      %s", ", ".join(
                f"{t.metric}-{t.statistic}≥{t.value}" for t in self._config.quality_targets
            ))

        from pyqenc.metrics import MetricKey

        with self._collector.time(MetricKey.RECOVERY):
            artifacts = self._recover(force_wipe=force_wipe, execute=True)

        complete_count = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
        pending_count  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
        log_recovery_line(logger, complete_count, pending_count)

        # Dry-run path
        if dry_run:
            outcome = PhaseOutcome.REUSED if pending_count == 0 else PhaseOutcome.DRY_RUN
            self.result = MergePhaseResult(
                outcome   = outcome,
                artifacts = artifacts,
                message   = "dry-run",
                merged    = artifacts,
            )
            return self.result

        # Nothing to do
        if pending_count == 0:
            merge_yaml = self._config.work_dir / _MERGE_YAML
            persisted  = MergeParams.load(merge_yaml)
            if persisted is not None:
                logger.info(THICK_LINE)
                logger.info("MERGE SUMMARY")
                logger.info(THICK_LINE)
                _log_merge_summary_from_params(persisted, self._config.quality_targets)
            self.result = MergePhaseResult(
                outcome   = PhaseOutcome.REUSED,
                artifacts = artifacts,
                message   = "all merge artifacts reused",
                merged    = artifacts,
            )
            return self.result

        # Execute merging
        from pyqenc.metrics import MetricKey
        with self._collector.time(MetricKey.MERGE):
            result = self._execute_merge(artifacts)
        self.result = result
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_dependencies(self, execute: bool) -> "MergePhaseResult | None":
        """Scan/run dependencies if they have no cached result; fail fast if incomplete.

        Args:
            execute: When ``True``, call ``dep.run()`` for deps without a cached result.

        Returns:
            A ``FAILED`` result if any dependency is not complete; ``None`` otherwise.
        """
        if self._job is None:
            return _failed("MergePhase requires JobPhase")

        if self._job.result is None:
            if execute:
                self._job.run()
            else:
                self._job.scan()

        if not self._job.result.is_complete:  # type: ignore[union-attr]
            err = "JobPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        if self._extraction is None:
            return _failed("MergePhase requires ExtractionPhase")

        if self._extraction.result is None:
            if execute:
                self._extraction.run()
            else:
                self._extraction.scan()

        if not self._extraction.result.is_complete:  # type: ignore[union-attr]
            err = "ExtractionPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        if self._encoding is None:
            return _failed("MergePhase requires EncodingPhase")

        if self._encoding.result is None:
            if execute:
                self._encoding.run()
            else:
                self._encoding.scan()

        if not self._encoding.result.is_complete:  # type: ignore[union-attr]
            err = "EncodingPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        incomplete = [
            a for a in self._encoding.result.encoded  # type: ignore[union-attr]
            if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY)
        ]
        if incomplete:
            err = f"EncodingPhase has {len(incomplete)} incomplete artifact(s) — cannot merge"
            logger.critical(err)
            return _failed(err)

        if self._audio is None:
            return _failed("MergePhase requires AudioPhase")

        if self._audio.result is None:
            if execute:
                self._audio.run()
            else:
                self._audio.scan()

        if not self._audio.result.is_complete:  # type: ignore[union-attr]
            err = "AudioPhase did not complete successfully"
            logger.critical(err)
            return _failed(err)

        return None

    def _recover(self, force_wipe: bool, execute: bool) -> list[MergeArtifact]:
        """Classify merge artifacts and handle force-wipe.

        Steps:
        1. If ``force_wipe`` and execute: delete ``final/`` and ``merge.yaml``.
        2. Detect quality-target / metrics_sampling change — delete per-output
           sidecars so stale COMPLETE artifacts are reclassified as ARTIFACT_ONLY
           and the merge re-runs with fresh metrics.
        3. Clean up leftover ``.tmp`` files (execute mode only).
        4. Determine expected strategies from ``EncodingPhase.result``.
        5. Scan ``final/`` for output + sidecar pairs; classify each.

        Args:
            force_wipe: When ``True``, wipe all merge artifacts first.
            execute:    When ``True``, wipe and ``.tmp`` cleanup are performed.

        Returns:
            List of ``MergeArtifact`` objects.
        """
        work_dir  = self._config.work_dir
        final_dir = work_dir / FINAL_OUTPUT_DIR
        merge_yaml = work_dir / _MERGE_YAML

        # Step 1: force-wipe
        if force_wipe and execute:
            if final_dir.exists():
                shutil.rmtree(final_dir)
                logger.debug("force_wipe: deleted %s", final_dir)
            merge_yaml.unlink(missing_ok=True)
            logger.debug("force_wipe: deleted %s", merge_yaml)

        # Step 2: quality-target / metrics_sampling change detection.
        # When params change, delete all per-output sidecars so every artifact
        # is reclassified as ARTIFACT_ONLY and the merge re-runs with fresh metrics.
        if execute and not force_wipe and final_dir.exists():
            persisted = MergeParams.load(merge_yaml)
            if persisted is not None and persisted != self.params:
                targets_changed  = bool(persisted.quality_targets) and persisted.quality_targets != self.params.quality_targets
                sampling_changed = persisted.metrics_sampling is not None and persisted.metrics_sampling != self.params.metrics_sampling
                if targets_changed or sampling_changed:
                    logger.info(
                        "Merge params changed (%s) — deleting merge sidecars to re-measure quality",
                        "quality targets" if targets_changed else "sampling",
                    )
                    for sidecar in final_dir.glob("*.yaml"):
                        try:
                            sidecar.unlink()
                            logger.debug("Deleted stale merge sidecar: %s", sidecar.name)
                        except OSError as exc:
                            logger.warning("Could not delete merge sidecar %s: %s", sidecar.name, exc)
                    merge_yaml.unlink(missing_ok=True)

        # Step 3: clean up .tmp files (execute mode only)
        if execute and final_dir.exists():
            for tmp in final_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 4: determine expected strategies
        strategies = self._get_expected_strategies()
        if not strategies:
            return []

        source_stem = self._config.source_video.stem

        # Step 5: classify each expected output
        artifacts: list[MergeArtifact] = []
        for strategy_name, safe_name in strategies:
            output_file = final_dir / f"{source_stem} {safe_name}.mkv"
            sidecar     = _load_merge_sidecar(output_file)

            if output_file.exists() and sidecar is not None:
                # COMPLETE — file and sidecar both present
                frame_count = sidecar.get("frame_count")
                metrics     = {k: float(v) for k, v in sidecar.get("metrics", {}).items()}
                targets_met = bool(sidecar.get("targets_met", False))
                plot_path: Path | None = None
                if sidecar.get("plot"):
                    p = Path(sidecar["plot"])
                    if p.exists():
                        plot_path = p

                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.COMPLETE,
                    strategy_name = strategy_name,
                    frame_count   = int(frame_count) if frame_count is not None else None,
                    metrics       = metrics,
                    targets_met   = targets_met,
                    plot_path     = plot_path,
                ))
            elif output_file.exists():
                # ARTIFACT_ONLY — file present but sidecar missing
                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.ARTIFACT_ONLY,
                    strategy_name = strategy_name,
                ))
            else:
                # ABSENT — not yet produced
                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.ABSENT,
                    strategy_name = strategy_name,
                ))

        return artifacts

    def _get_expected_strategies(self) -> list[tuple[str, str]]:
        """Return ``(display_name, safe_name)`` pairs for all expected strategies.

        In pipeline mode reads from ``EncodingPhase.result.encoded`` directly.
        In standalone mode calls ``EncodingPhase.scan()`` first to populate the
        result, then reads from it — this ensures quality-target re-evaluation
        and crop mismatch detection are applied (Req 3.1, 3.2, 6.5).

        Returns:
            List of ``(strategy_name, safe_name)`` tuples.
        """
        self._ensure_encoding_result()

        if self._encoding is None or self._encoding.result is None:
            return []

        encoded = getattr(self._encoding.result, "encoded", [])
        seen: dict[str, str] = {}
        for artifact in encoded:
            if artifact.state == ArtifactState.COMPLETE:
                strategy_name = artifact.strategy
                safe_name     = strategy_name.replace(":", "_")
                seen[strategy_name] = safe_name
        return list(seen.items())

    def _execute_merge(self, artifacts: list[MergeArtifact]) -> "MergePhaseResult":
        """Merge pending strategies by concatenating encoded chunks.

        Args:
            artifacts: Artifact list from ``_recover()``.

        Returns:
            ``MergePhaseResult`` after merging.
        """
        from pyqenc.metrics import MetricKey

        work_dir  = self._config.work_dir
        final_dir = work_dir / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        job_result = self._job.result  # type: ignore[union-attr]
        crop       = getattr(job_result, "crop", None)
        job        = getattr(job_result, "job", None)
        source_video: VideoMetadata | None = getattr(job, "source", None) if job else None
        source_frame_count: int | None = source_video.frame_count if source_video else None
        source_stem = self._config.source_video.stem

        # Build encoded_chunks dict from EncodingPhase result
        encoded_chunks = self._collect_encoded_chunks()

        final_artifacts: list[MergeArtifact] = []
        failed_strategies: list[str] = []

        for artifact in artifacts:
            strategy_name = artifact.strategy_name
            safe_name     = strategy_name.replace(":", "_")

            if artifact.state == ArtifactState.COMPLETE:
                final_artifacts.append(artifact)
                continue

            output_file = final_dir / f"{source_stem} {safe_name}.mkv"
            logger.info("Merging: %s", strategy_name)

            try:
                # Collect and sort chunks for this strategy
                strategy_chunks: list[Path] = sorted(
                    (
                        encoded_chunks[chunk_id][strategy_name]
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
                    self._extraction.result.timestamps_path  # type: ignore[union-attr]
                    if self._extraction is not None and self._extraction.result is not None
                    else None
                )

                if timestamps_path is None or not timestamps_path.exists():
                    logger.critical(
                        "timestamps.txt not found — cannot restore PTS. "
                        "Re-run the extraction phase to generate it."
                    )
                    failed_strategies.append(strategy_name)
                    continue

                # Write mkvmerge options file
                options_file = final_dir / f"concat_{safe_name}.json"
                args = _build_mkvmerge_options(strategy_chunks, output_file, timestamps_path)
                _write_mkvmerge_options_file(options_file, args)

                # Run mkvmerge via options file (avoids OS command-line length limits)
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

                # Write concat list to a temp file (kept for reference / dead code after mkvmerge switch)
                concat_file = final_dir / f"concat_{safe_name}{TEMP_SUFFIX}.txt"
                concat_cmd: list[str | os.PathLike] = [
                    "ffmpeg",
                    "-f",      "concat",
                    "-safe",   "0",
                    "-i",      concat_file,
                    "-c",      "copy",
                    "-fflags", "+genpts",
                    "-y",
                    output_file,
                ]

                # Verify frame count
                frame_count:       int | None = None
                frame_count_ok:    bool       = False
                try:
                    frame_count = get_frame_count(output_file)
                    if source_frame_count is not None:
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

                if source_video and self._config.quality_targets:
                    try:
                        with self._collector.time(MetricKey.MERGE, METRIC_KEY_QUALITY_MEASURE):
                            metrics_dict, targets_met, plot_path = _measure_quality(
                                final_result     = output_file,
                                source_video     = source_video,
                                ref_crop         = crop,
                                quality_targets  = self._config.quality_targets,
                                output_dir       = final_dir,
                                metrics_sampling = self._config.metrics_sampling,
                            )
                    except Exception as exc:
                        logger.warning("  Could not measure quality: %s", exc)

                # CRF distribution plot
                encoded_artifacts = getattr(
                    getattr(self._encoding, "result", None), "encoded", []
                )
                crf_data = _collect_crf_data(encoded_artifacts, strategy_name)
                if crf_data:
                    crf_plot_path = final_dir / f"{output_file.stem}.crf.png"
                    try:
                        qlabel = (
                            self._encoding.quality_labels.get(strategy_name, "CRF")
                            if self._encoding is not None
                            else "CRF"
                        )
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
                    quality_targets = self._config.quality_targets,
                    targets_met     = targets_met,
                    plot_path       = plot_path,
                )

                symbol      = SUCCESS_SYMBOL_MAJOR if targets_met else WARNING_SYMBOL
                frames_sym  = SUCCESS_SYMBOL_MINOR if frame_count_ok else FAILURE_SYMBOL_MINOR
                frames_str  = str(frame_count) if frame_count is not None else "unknown"
                metrics_str = _fmt_inline_metrics(metrics_dict, self._config.quality_targets)
                logger.info(
                    "%s Merged %s:  frames=%s %s%s",
                    symbol, strategy_name, frames_str, frames_sym,
                    f"  {metrics_str}" if metrics_str else "",
                )

                final_artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.COMPLETE,
                    strategy_name = strategy_name,
                    frame_count   = frame_count,
                    metrics       = metrics_dict,
                    targets_met   = targets_met,
                    plot_path     = plot_path,
                ))

            except Exception as exc:
                logger.error("Merging strategy %s error: %s", strategy_name, exc, exc_info=True)
                failed_strategies.append(strategy_name)

        # Phase completion summary
        complete_count = sum(1 for a in final_artifacts if a.state == ArtifactState.COMPLETE)
        logger.info(THICK_LINE)
        logger.info("MERGE SUMMARY")
        logger.info(THICK_LINE)
        if failed_strategies:
            logger.error("  Failed strategies: %s", ", ".join(failed_strategies))
        _log_merge_summary(
            artifacts          = [a for a in final_artifacts if a.state == ArtifactState.COMPLETE],
            source_stem        = source_stem,
            source_size_bytes  = _safe_file_size(source_video.path) if source_video else 0,
            quality_targets    = self._config.quality_targets,
            metrics_sampling   = self._config.metrics_sampling,
        )
        if failed_strategies and not final_artifacts:
            return _failed("All strategy merges failed")

        # Persist merge params (with summary) so quality-target / sampling changes are
        # detected next run and the summary table can be replayed on rerun.
        if complete_count > 0:
            source_size_bytes, strategy_summaries = _build_strategy_summaries(
                final_artifacts,
                source_video.path if source_video else None,
            )
            MergeParams(
                quality_targets    = self.params.quality_targets,
                metrics_sampling   = self.params.metrics_sampling,
                source_stem        = source_stem,
                source_size_bytes  = source_size_bytes,
                strategy_summaries = strategy_summaries,
            ).save(self._config.work_dir / _MERGE_YAML)

        if failed_strategies:
            return MergePhaseResult(
                outcome   = PhaseOutcome.FAILED,
                artifacts = final_artifacts,
                message   = f"{len(failed_strategies)} strategy(ies) failed",
                error     = f"Failed: {', '.join(failed_strategies[:5])}",
                merged    = final_artifacts,
            )

        did_work = any(a.state == ArtifactState.COMPLETE for a in final_artifacts)
        return MergePhaseResult(
            outcome   = PhaseOutcome.COMPLETED if did_work else PhaseOutcome.REUSED,
            artifacts = final_artifacts,
            message   = f"{complete_count} output file(s) complete",
            merged    = final_artifacts,
        )

    def _ensure_encoding_result(self) -> None:
        """Ensure ``EncodingPhase.result`` is populated.

        In pipeline mode the result is already cached from a prior ``run()`` call.
        In standalone mode (no cached result) calls ``self._encoding.scan()`` so
        that quality-target re-evaluation and crop mismatch detection are applied
        before any strategy or chunk lookup (Req 3.1, 3.2, 6.5).
        """
        if self._encoding is not None and self._encoding.result is None:
            self._encoding.scan()

    def _collect_encoded_chunks(self) -> dict[str, dict[str, Path]]:
        """Build ``{chunk_id: {strategy_name: path}}`` from ``EncodingPhase.result``.

        In pipeline mode reads directly from ``EncodingPhase.result.encoded``.
        In standalone mode calls ``EncodingPhase.scan()`` first to populate the
        result — this applies quality-target re-evaluation and crop mismatch
        detection that a raw filesystem glob would miss (Req 3.1, 3.2, 6.5).

        Returns:
            Nested dict mapping chunk IDs to strategy-to-path mappings.
        """
        self._ensure_encoding_result()

        if self._encoding is None or self._encoding.result is None:
            return {}

        encoded = getattr(self._encoding.result, "encoded", [])
        chunks: dict[str, dict[str, Path]] = {}
        for artifact in encoded:
            if artifact.state == ArtifactState.COMPLETE and artifact.path.exists():
                chunk_id      = artifact.chunk_id
                strategy_name = artifact.strategy
                if chunk_id not in chunks:
                    chunks[chunk_id] = {}
                chunks[chunk_id][strategy_name] = artifact.path
        return chunks


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
    ``--timestamps`` is applied to track 0 of the first chunk only.

    Args:
        chunks:          Ordered list of encoded chunk paths.
        output:          Destination output MKV path.
        timestamps_path: Path to the timestamps.txt file.

    Returns:
        List of strings suitable for writing to a JSON options file.
    """
    args: list[str] = [
        "-o",          str(output),
        "--timestamps", f"0:{timestamps_path}",
        str(chunks[0]),
    ]
    for chunk in chunks[1:]:
        args.append(f"+{chunk}")
    return args


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


def _outcome_from_artifacts(
    artifacts: list[MergeArtifact],
    did_work:  bool,
) -> PhaseOutcome:
    """Derive ``PhaseOutcome`` from artifact states."""
    if not artifacts:
        return PhaseOutcome.REUSED
    if any(a.state == ArtifactState.ABSENT for a in artifacts):
        return PhaseOutcome.DRY_RUN
    if all(a.state == ArtifactState.COMPLETE for a in artifacts):
        return PhaseOutcome.REUSED if not did_work else PhaseOutcome.COMPLETED
    return PhaseOutcome.DRY_RUN


def _recovery_message(artifacts: list[MergeArtifact]) -> str:
    """Build a human-readable recovery summary string."""
    complete = sum(1 for a in artifacts if a.state == ArtifactState.COMPLETE)
    pending  = sum(1 for a in artifacts if a.state in (ArtifactState.ABSENT, ArtifactState.ARTIFACT_ONLY))
    if pending == 0:
        return f"{complete} output(s) complete — reusing"
    if complete == 0:
        return f"{pending} output(s) pending — full run needed"
    return f"{complete} output(s) complete, {pending} pending — resuming"


def _failed(error: str) -> MergePhaseResult:
    """Return a ``FAILED`` ``MergePhaseResult`` with the given error message."""
    return MergePhaseResult(
        outcome   = PhaseOutcome.FAILED,
        artifacts = [],
        message   = error,
        error     = error,
        merged    = [],
    )
