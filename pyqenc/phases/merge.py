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
from typing import TYPE_CHECKING, ClassVar

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
from pyqenc.metrics import MetricKey
from pyqenc.models import CropParams, PhaseOutcome, QualityTarget, VideoMetadata
from pyqenc.phase import (
    Artifact,
    ArtifactState,
    Phase,
    PhaseBase,
    PhaseResult,
    Recovery,
)
from pyqenc.phases.audio import AudioPhase
from pyqenc.phases.encoding import EncodingPhase
from pyqenc.phases.extraction import ExtractionPhase
from pyqenc.phases.job import JobPhase
from pyqenc.phases.probe import ProbePhase
from pyqenc.state import MergeParams, MergeStrategySummary, ProbeState
from pyqenc.utils.ffmpeg_runner import get_frame_count
from pyqenc.utils.log_format import (
    fmt_key_value_table,
    fmt_metric_value,
)
from pyqenc.utils.visualization import QualityEvaluator, create_crf_plot
from pyqenc.utils.yaml_utils import write_yaml_atomic

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig
    from pyqenc.metrics import MetricsCollector
    from pyqenc.phases.encoding import EncodedArtifact

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
    artifacts:         list[MergeArtifact],
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
    encoded:     list[EncodedArtifact],
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

class MergePhase(PhaseBase):
    """Phase object for final video merging.

    Owns artifact enumeration, recovery, execution, and logging for the merge
    phase.  Wraps the existing ``merge_final_video`` helper. The uniform run
    footprint is inherited from :class:`PhaseBase`.

    In pipeline mode encoded chunks are read directly from
    ``EncodingPhase.result`` without rescanning the filesystem.  In standalone
    mode the phase scans ``encoded/<strategy.safe_name>/`` for each strategy.

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
        phases:    dict[type[Phase], Phase] | None = None,
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
        probe_result = self._dep(ProbePhase).result
        if probe_result is not None:
            probe = ProbeState(
                frame_count = probe_result.source.frame_count if probe_result.source else 0,
                crop        = probe_result.crop if probe_result.crop else None,
            )

        job_result = self._dep(JobPhase).result
        if job_result is not None:
            return MergeParams(
                quality_targets  = _targets_as_strings(job_result.config.encoding.resolved_targets),
                metrics_sampling = job_result.config.measurement.sampling,
                probe            = probe,
            )
        # Fallback: empty params before job result is available
        return MergeParams(quality_targets=[], metrics_sampling=1)

    # ------------------------------------------------------------------
    # PhaseBase hooks
    # ------------------------------------------------------------------

    def _log_key_params(self) -> None:
        """Log the source stem and quality targets (key parameters)."""
        logger.info("Source stem:  %s", self._dep(JobPhase).result.source.stem)  # type: ignore[union-attr]
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
            a for a in self._dep(EncodingPhase).result.encoded  # type: ignore[union-attr]
            if a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL)
        ]
        if incomplete:
            err = f"EncodingPhase has {len(incomplete)} incomplete artifact(s) — cannot merge"
            logger.critical(err)
            return self._make_result(PhaseOutcome.FAILED, [], err, error=err)

        return None

    def _recover(self) -> Recovery:
        """Classify merge artifacts and handle force-wipe / param invalidation.

        Steps:
        1. If ``force_wipe``: delete ``final/`` and ``merge.yaml``.
        2. Detect quality-target / metrics_sampling change — delete per-output
           sidecars so stale COMPLETE artifacts are reclassified as PARTIAL
           and the merge re-runs with fresh metrics. A probe change deletes
           the whole ``final/`` (outputs are re-merged).
        3. Clean up leftover ``.tmp`` files.
        4. Determine expected strategies from ``EncodingPhase.result``.
        5. Scan ``final/`` for output + sidecar pairs; classify each. Output
           files not matching any expected strategy surface as ``wanted=False``
           artifacts (kept in place; deletion only via explicit cleanup).

        Returns:
            The :class:`Recovery` single source of truth.
        """
        work_dir  = self._dep(JobPhase).result.work_dir  # type: ignore[union-attr]
        final_dir = work_dir / FINAL_OUTPUT_DIR
        merge_yaml = work_dir / _MERGE_YAML
        force_wipe = getattr(self._dep(JobPhase).result, "force_wipe", False)  # type: ignore[union-attr]

        # Step 1: force-wipe
        if force_wipe:
            if final_dir.exists():
                shutil.rmtree(final_dir)
                logger.debug("force_wipe: deleted %s", final_dir)
            merge_yaml.unlink(missing_ok=True)
            logger.debug("force_wipe: deleted %s", merge_yaml)

        # Step 2: quality-target / metrics_sampling change detection.
        # When params change, delete all per-output sidecars so every artifact
        # is reclassified as PARTIAL and the merge re-runs with fresh metrics.
        if not force_wipe and final_dir.exists():
            persisted = MergeParams.load(merge_yaml)
            if persisted is not None and persisted != self.params:
                targets_changed  = bool(persisted.quality_targets) and persisted.quality_targets != self.params.quality_targets
                sampling_changed = persisted.metrics_sampling is not None and persisted.metrics_sampling != self.params.metrics_sampling
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
                    for sidecar in final_dir.glob("*.yaml"):
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
                    if final_dir.exists():
                        shutil.rmtree(final_dir)
                        logger.debug("Probe mismatch: deleted %s", final_dir)
                    merge_yaml.unlink(missing_ok=True)

        # Step 3: clean up .tmp files
        if final_dir.exists():
            for tmp in final_dir.glob(f"*{TEMP_SUFFIX}"):
                try:
                    tmp.unlink()
                    logger.warning("Removed leftover temp file: %s", tmp)
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", tmp, exc)

        # Step 4: determine expected strategies
        strategies = self._get_expected_strategies()
        if not strategies:
            return Recovery()

        source_stem = self._dep(JobPhase).result.source.stem  # type: ignore[union-attr]

        # Step 5: classify each expected output
        artifacts: list[MergeArtifact] = []
        expected_names: set[str] = set()
        for strategy_name, safe_name in strategies:
            output_file = final_dir / f"{source_stem} {safe_name}.mkv"
            expected_names.add(output_file.name)
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
                # PARTIAL — file present but sidecar missing
                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.PARTIAL,
                    strategy_name = strategy_name,
                ))
            else:
                # ABSENT — not yet produced
                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = ArtifactState.ABSENT,
                    strategy_name = strategy_name,
                ))

        # Surface present-but-unwanted surplus outputs (a strategy dropped from
        # the selection whose final file still exists). Retained in place,
        # never pending; deletion only via explicit cleanup.
        if final_dir.exists():
            prefix = f"{source_stem} "
            for output_file in sorted(final_dir.glob("*.mkv")):
                if output_file.name in expected_names:
                    continue
                surplus_strategy = (
                    output_file.name[len(prefix):-len(".mkv")]
                    if output_file.name.startswith(prefix) else output_file.stem
                )
                state = (
                    ArtifactState.COMPLETE
                    if _load_merge_sidecar(output_file) is not None
                    else ArtifactState.PARTIAL
                )
                artifacts.append(MergeArtifact(
                    path          = output_file,
                    state         = state,
                    wanted        = False,
                    strategy_name = surplus_strategy,
                ))
                logger.debug("Final output %s is surplus (strategy no longer selected) — unwanted", output_file.name)

        return Recovery.from_artifacts(artifacts)

    def _reused_result(self, wanted: list[Artifact], message: str) -> MergePhaseResult:
        """Build the reused result, replaying the persisted merge summary."""
        merge_yaml = self._dep(JobPhase).result.work_dir / _MERGE_YAML  # type: ignore[union-attr]
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
        artifacts: list[MergeArtifact],
        message:   str,
        error:     str | None = None,
    ) -> MergePhaseResult:
        """Assemble a ``MergePhaseResult`` from the final-output artifacts.

        Args:
            outcome:   The phase outcome.
            artifacts: The wanted artifact list.
            message:   Human-readable summary.
            error:     Error description when ``outcome`` is ``FAILED``.

        Returns:
            The populated result (``merged`` mirrors ``artifacts``).
        """
        return MergePhaseResult(
            outcome   = outcome,
            artifacts = artifacts,
            message   = message,
            error     = error,
            merged    = artifacts,
        )

    # ------------------------------------------------------------------
    # Public Phase interface
    # ------------------------------------------------------------------

    def _get_expected_strategies(self) -> list[tuple[str, str]]:
        """Return ``(display_name, safe_name)`` pairs for all expected strategies.

        Reads the already-cached ``EncodingPhase.result.encoded`` — the list of
        winning encoding attempts — resolved once by the shared dependency walk.
        Quality-target re-evaluation and crop-mismatch detection are owned by
        ``EncodingPhase._recover()`` and are already reflected in the cached
        artifact states, so this helper only reads them.

        Returns:
            List of ``(strategy_name, safe_name)`` tuples.
        """
        encoding = self._dep(EncodingPhase)
        if encoding.result is None:
            return []

        encoded = getattr(encoding.result, "encoded", [])
        seen: dict[str, str] = {}
        for artifact in encoded:
            if artifact.state == ArtifactState.COMPLETE:
                strategy_name = artifact.strategy
                safe_name     = strategy_name.replace(":", "_")
                seen[strategy_name] = safe_name
        return list(seen.items())

    def _execute(
        self,
        wanted:  list[MergeArtifact],
        dry_run: bool,
    ) -> MergePhaseResult:
        """Merge pending strategies by concatenating encoded chunks.

        The top-level ``merge`` span belongs to the template; the dotted
        ``merge.concat`` / ``merge.quality_measure`` spans are recorded around
        the individual sub-actions below. ``dry_run`` is never ``True`` here
        (merge is not a readonly-execute phase; the template previews
        instead).

        Args:
            wanted:  Wanted artifact list from ``_recover()``.
            dry_run: Unused for this phase (template guarantees ``False``).

        Returns:
            ``MergePhaseResult`` after merging.
        """
        from pyqenc.metrics import MetricKey

        artifacts = wanted
        work_dir  = self._dep(JobPhase).result.work_dir  # type: ignore[union-attr]
        final_dir = work_dir / FINAL_OUTPUT_DIR
        final_dir.mkdir(parents=True, exist_ok=True)

        job_result = self._dep(JobPhase).result  # type: ignore[union-attr]
        probe_result = self._dep(ProbePhase).result
        crop: CropParams | None = probe_result.crop if probe_result is not None else None
        job        = getattr(job_result, "job", None)
        source_video: VideoMetadata | None = getattr(job, "source", None) if job else None
        source_frame_count: int = (
            probe_result.source.frame_count if (probe_result is not None and probe_result.source) else 0
        )
        source_stem = self._dep(JobPhase).result.source.stem  # type: ignore[union-attr]

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

                if source_video and self._dep(JobPhase).result.config.encoding.resolved_targets:  # type: ignore[union-attr]
                    try:
                        with self._collector.time(MetricKey.MERGE, METRIC_KEY_QUALITY_MEASURE):
                            metrics_dict, targets_met, plot_path = _measure_quality(
                                final_result     = output_file,
                                source_video     = source_video,
                                ref_crop         = crop,
                                quality_targets  = self._dep(JobPhase).result.config.encoding.resolved_targets,  # type: ignore[union-attr]
                                output_dir       = final_dir,
                                metrics_sampling = self._dep(JobPhase).result.config.measurement.sampling,  # type: ignore[union-attr]
                            )
                    except Exception as exc:
                        logger.warning("  Could not measure quality: %s", exc)

                # CRF distribution plot
                encoded_artifacts = getattr(
                    self._dep(EncodingPhase).result, "encoded", []
                )
                crf_data = _collect_crf_data(encoded_artifacts, strategy_name)
                if crf_data:
                    crf_plot_path = final_dir / f"{output_file.stem}.crf.png"
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
                    quality_targets = self._dep(JobPhase).result.config.encoding.resolved_targets,  # type: ignore[union-attr]
                    targets_met     = targets_met,
                    plot_path       = plot_path,
                )

                symbol      = SUCCESS_SYMBOL_MAJOR if targets_met else WARNING_SYMBOL
                frames_sym  = SUCCESS_SYMBOL_MINOR if frame_count_ok else FAILURE_SYMBOL_MINOR
                frames_str  = str(frame_count) if frame_count is not None else "unknown"
                metrics_str = _fmt_inline_metrics(metrics_dict, self._dep(JobPhase).result.config.encoding.resolved_targets)  # type: ignore[union-attr]
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
            quality_targets    = self._dep(JobPhase).result.config.encoding.resolved_targets,  # type: ignore[union-attr]
            metrics_sampling   = self._dep(JobPhase).result.config.measurement.sampling,  # type: ignore[union-attr]
        )
        if failed_strategies and not final_artifacts:
            return self._make_result(PhaseOutcome.FAILED, [], "All strategy merges failed", error="All strategy merges failed")

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
                probe              = self.params.probe,
                source_stem        = source_stem,
                source_size_bytes  = source_size_bytes,
                strategy_summaries = strategy_summaries,
            ).save(self._dep(JobPhase).result.work_dir / _MERGE_YAML)  # type: ignore[union-attr]

        if failed_strategies:
            return self._make_result(
                PhaseOutcome.FAILED, final_artifacts,
                f"{len(failed_strategies)} strategy(ies) failed",
                error=f"Failed: {', '.join(failed_strategies[:5])}",
            )

        did_work = any(a.state == ArtifactState.COMPLETE for a in final_artifacts)
        return self._make_result(
            PhaseOutcome.COMPLETED if did_work else PhaseOutcome.REUSED,
            final_artifacts,
            f"{complete_count} output file(s) complete",
        )

    def _collect_encoded_chunks(self) -> dict[str, dict[str, Path]]:
        """Build ``{chunk_id: {strategy_name: path}}`` from ``EncodingPhase.result``.

        Reads the already-cached ``EncodingPhase.result.encoded`` — the list of
        winning encoding attempts — resolved once by the shared dependency walk.
        Quality-target re-evaluation and crop-mismatch detection are owned by
        ``EncodingPhase._recover()`` and are already reflected in the cached
        artifact states, so this helper only reads them.

        Returns:
            Nested dict mapping chunk IDs to strategy-to-path mappings.
        """
        encoding = self._dep(EncodingPhase)
        if encoding.result is None:
            return {}

        encoded = getattr(encoding.result, "encoded", [])
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
    """Derive ``PhaseOutcome`` purely from artifact states (mode-free).

    Any ``ABSENT`` or ``PARTIAL`` artifact means wanted work remains, so the
    phase is ``PENDING`` regardless of run mode; the runner owns the dry-run
    vs execute distinction. When every artifact is ``COMPLETE`` the phase is
    ``COMPLETED`` (did work) or ``REUSED`` (nothing to do). With no artifacts
    there is nothing to produce, so the phase is ``REUSED``.
    """
    if not artifacts:
        return PhaseOutcome.REUSED
    if any(a.state in (ArtifactState.ABSENT, ArtifactState.PARTIAL) for a in artifacts):
        return PhaseOutcome.PENDING
    if all(a.state == ArtifactState.COMPLETE for a in artifacts):
        return PhaseOutcome.REUSED if not did_work else PhaseOutcome.COMPLETED
    return PhaseOutcome.PENDING



