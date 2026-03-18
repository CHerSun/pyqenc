"""
Merging phase for the quality-based encoding pipeline.

This module handles concatenation of encoded video chunks and muxing with
processed audio streams to produce final MKV output files. It also measures
final quality metrics and generates visual plots for verification.
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pyqenc.constants import FAILURE_SYMBOL_MINOR, SUCCESS_SYMBOL_MINOR
from pyqenc.models import CropParams, PhaseOutcome, QualityTarget, VideoMetadata
from pyqenc.utils.ffmpeg_runner import get_frame_count, run_ffmpeg
from pyqenc.utils.visualization import QualityEvaluator

logger = logging.getLogger(__name__)


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


def merge_final_video(
    encoded_chunks:     dict[str, dict[str, Path]],
    audio_files:        list[Path],
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
    """Merge encoded chunks and audio into final MKV files.

    Produces separate output files for each encoding strategy.
    Uses ffmpeg concat demuxer for frame-perfect video concatenation,
    then mkvmerge to combine video and audio streams.

    After merging, measures final quality metrics by comparing the complete
    merged video against the original source, and generates visual quality
    plots for verification.

    Args:
        encoded_chunks:     Nested dict ``{chunk_id: {strategy: path}}`` with encoded chunks.
        audio_files:        List of processed audio files to include.
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

    Requirements:
        5.1, 5.2, 5.3, 5.4, 5.5, 6.1, 6.2, 6.3, 6.4, 6.5
    """
    try:
        logger.info("Merging phase starting")

        # Ensure output directory exists
        output_dir.mkdir(parents=True, exist_ok=True)

        # Determine which strategies we have
        all_strategies: set[str] = set()
        for chunk_strategies in encoded_chunks.values():
            all_strategies.update(chunk_strategies.keys())

        if not all_strategies:
            logger.error("No encoded chunks found for merging")
            return MergeResult(
                output_files={},
                frame_counts={},
                final_metrics={},
                targets_met={},
                metrics_plots={},
                outcome=PhaseOutcome.FAILED,
                error="No encoded chunks found"
            )

        # Respect optimal_strategy: when set, merge only that strategy
        if optimal_strategy:
            if optimal_strategy in all_strategies:
                strategies = {optimal_strategy}
                logger.info("Merging optimal strategy only: %s", optimal_strategy)
            else:
                logger.warning(
                    "Optimal strategy '%s' not found in encoded chunks; "
                    "available: %s — merging all strategies instead.",
                    optimal_strategy, sorted(all_strategies),
                )
                strategies = all_strategies
        else:
            strategies = all_strategies

        logger.info("Found %d strategies to merge: %s", len(strategies), ", ".join(sorted(strategies)))

        # Check for existing output files
        if not force:
            existing_outputs = {}
            all_exist = True

            for strategy in strategies:
                # Make strategy name filesystem-safe
                safe_strategy = strategy.replace("+", "_").replace(":", "_")
                output_file = output_dir / f"output_{safe_strategy}.mkv"

                if output_file.exists():
                    existing_outputs[strategy] = output_file
                    logger.debug(f"Found existing output for {strategy}: {output_file.name}")
                else:
                    all_exist = False
                    break

            if all_exist:
                logger.info(f"Reusing existing output files: {len(existing_outputs)} files")

                # Get frame counts if verification requested
                frame_counts = {}
                if verify_frames:
                    for strategy, output_file in existing_outputs.items():
                        try:
                            frame_counts[strategy] = get_frame_count(output_file)
                        except Exception as e:
                            logger.warning(f"Could not verify frame count for {strategy}: {e}")

                # Measure quality if requested and source provided
                final_metrics = {}
                targets_met_dict = {}
                metrics_plots = {}

                if measure_quality and source_video and quality_targets:
                    logger.info("Measuring final quality metrics for existing outputs...")
                    evaluator = QualityEvaluator(output_dir)

                    for strategy, output_file in existing_outputs.items():
                        try:
                            safe_strategy = strategy.replace("+", "_").replace(":", "_")
                            metrics_dir = output_dir / f"final_metrics_{safe_strategy}"
                            evaluation = evaluator.evaluate_chunk(
                                encoded=output_file,
                                reference=source_video.path,
                                ref_crop=ref_crop,
                                targets=quality_targets,
                                output_dir=metrics_dir,
                                subsample_factor=subsample_factor,
                                show_progress=True,
                            )

                            # Extract metrics as flat dict
                            metrics_dict = {}
                            for metric_name, metric_stats in evaluation.metrics.items():
                                for stat_name, stat_value in metric_stats.items():
                                    metrics_dict[f"{metric_name}_{stat_name}"] = stat_value

                            final_metrics[strategy] = metrics_dict
                            targets_met_dict[strategy] = evaluation.targets_met

                            if evaluation.artifacts.plot:
                                metrics_plots[strategy] = evaluation.artifacts.plot
                                logger.info(f"  {strategy}: Quality plot saved to {evaluation.artifacts.plot.name}")

                            # Log metrics summary
                            logger.info(f"  {strategy}: Final quality metrics:")
                            for target in quality_targets:
                                key = f"{target.metric}_{target.statistic}"
                                if key in metrics_dict:
                                    status: str = SUCCESS_SYMBOL_MINOR if metrics_dict[key] >= target.value \
                                                    else FAILURE_SYMBOL_MINOR
                                    logger.info(
                                        f"    {status} {target.metric}-{target.statistic}: "
                                        f"{metrics_dict[key]:.2f} (target: {target.value})"
                                    )

                        except Exception as e:
                            logger.warning(f"Could not measure quality for {strategy}: {e}")

                if dry_run:
                    logger.info("[DRY-RUN] Merging: Complete (reusing existing files)")
                    return MergeResult(
                        output_files=existing_outputs,
                        frame_counts=frame_counts,
                        final_metrics=final_metrics,
                        targets_met=targets_met_dict,
                        metrics_plots=metrics_plots,
                        outcome=PhaseOutcome.REUSED,
                    )

                return MergeResult(
                    output_files=existing_outputs,
                    frame_counts=frame_counts,
                    final_metrics=final_metrics,
                    targets_met=targets_met_dict,
                    metrics_plots=metrics_plots,
                    outcome=PhaseOutcome.REUSED,
                )

        # Dry-run mode: report what would be done
        if dry_run:
            logger.info(f"[DRY-RUN] Would merge {len(strategies)} strategies")
            for strategy in sorted(strategies):
                chunk_count = sum(1 for cs in encoded_chunks.values() if strategy in cs)
                logger.info(f"[DRY-RUN]   {strategy}: {chunk_count} chunks")
            logger.info(f"[DRY-RUN]   Audio files: {len(audio_files)}")
            if measure_quality and source_video:
                logger.info(f"[DRY-RUN]   Would measure final quality against source")
            logger.info("[DRY-RUN] Merging: Needs work")
            return MergeResult(
                output_files={},
                frame_counts={},
                final_metrics={},
                targets_met={},
                metrics_plots={},
                outcome=PhaseOutcome.DRY_RUN,
            )

        # Process each strategy
        output_files = {}
        frame_counts = {}
        final_metrics = {}
        targets_met_dict = {}
        metrics_plots = {}

        for strategy in sorted(strategies):
            logger.info(f"Merging strategy: {strategy}")

            try:
                # Collect chunks for this strategy sorted by filename (encodes start timestamp)
                strategy_chunks: list[Path] = []
                for chunk_id in sorted(encoded_chunks.keys()):
                    if strategy in encoded_chunks[chunk_id]:
                        strategy_chunks.append(encoded_chunks[chunk_id][strategy])

                # Sort by filename for correct temporal order (Req 5.3)
                strategy_chunks.sort(key=lambda p: p.name)

                if not strategy_chunks:
                    logger.warning(f"No chunks found for strategy {strategy}, skipping")
                    continue

                # Log chunk count and total frame count before merge (Req 5.5)
                total_chunk_frames: int | None = None
                if verify_frames:
                    try:
                        total_chunk_frames = sum(get_frame_count(c) for c in strategy_chunks)
                    except Exception as e:
                        logger.warning("Could not count frames for strategy %s: %s", strategy, e)

                if total_chunk_frames is not None:
                    logger.info(
                        "  Merging %d chunks, %d total frames",
                        len(strategy_chunks), total_chunk_frames,
                    )
                else:
                    logger.info("  Merging %d chunks", len(strategy_chunks))

                # Create concat file for ffmpeg
                concat_file = output_dir / f"concat_{strategy.replace('+', '_')}.txt"
                with open(concat_file, 'w') as f:
                    for chunk_path in strategy_chunks:
                        # Use absolute paths and escape single quotes
                        abs_path = chunk_path.resolve()
                        f.write(f"file '{abs_path}'\n")

                # Concatenate video chunks using ffmpeg concat demuxer
                safe_strategy = strategy.replace("+", "_").replace(":", "_")
                concatenated_video = output_dir / f"concatenated_{safe_strategy}.mkv"

                concat_cmd = [
                    "ffmpeg",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-c", "copy",  # Stream copy for lossless concatenation
                    "-y",
                    concatenated_video,
                ]

                logger.debug("Running: %s", " ".join(str(a) for a in concat_cmd))
                concat_result = run_ffmpeg(concat_cmd, output_file=concatenated_video)
                if not concat_result.success:
                    logger.error("Video concatenation failed for strategy %s", strategy)
                    continue

                logger.info(f"  Video concatenation complete: {concatenated_video.name}")

                # Verify frame count if requested (Req 5.4 — warn and continue, do not abort)
                if verify_frames:
                    video_frames = get_frame_count(concatenated_video)
                    logger.info(f"  Concatenated video frame count: {video_frames}")

                    if source_frame_count is not None:
                        if video_frames != source_frame_count:
                            logger.warning(
                                "  Frame count mismatch: expected %d, got %d (difference: %d)",
                                source_frame_count, video_frames,
                                abs(video_frames - source_frame_count),
                            )
                        else:
                            logger.info("  Frame count verification passed: %d frames", video_frames)

                    frame_counts[strategy] = video_frames

                # Mux video and audio using mkvmerge
                output_file = output_dir / f"output_{safe_strategy}.mkv"

                mkvmerge_cmd = [
                    "mkvmerge",
                    "-o", str(output_file),
                    str(concatenated_video),  # Video track
                ]

                # Add all audio tracks
                for audio_file in audio_files:
                    mkvmerge_cmd.append(str(audio_file))

                logger.debug(f"Running: {' '.join(mkvmerge_cmd)}")
                result = subprocess.run(
                    mkvmerge_cmd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=300  # 5 minute timeout
                )

                logger.info(f"  Muxing complete: {output_file.name}")

                # Measure final quality if requested (Req 6.1–6.5)
                if measure_quality and source_video and quality_targets:
                    logger.info(f"  Measuring final quality metrics...")

                    try:
                        evaluator = QualityEvaluator(output_dir)
                        metrics_dir = output_dir / f"final_metrics_{safe_strategy}"

                        evaluation = evaluator.evaluate_chunk(
                            encoded=output_file,
                            reference=source_video.path,
                            ref_crop=ref_crop,
                            targets=quality_targets,
                            output_dir=metrics_dir,
                            subsample_factor=subsample_factor,
                            show_progress=True,
                        )

                        # Extract metrics as flat dict
                        metrics_dict = {}
                        for metric_name, metric_stats in evaluation.metrics.items():
                            for stat_name, stat_value in metric_stats.items():
                                metrics_dict[f"{metric_name}_{stat_name}"] = stat_value

                        final_metrics[strategy] = metrics_dict
                        targets_met_dict[strategy] = evaluation.targets_met

                        if evaluation.artifacts.plot:
                            metrics_plots[strategy] = evaluation.artifacts.plot
                            logger.info(f"  Quality plot saved to {evaluation.artifacts.plot.name}")

                        # Log metrics summary
                        logger.info("  Final quality metrics:")
                        for target in quality_targets:
                            key = f"{target.metric}_{target.statistic}"
                            if key in metrics_dict:
                                status: str = SUCCESS_SYMBOL_MINOR if metrics_dict[key] >= target.value else FAILURE_SYMBOL_MINOR
                                logger.info(
                                    f"    {status} {target.metric}-{target.statistic}: "
                                    f"{metrics_dict[key]:.2f} (target: {target.value})"
                                )

                        if evaluation.targets_met:
                            logger.info(f"  {SUCCESS_SYMBOL_MINOR} All quality targets met for {strategy}")
                        else:
                            logger.warning(f"  {FAILURE_SYMBOL_MINOR} Some quality targets not met for {strategy}")

                    except Exception as e:
                        logger.warning(f"  Could not measure final quality: {e}")

                # Clean up intermediate files
                concat_file.unlink(missing_ok=True)
                concatenated_video.unlink(missing_ok=True)

                output_files[strategy] = output_file

            except subprocess.TimeoutExpired as e:
                logger.error(f"Merging strategy {strategy} timed out: {e}")
                continue
            except subprocess.CalledProcessError as e:
                logger.error(f"Merging strategy {strategy} failed: {e.stderr}")
                continue
            except Exception as e:
                logger.error(f"Merging strategy {strategy} error: {e}", exc_info=True)
                continue

        if not output_files:
            logger.error("No strategies were successfully merged")
            return MergeResult(
                output_files={},
                frame_counts={},
                final_metrics={},
                targets_met={},
                metrics_plots={},
                outcome=PhaseOutcome.FAILED,
                error="All strategy merges failed"
            )

        logger.info(f"Merging complete: {len(output_files)} output files created")

        # Report final metrics summary
        if final_metrics:
            logger.info("Final quality metrics summary:")
            for strategy in sorted(output_files.keys()):
                if strategy in final_metrics:
                    status: str = SUCCESS_SYMBOL_MINOR if targets_met_dict.get(strategy, False) else FAILURE_SYMBOL_MINOR
                    logger.info(f"  {status} {strategy}:")
                    if quality_targets:
                        for target in quality_targets:
                            key = f"{target.metric}_{target.statistic}"
                            if key in final_metrics[strategy]:
                                logger.info(
                                    f"      {target.metric}-{target.statistic}: "
                                    f"{final_metrics[strategy][key]:.2f}"
                                )
                    if strategy in metrics_plots:
                        logger.info(f"      Plot: {metrics_plots[strategy]}")

        return MergeResult(
            output_files=output_files,
            frame_counts=frame_counts,
            final_metrics=final_metrics,
            targets_met=targets_met_dict,
            metrics_plots=metrics_plots,
            outcome=PhaseOutcome.COMPLETED,
        )

    except Exception as e:
        logger.critical(f"Merging phase failed: {e}", exc_info=True)
        return MergeResult(
            output_files={},
            frame_counts={},
            final_metrics={},
            targets_met={},
            metrics_plots={},
            outcome=PhaseOutcome.FAILED,
            error=str(e),
        )
