"""
Merging phase for the quality-based encoding pipeline.

This module handles concatenation of encoded video chunks to produce final
MKV output files.  It also measures final quality metrics and generates
visual plots for verification.

Audio muxing is intentionally omitted — the final output is video-only.
Audio delivery files are kept alongside the output for the user to mux
manually or in a downstream step.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from pyqenc.constants import (
    FAILURE_SYMBOL_MINOR,
    SUCCESS_SYMBOL_MINOR,
    TEMP_SUFFIX,
)
from pyqenc.models import CropParams, PhaseOutcome, QualityTarget, VideoMetadata
from pyqenc.utils.ffmpeg_runner import get_frame_count, run_ffmpeg
from pyqenc.utils.visualization import QualityEvaluator
from pyqenc.utils.yaml_utils import write_yaml_atomic

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sidecar model
# ---------------------------------------------------------------------------

_MERGE_SIDECAR_VERSION = 1


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


def _write_merge_sidecar(
    output_file:     Path,
    frame_count:     int | None,
    final_metrics:   dict[str, float],
    targets_met:     bool,
    plot_path:       Path | None,
) -> None:
    """Atomically write a merge sidecar alongside *output_file*."""
    data: dict = {
        "version":     _MERGE_SIDECAR_VERSION,
        "frame_count": frame_count,
        "targets_met": targets_met,
        "metrics":     final_metrics,
    }
    if plot_path is not None:
        data["plot"] = str(plot_path)
    try:
        write_yaml_atomic(_sidecar_path(output_file), data)
    except Exception as exc:
        logger.warning("Could not write merge sidecar for %s: %s", output_file.name, exc)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    """Result of merging phase.

    Attributes:
        output_files:   Dictionary mapping strategy names to output file paths.
        frame_counts:   Dictionary mapping strategy names to frame counts.
        final_metrics:  Dictionary mapping strategy names to quality metrics.
        targets_met:    Dictionary mapping strategy names to whether targets were met.
        metrics_plots:  Dictionary mapping strategy names to plot file paths.
        outcome:        Phase outcome.
        error:          Error message if merging failed.
    """

    output_files:  dict[str, Path]
    frame_counts:  dict[str, int]
    final_metrics: dict[str, dict[str, float]]
    targets_met:   dict[str, bool]
    metrics_plots: dict[str, Path]
    outcome:       PhaseOutcome
    error:         str | None = None

    @property
    def needs_work(self) -> bool:
        """True when the phase is in dry-run mode and work would be required."""
        return self.outcome == PhaseOutcome.DRY_RUN


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strategy_display_name(strategy: str) -> str:
    """Return a human-readable strategy name (preserves ``+`` and ``:`` separators)."""
    return strategy.replace("_", "+", 1)  # only first underscore — profile may contain underscores


def _strategy_safe_name(strategy: str) -> str:
    """Return a filesystem-safe strategy name."""
    return strategy.replace("+", "_").replace(":", "_")


def _cleanup_tmp_files(directory: Path) -> None:
    """Remove leftover ``.tmp`` files from a previous interrupted run."""
    if not directory.exists():
        return
    for tmp_file in directory.glob(f"*{TEMP_SUFFIX}"):
        try:
            tmp_file.unlink()
            logger.warning("Removed leftover temp file: %s", tmp_file.name)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", tmp_file.name, exc)


def _measure_quality(
    output_file:     Path,
    source_video:    VideoMetadata,
    ref_crop:        CropParams | None,
    quality_targets: list[QualityTarget],
    output_dir:      Path,
    safe_strategy:   str,
    subsample_factor: int,
) -> tuple[dict[str, float], bool, Path | None]:
    """Measure final quality metrics for *output_file* against *source_video*.

    Returns:
        Tuple of ``(metrics_dict, targets_met, plot_path)``.
    """
    evaluator   = QualityEvaluator(output_dir)
    metrics_dir = output_dir / f"metrics_{safe_strategy}"

    evaluation = evaluator.evaluate_chunk(
        encoded=output_file,
        reference=source_video.path,
        ref_crop=ref_crop,
        targets=quality_targets,
        output_dir=metrics_dir,
        subsample_factor=subsample_factor,
        show_progress=True,
    )

    metrics_dict: dict[str, float] = {}
    for metric_name, metric_stats in evaluation.metrics.items():
        for stat_name, stat_value in metric_stats.items():
            metrics_dict[f"{metric_name}_{stat_name}"] = stat_value

    plot_path = evaluation.artifacts.plot if evaluation.artifacts.plot else None
    return metrics_dict, evaluation.targets_met, plot_path


def _log_metrics_summary(
    strategy:        str,
    metrics_dict:    dict[str, float],
    quality_targets: list[QualityTarget],
    targets_met:     bool,
) -> None:
    """Log a compact quality metrics summary for *strategy*."""
    for target in quality_targets:
        key    = f"{target.metric}_{target.statistic}"
        value  = metrics_dict.get(key)
        if value is None:
            continue
        symbol = SUCCESS_SYMBOL_MINOR if value >= target.value else FAILURE_SYMBOL_MINOR
        logger.info(
            "  %s %s-%s: %.2f (target: %.2f)",
            symbol, target.metric, target.statistic, value, target.value,
        )
    overall = SUCCESS_SYMBOL_MINOR if targets_met else FAILURE_SYMBOL_MINOR
    logger.info("  %s %s: quality targets %s", overall, strategy, "met" if targets_met else "NOT met")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def merge_final_video(
    encoded_chunks:     dict[str, dict[str, Path]],
    output_dir:         Path,
    source_video:       VideoMetadata | None = None,
    ref_crop:           CropParams | None = None,
    quality_targets:    list[QualityTarget] | None = None,
    source_frame_count: int | None = None,
    optimal_strategy:   str | None = None,
    subsample_factor:   int = 10,
    verify_frames:      bool = True,
    measure_quality:    bool = True,
    force:              bool = False,
    dry_run:            bool = False,
) -> MergeResult:
    """Merge encoded chunks into final MKV files (video-only concatenation).

    Produces one output file per encoding strategy.  Uses the ffmpeg concat
    demuxer for frame-perfect video concatenation.  Audio muxing is omitted —
    audio delivery files are kept alongside the output for downstream use.

    After merging, measures final quality metrics by comparing the complete
    merged video against the original source, and generates visual quality
    plots for verification.  Results are persisted in a per-output sidecar
    YAML so metrics are not re-measured on subsequent runs.

    Args:
        encoded_chunks:     Nested dict ``{chunk_id: {strategy: path}}`` with encoded chunks.
        output_dir:         Directory for final output files.
        source_video:       Original source ``VideoMetadata`` for quality measurement (optional).
        ref_crop:           Crop parameters applied during encoding; used to align the reference
                            for quality measurement (optional).
        quality_targets:    Quality targets to verify against (optional).
        source_frame_count: Expected frame count for verification (optional).
        optimal_strategy:   When set, merge only this strategy; otherwise merge all.
        subsample_factor:   Frame subsampling factor for final quality metrics.
        verify_frames:      Whether to verify frame count matches source.
        measure_quality:    Whether to measure final video quality metrics.
        force:              If False, reuse existing output files.
        dry_run:            If True, only report status without performing merge.

    Returns:
        MergeResult with paths to final output files, metrics, and plots.
    """
    try:
        # ------------------------------------------------------------------
        # Determine which strategies we have
        # ------------------------------------------------------------------
        all_strategies: set[str] = set()
        for chunk_strategies in encoded_chunks.values():
            all_strategies.update(chunk_strategies.keys())

        if not all_strategies:
            logger.error("No encoded chunks found for merging")
            return MergeResult(
                output_files={}, frame_counts={}, final_metrics={},
                targets_met={}, metrics_plots={},
                outcome=PhaseOutcome.FAILED, error="No encoded chunks found",
            )

        total_chunks = len(encoded_chunks)

        # Strategies that have ALL chunks encoded
        complete_strategies: set[str] = {
            s for s in all_strategies
            if sum(1 for cs in encoded_chunks.values() if s in cs) == total_chunks
        }

        # ------------------------------------------------------------------
        # Optimal-strategy mode: resolve and restrict to one strategy
        # ------------------------------------------------------------------
        if optimal_strategy:
            # Normalise both sides for comparison (stored name may use '+' or '_')
            normalised_optimal = optimal_strategy.replace("+", "_").replace(":", "_")
            normalised_map: dict[str, str] = {
                s.replace("+", "_").replace(":", "_"): s for s in complete_strategies
            }

            if normalised_optimal not in normalised_map:
                # Also check all_strategies in case it's incomplete
                normalised_all: dict[str, str] = {
                    s.replace("+", "_").replace(":", "_"): s for s in all_strategies
                }
                if normalised_optimal in normalised_all:
                    logger.error(
                        "Optimal strategy '%s' is incomplete — not all %d chunks encoded",
                        optimal_strategy, total_chunks,
                    )
                else:
                    logger.error(
                        "Optimal strategy '%s' not found in encoded artifacts",
                        optimal_strategy,
                    )
                return MergeResult(
                    output_files={}, frame_counts={}, final_metrics={},
                    targets_met={}, metrics_plots={},
                    outcome=PhaseOutcome.FAILED,
                    error=f"Optimal strategy '{optimal_strategy}' not found or incomplete",
                )

            resolved = normalised_map[normalised_optimal]
            strategies: set[str] = {resolved}
            # Log the human-readable name as the very first message
            logger.info("Merging optimal strategy: %s", _strategy_display_name(resolved))

        else:
            # All-strategies mode: warn about incomplete ones
            incomplete_strategies = all_strategies - complete_strategies
            if incomplete_strategies:
                logger.warning(
                    "Skipping %d incomplete strategies (not all %d chunks encoded): %s",
                    len(incomplete_strategies), total_chunks, sorted(incomplete_strategies),
                )

            if not complete_strategies:
                logger.error("No strategy has all %d chunks encoded — cannot merge", total_chunks)
                return MergeResult(
                    output_files={}, frame_counts={}, final_metrics={},
                    targets_met={}, metrics_plots={},
                    outcome=PhaseOutcome.FAILED,
                    error=f"No strategy has all {total_chunks} chunks encoded",
                )

            strategies = complete_strategies
            logger.info(
                "Merging %d strategy(ies): %s",
                len(strategies), ", ".join(sorted(strategies)),
            )

        # ------------------------------------------------------------------
        # Ensure output directory exists and clean up leftover .tmp files
        # ------------------------------------------------------------------
        output_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_tmp_files(output_dir)

        # ------------------------------------------------------------------
        # Dry-run mode
        # ------------------------------------------------------------------
        if dry_run:
            for strategy in sorted(strategies):
                safe = _strategy_safe_name(strategy)
                output_file = output_dir / f"output_{safe}.mkv"
                if not output_file.exists():
                    logger.info("[DRY-RUN] Would merge: %s", _strategy_display_name(strategy))
            return MergeResult(
                output_files={}, frame_counts={}, final_metrics={},
                targets_met={}, metrics_plots={},
                outcome=PhaseOutcome.DRY_RUN,
            )

        # ------------------------------------------------------------------
        # Check for fully complete existing outputs (file + sidecar)
        # ------------------------------------------------------------------
        if not force:
            existing_outputs: dict[str, Path] = {}
            all_complete = True

            for strategy in strategies:
                safe        = _strategy_safe_name(strategy)
                output_file = output_dir / f"output_{safe}.mkv"
                sidecar     = _load_merge_sidecar(output_file)
                if output_file.exists() and sidecar is not None:
                    existing_outputs[strategy] = output_file
                else:
                    all_complete = False
                    break

            if all_complete and existing_outputs:
                frame_counts:    dict[str, int]              = {}
                final_metrics:   dict[str, dict[str, float]] = {}
                targets_met_map: dict[str, bool]             = {}
                metrics_plots:   dict[str, Path]             = {}

                for strategy, output_file in existing_outputs.items():
                    sidecar = _load_merge_sidecar(output_file)
                    if sidecar:
                        if sidecar.get("frame_count") is not None:
                            frame_counts[strategy] = int(sidecar["frame_count"])
                        if sidecar.get("metrics"):
                            final_metrics[strategy] = {
                                k: float(v) for k, v in sidecar["metrics"].items()
                            }
                        targets_met_map[strategy] = bool(sidecar.get("targets_met", False))
                        if sidecar.get("plot"):
                            plot = Path(sidecar["plot"])
                            if plot.exists():
                                metrics_plots[strategy] = plot

                logger.info(
                    "Reusing %d existing output(s) — all complete",
                    len(existing_outputs),
                )
                return MergeResult(
                    output_files=existing_outputs,
                    frame_counts=frame_counts,
                    final_metrics=final_metrics,
                    targets_met=targets_met_map,
                    metrics_plots=metrics_plots,
                    outcome=PhaseOutcome.REUSED,
                )

        # ------------------------------------------------------------------
        # Process each strategy
        # ------------------------------------------------------------------
        output_files:    dict[str, Path]              = {}
        frame_counts     = {}
        final_metrics    = {}
        targets_met_map  = {}
        metrics_plots    = {}

        for strategy in sorted(strategies):
            safe = _strategy_safe_name(strategy)
            logger.info("Merging: %s", _strategy_display_name(strategy))

            try:
                # Collect and sort chunks for this strategy
                strategy_chunks: list[Path] = sorted(
                    (
                        encoded_chunks[chunk_id][strategy]
                        for chunk_id in sorted(encoded_chunks.keys())
                        if strategy in encoded_chunks[chunk_id]
                    ),
                    key=lambda p: p.name,
                )

                if not strategy_chunks:
                    logger.warning("No chunks found for strategy %s — skipping", strategy)
                    continue

                logger.info("  %d chunks to concatenate", len(strategy_chunks))

                # Write concat list to a temp file in the output dir
                concat_file = output_dir / f"concat_{safe}{TEMP_SUFFIX}.txt"
                with concat_file.open("w", encoding="utf-8") as fh:
                    for chunk_path in strategy_chunks:
                        abs_path = chunk_path.resolve()
                        escaped  = str(abs_path).replace("'", "'\\''")
                        fh.write(f"file '{escaped}'\n")

                output_file = output_dir / f"output_{safe}.mkv"

                concat_cmd: list[str | os.PathLike] = [
                    "ffmpeg",
                    "-f",    "concat",
                    "-safe", "0",
                    "-i",    concat_file,
                    "-c",    "copy",
                    "-y",
                    output_file,
                ]

                logger.debug("Concat command: %s", " ".join(str(a) for a in concat_cmd))
                concat_result = run_ffmpeg(concat_cmd, output_file=output_file)

                # Clean up concat list regardless of outcome
                concat_file.unlink(missing_ok=True)

                if not concat_result.success:
                    logger.error("Concatenation failed for strategy %s", strategy)
                    continue

                logger.info("  Concatenation complete: %s", output_file.name)

                # Verify frame count
                frame_count: int | None = None
                if verify_frames:
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
                                logger.info(
                                    "  Frame count verified: %d %s",
                                    frame_count, SUCCESS_SYMBOL_MINOR,
                                )
                        frame_counts[strategy] = frame_count
                    except Exception as exc:
                        logger.warning("  Could not verify frame count: %s", exc)

                # Measure quality (skip if sidecar already present and not forced)
                metrics_dict: dict[str, float] = {}
                targets_met:  bool             = False
                plot_path:    Path | None       = None

                if measure_quality and source_video and quality_targets:
                    existing_sidecar = _load_merge_sidecar(output_file)
                    if not force and existing_sidecar and existing_sidecar.get("metrics"):
                        logger.info("  Quality metrics: reusing from sidecar")
                        metrics_dict = {
                            k: float(v) for k, v in existing_sidecar["metrics"].items()
                        }
                        targets_met = bool(existing_sidecar.get("targets_met", False))
                        if existing_sidecar.get("plot"):
                            p = Path(existing_sidecar["plot"])
                            if p.exists():
                                plot_path = p
                    else:
                        logger.info("  Measuring final quality metrics...")
                        try:
                            metrics_dict, targets_met, plot_path = _measure_quality(
                                output_file=output_file,
                                source_video=source_video,
                                ref_crop=ref_crop,
                                quality_targets=quality_targets,
                                output_dir=output_dir,
                                safe_strategy=safe,
                                subsample_factor=subsample_factor,
                            )
                        except Exception as exc:
                            logger.warning("  Could not measure quality: %s", exc)

                    if metrics_dict:
                        _log_metrics_summary(
                            _strategy_display_name(strategy),
                            metrics_dict, quality_targets, targets_met,
                        )
                        final_metrics[strategy]   = metrics_dict
                        targets_met_map[strategy] = targets_met
                        if plot_path:
                            metrics_plots[strategy] = plot_path

                # Write sidecar (marks this output as COMPLETE)
                _write_merge_sidecar(
                    output_file=output_file,
                    frame_count=frame_count,
                    final_metrics=metrics_dict,
                    targets_met=targets_met,
                    plot_path=plot_path,
                )

                output_files[strategy] = output_file

            except Exception as exc:
                logger.error("Merging strategy %s error: %s", strategy, exc, exc_info=True)
                continue

        if not output_files:
            logger.error("No strategies were successfully merged")
            return MergeResult(
                output_files={}, frame_counts={}, final_metrics={},
                targets_met={}, metrics_plots={},
                outcome=PhaseOutcome.FAILED, error="All strategy merges failed",
            )

        logger.info(
            "Merge complete: %d output file(s)",
            len(output_files),
        )

        return MergeResult(
            output_files=output_files,
            frame_counts=frame_counts,
            final_metrics=final_metrics,
            targets_met=targets_met_map,
            metrics_plots=metrics_plots,
            outcome=PhaseOutcome.COMPLETED,
        )

    except Exception as exc:
        logger.critical("Merging phase failed: %s", exc, exc_info=True)
        return MergeResult(
            output_files={}, frame_counts={}, final_metrics={},
            targets_met={}, metrics_plots={},
            outcome=PhaseOutcome.FAILED, error=str(exc),
        )
