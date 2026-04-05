"""CLI interface for the quality-based encoding pipeline."""
# CHerSun 2026

import argparse
import logging
import os
import sys
from pathlib import Path

import psutil

import pyqenc
from pyqenc.constants import (
    DEFAULT_METRICS_SAMPLING,
    DEFAULT_SCREENSHOT_COUNT,
    FAILURE_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MAJOR,
)
from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
    PipelineConfig,
    QualityTarget,
)
from pyqenc.state import ArtifactState
from pyqenc.utils.log_format import fmt_key_value_table
from pyqenc.utils.logging import setup_logging

logger = logging.getLogger(__name__)


def _set_process_priority() -> None:
    """Set main process priority to below normal."""
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
        logger.debug("Process priority set to below normal")
    except Exception as e:
        logger.warning(f"Failed to set process priority: {e}")


def _parse_quality_targets(targets_str: str) -> list[str]:
    """Parse comma-separated quality targets, i.e. "vmaf-min:95,ssim-med:98" into a list of strings"""
    return [t.strip() for t in targets_str.split(",") if t.strip()]


def _parse_strategies(strategies_str: str | None) -> list[str] | None:
    """Parse comma-separated encoding strategies, i.e. "slow+h265-aq,veryslow+h264-anime" into a list of strings. None meaning use defaults, while empty string means all combinations."""
    if strategies_str is None:
        return None

    if strategies_str.strip() == "":
        return [""]  # Empty string means all combinations

    return [s.strip() for s in strategies_str.split(",") if s.strip()]


def _parse_cleanup_level(cleanup_value: str | None) -> CleanupLevel:
    """Parse the --cleanup flag value into a ``CleanupLevel``. None for no cleanup, "" for intermediate, "all" for all.

    Raises:
        argparse.ArgumentTypeError: If the value is not recognised.
    """
    if cleanup_value is None:
        return CleanupLevel.NONE
    if cleanup_value.lower() == "":
        return CleanupLevel.INTERMEDIATE
    if cleanup_value.lower() == "all":
        return CleanupLevel.ALL
    raise argparse.ArgumentTypeError(
        f"Invalid --cleanup value '{cleanup_value}'. Expected no argument or 'all'."
    )


def _add_base_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments universal to ALL subcommands (including measure)."""
    parser.add_argument("--work-dir", type=Path, default=Path("./pyqenc"), help="Working directory for intermediate files and state (default: ./pyqenc)")
    parser.add_argument("--log-level", choices=["debug", "info", "warning", "critical"], default="info", help="Logging level (default: info)")


def _add_pipeline_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments specific to pipeline phases (NOT used by measure)."""
    parser.add_argument("-y", "--execute", action="store_true", default=False, help="Execute phases (default: dry-run). Without this flag only a dry-run is performed.")
    parser.add_argument("--cleanup", nargs="?", const="intermediate", metavar="all", help=(
            "Cleanup level for intermediate files. "
            "--cleanup (no argument): delete workspace files per artifact after completion. "
            "--cleanup all: also delete remaining intermediate directories after full pipeline success."
        ),
    )
    parser.add_argument("--force", action="store_true", help=(
            "When a source-file mismatch is detected in execute mode (-y), "
            "delete all intermediate artifacts and reset state, then continue "
            "with the new source file. Has no effect without -y."
        ),
    )
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        default=False,
        help="Suppress metrics.yaml output (metrics are still collected internally but not written to disk)",
    )


def _add_quality_arguments(parser: argparse.ArgumentParser) -> None:
    """Add quality-related arguments.

    Args:
        parser: Argument parser to add arguments to
    """
    parser.add_argument(
        "--quality-target",
        type=str,
        default="vmaf-min:94,vmaf-med:97,psnr-min:42,ssim-min:94",
        help="Quality targets (e.g., 'vmaf-min:95,ssim-med:98') (default: vmaf-min:94,vmaf-med:97,psnr-min:42,ssim-min:94). NOTE: all metrics are scaled to 0-100 range, so targets should be specified accordingly (e.g., ssim-med:98 means 0.98 raw SSIM)."
    )
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help="Encoding strategies (e.g., 'slow+h265-aq,veryslow+h264-anime'). "
             "If not specified, uses default from config file. "
             "Use empty string '' for all combinations."
    )
    parser.add_argument(
        "--all-strategies",
        action="store_true",
        help="Disable optimization phase and produce output for all strategies (default: picks the best strategy during optimization)"
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=2,
        help="Maximum concurrent encoding processes (default: 2). Don't set this high, ffmpeg knows how to scale too."
    )
    parser.add_argument(
        "--metrics-sampling",
        type=int,
        default=DEFAULT_METRICS_SAMPLING,
        metavar="N",
        help=(
            "Metrics sampling factor: measure every N-th frame. "
            f"Min: 1 (every frame measured). Default: {DEFAULT_METRICS_SAMPLING}. Directly affects reliability of metrics. A tradeoff between precision and speed. "
            "Values above 30 are not recommended due to measurement volatility. 1 gives the highest precision but lowest speed. 2-4 are a good compromise. 5-10 start to become unreliable.."
        ),
    )
    parser.add_argument(
        "--no-visual-hash",
        action="store_true",
        help="Disable emoji visual hash prefix on chunk log lines (default: enabled).",
    )


def _add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    """Add stream filter arguments.

    Args:
        parser: Argument parser to add arguments to
    """
    parser.add_argument(
        "--include",
        type=str,
        help="Regex pattern to include streams across all types (e.g. '.*eng.*')"
    )
    parser.add_argument(
        "--exclude",
        type=str,
        help="Regex pattern to exclude streams across all types (e.g. 'attachment')"
    )

def _add_audio_convert_arguments(parser: argparse.ArgumentParser) -> None:
    """Add audio conversion arguments for the audio output phase.

    Args:
        parser: Argument parser to add arguments to
    """
    parser.add_argument(
        "--audio-convert",
        type=str,
        default=None,
        metavar="REGEX",
        help=(
            "Regex pattern selecting processed audio files to convert to the final delivery format. "
            "Overrides the config-derived audio_output.convert_filter for this run."
        ),
    )
    parser.add_argument(
        "--audio-codec",
        type=str,
        default=None,
        metavar="CODEC",
        help="Override the audio codec for all conversion profiles in this run (e.g. 'aac').",
    )
    parser.add_argument(
        "--audio-bitrate",
        type=str,
        default=None,
        metavar="BITRATE",
        help=(
            "Base bitrate for 2.0 stereo conversion (e.g. '192k'). "
            "Bitrates for other channel layouts are scaled proportionally by channel count."
        ),
    )


def _add_crop_arguments(parser: argparse.ArgumentParser) -> None:
    """Add crop-related arguments.

    Args:
        parser: Argument parser to add arguments to
    """
    crop_group = parser.add_mutually_exclusive_group()
    crop_group.add_argument(
        "--no-crop",
        action="store_true",
        help="Disable automatic black border detection and cropping"
    )
    crop_group.add_argument(
        "--crop",
        type=str,
        metavar="PARAMS",
        help="Manual crop parameters: 'top bottom' or 'top bottom left right'"
    )


def _resolve_crop_params(args: argparse.Namespace) -> CropParams | None:
    """Parse crop parameters from CLI args into a CropParams instance.

    Returns:
        An explicit ``CropParams`` (including empty/no-op) if ``--crop`` or ``--no-crop`` given.
        ``None`` as a sentinel meaning "auto-resolve from job.yaml" (handled by the phase layer).

    Raises:
        ValueError: On bad ``--crop`` format. Caller should catch and log critical.
    """
    if getattr(args, "no_crop", False):
        return CropParams(top=0, bottom=0, left=0, right=0)
    crop_str = getattr(args, "crop", None)
    if crop_str:
        return CropParams.parse(crop_str)
    return None


def _create_auto_subcommand(subparsers) -> None:
    """Create the 'auto' subcommand for full pipeline execution.

    Args:
        subparsers: Subparsers object to add command to
    """
    auto_parser = subparsers.add_parser(
        "auto",
        help="Execute complete pipeline from extraction to final merge"
    )
    auto_parser.add_argument(
        "source",
        type=Path,
        help="Source MKV video file"
    )
    _add_base_arguments(auto_parser)
    _add_pipeline_arguments(auto_parser)
    _add_quality_arguments(auto_parser)
    _add_filter_arguments(auto_parser)
    _add_crop_arguments(auto_parser)
    _add_audio_convert_arguments(auto_parser)
    auto_parser.add_argument(
        "--remux-chunking",
        action="store_true",
        help=(
            "Use stream-copy (remux) for chunking instead of the default FFV1 lossless re-encode. "
            "Trades frame-perfect chunk boundaries for faster chunking and smaller intermediate files."
        ),
    )
    auto_parser.set_defaults(func=_cmd_auto)


def _create_extract_subcommand(subparsers) -> None:
    """Create the 'extract' subcommand for stream extraction."""
    p = subparsers.add_parser("extract", help="Extract video and audio streams from source MKV")
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_filter_arguments(p)
    _add_crop_arguments(p)
    p.set_defaults(func=_cmd_extract)


def _create_chunk_subcommand(subparsers) -> None:
    """Create the 'chunk' subcommand for video chunking."""
    p = subparsers.add_parser("chunk", help="Split extracted video into scene-based chunks")
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    p.add_argument("--scene-threshold", type=float, default=0.3,
                   help="Scene detection sensitivity 0.0-1.0 (default: 0.3)")
    p.add_argument("--min-scene-length", type=int, default=24,
                   help="Minimum frames per chunk (default: 24)")
    p.add_argument("--remux-chunking", action="store_true",
                   help="Use stream-copy (remux) instead of FFV1 lossless re-encode.")
    p.set_defaults(func=_cmd_chunk)


def _create_encode_subcommand(subparsers) -> None:
    """Create the 'encode' subcommand for chunk encoding."""
    p = subparsers.add_parser("encode", help="Encode chunks to meet quality targets")
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_quality_arguments(p)
    p.set_defaults(func=_cmd_encode)


def _create_audio_subcommand(subparsers) -> None:
    """Create the 'audio' subcommand for audio processing."""
    p = subparsers.add_parser("audio", help="Process audio streams with normalization")
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_audio_convert_arguments(p)
    p.set_defaults(func=_cmd_audio)


def _create_merge_subcommand(subparsers) -> None:
    """Create the 'merge' subcommand for final video merging."""
    p = subparsers.add_parser("merge", help="Merge encoded chunks and audio into final MKV files")
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    p.set_defaults(func=_cmd_merge)


def _cmd_auto(args: argparse.Namespace) -> int:
    """Execute the 'auto' subcommand.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pyqenc.api import run_pipeline
    from pyqenc.config import ConfigManager

    logger.info("Starting automatic pipeline execution...")
    logger.info("")

    # Parse execution-related flags
    execute = args.execute
    cleanup = _parse_cleanup_level(args.cleanup)
    # Parse strategies
    strategies = _parse_strategies(args.strategies)
    # Parse quality targets and strategies
    try:
        quality_targets = [QualityTarget.parse(t) for t in _parse_quality_targets(args.quality_target)]
    except ValueError as e:
        logger.critical(f"Invalid quality target: {e}")
        return 1
    # Parse crop parameters
    try:
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    # Resolve metrics sampling: CLI arg takes precedence over config file
    config_manager = ConfigManager()
    metrics_sampling = args.metrics_sampling if args.metrics_sampling is not None \
                       else config_manager.get_metrics_sampling()

    # Resolve strategy patterns → typed Strategy objects
    resolved_strategies = config_manager.resolve_strategies(strategies)

    # Aggregate into a key/value table and print it
    strategy_display = (
        "using defaults from config file" if strategies is None
        else "all combinations" if strategies == [""]
        else ", ".join(s.name for s in resolved_strategies)
    )
    kv_to_show = {
        "Source:":        args.source,
        "Work directory:": args.work_dir,
        "Cropping:":      "disabled" if args.no_crop else f"manual ({crop_params})" if crop_params else "automatic",
        "Strategies:":    strategy_display,
        "Targets:":       ", ".join(str(t) for t in quality_targets),
        "Work mode:":     "DRY-RUN (no changes will be made)" if not execute else "EXECUTE",
    }
    fmt_key_value_table(kv_to_show)
    logger.info("")

    # Create pipeline configuration
    config = PipelineConfig(
        source_video=args.source,
        work_dir=args.work_dir,
        quality_targets=quality_targets,
        strategies=resolved_strategies,
        optimize=not args.all_strategies,  # optimize unless --all-strategies requested
        max_parallel=args.max_parallel,
        log_level=args.log_level,
        include=args.include,
        exclude=args.exclude,
        crop_params=crop_params,
        cleanup=cleanup,
        chunking_mode=ChunkingMode.REMUX if args.remux_chunking else ChunkingMode.LOSSLESS,
        force=args.force if hasattr(args, "force") else False,
        audio_convert=args.audio_convert,
        audio_codec=args.audio_codec,
        audio_base_bitrate=args.audio_bitrate,
        metrics_sampling=metrics_sampling,
        visual_hash=not args.no_visual_hash,
        no_metrics=args.no_metrics,
    )

    # Execute pipeline
    try:
        result = run_pipeline(config, dry_run=not execute)
        if result.success:
            logger.info(f"{SUCCESS_SYMBOL_MAJOR} Pipeline completed successfully")
            return 0
        logger.critical(f"{FAILURE_SYMBOL_MAJOR} Pipeline execution failed: {result.error}")
        return 1
    except Exception as e:
        logger.critical(f"{FAILURE_SYMBOL_MAJOR} Pipeline execution failed: {e}", exc_info=True)
        return 1

def _cmd_extract(args: argparse.Namespace) -> int:
    """Execute the 'extract' subcommand."""
    from pyqenc.api import extract_streams

    logger.info("Starting stream extraction")
    logger.info(f"Source: {args.source}")

    try:
        _resolve_crop_params(args)  # validate format early; crop is stored in job.yaml by the job phase
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    try:
        result = extract_streams(
            source_video = args.source,
            work_dir     = args.work_dir,
            include      = getattr(args, "include", None),
            exclude      = getattr(args, "exclude", None),
            force        = args.force,
            dry_run      = not args.execute,
        )
        if result.is_complete:
            logger.info("Extraction completed successfully")
            return 0
        logger.critical(f"Extraction failed: {result.error}")
        return 1
    except Exception as e:
        logger.critical(f"Extraction failed: {e}", exc_info=True)
        return 1


def _cmd_chunk(args: argparse.Namespace) -> int:
    """Execute the 'chunk' subcommand."""
    from pyqenc.api import chunk_video
    from pyqenc.models import ChunkingMode

    logger.info("Starting video chunking")
    logger.info(f"Source: {args.source}")

    chunking_mode = ChunkingMode.REMUX if args.remux_chunking else ChunkingMode.LOSSLESS

    try:
        result = chunk_video(
            source_video     = args.source,
            work_dir         = args.work_dir,
            scene_threshold  = args.scene_threshold,
            min_scene_length = args.min_scene_length,
            chunking_mode    = chunking_mode,
            force            = args.force,
            dry_run          = not args.execute,
        )
        if result.is_complete:
            logger.info("Chunking completed successfully")
            return 0
        logger.critical(f"Chunking failed: {result.error}")
        return 1
    except Exception as e:
        logger.critical(f"Chunking failed: {e}", exc_info=True)
        return 1


def _cmd_encode(args: argparse.Namespace) -> int:
    """Execute the 'encode' subcommand."""
    from pyqenc.api import encode_chunks

    logger.info("Starting chunk encoding")
    logger.info(f"Source: {args.source}")

    _quality_target_strs = _parse_quality_targets(args.quality_target)
    strategies = _parse_strategies(args.strategies)

    try:
        result = encode_chunks(
            source_video    = args.source,
            work_dir        = args.work_dir,
            strategies      = strategies or [],
            quality_targets = _quality_target_strs,
            max_parallel    = args.max_parallel,
            force           = args.force,
            dry_run         = not args.execute,
        )
        if result.is_complete:
            logger.info("Encoding completed successfully")
            return 0
        logger.critical(f"Encoding failed: {result.error}")
        return 1
    except Exception as e:
        logger.critical(f"Encoding failed: {e}", exc_info=True)
        return 1


def _cmd_audio(args: argparse.Namespace) -> int:
    """Execute the 'audio' subcommand."""
    from pyqenc.api import process_audio

    logger.info("Starting audio processing")
    logger.info(f"Source: {args.source}")

    try:
        result = process_audio(
            source_video       = args.source,
            work_dir           = args.work_dir,
            audio_convert      = getattr(args, "audio_convert", None),
            audio_codec        = getattr(args, "audio_codec", None),
            audio_base_bitrate = getattr(args, "audio_bitrate", None),
            dry_run            = not args.execute,
        )
        if result.is_complete:
            logger.info("Audio processing completed successfully")
            return 0
        logger.critical(f"Audio processing failed: {result.error}")
        return 1
    except Exception as e:
        logger.critical(f"Audio processing failed: {e}", exc_info=True)
        return 1


def _cmd_merge(args: argparse.Namespace) -> int:
    """Execute the 'merge' subcommand."""
    from pyqenc.api import merge_final

    logger.info("Starting final merge")
    logger.info(f"Source: {args.source}")

    try:
        result = merge_final(
            source_video = args.source,
            work_dir     = args.work_dir,
            dry_run      = not args.execute,
        )
        if result.is_complete:
            completed = [a for a in result.merged if a.state == ArtifactState.COMPLETE]
            if completed:
                logger.info(f"Merge completed successfully: {len(completed)} file(s)")
                for artifact in completed:
                    logger.info(f"  {artifact.path}")
            else:
                logger.info("Merge completed (no new files created)")
            return 0
        logger.critical(f"Merge failed: {result.error}")
        return 1
    except Exception as e:
        logger.critical(f"Merge failed: {e}", exc_info=True)
        return 1


def _create_config_subcommand(subparsers) -> None:
    """Create the 'config' subcommand for copying configuration files."""
    p = subparsers.add_parser(
        "config",
        help="Copy the active config to a target location for customisation",
        description=(
            "Finds the active config (current dir > user home > built-in default) "
            "and copies it to the target location. "
            "Without -y only announces what would be done."
        ),
    )
    p.add_argument(
        "target_dir",
        nargs="?",
        type=Path,
        default=None,
        help=(
            "Target directory for the config copy. "
            "Omit to target user home (~/.config/pyqenc/config.yaml). "
            "Use '.' for the current working directory (pyqenc.yaml)."
        ),
    )
    p.add_argument(
        "-y", "--execute",
        action="store_true",
        default=False,
        help="Execute the copy (default: dry-run, only announce).",
    )
    p.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "critical"],
        default="info",
        help="Logging level (default: info)",
    )
    p.set_defaults(func=_cmd_config)


def _cmd_config(args: argparse.Namespace) -> int:
    """Execute the 'config' subcommand."""
    import shutil

    from pyqenc.config import find_config_source
    from pyqenc.constants import (
        CONFIG_DIR_HOME,
        CONFIG_FILENAME_CWD,
        CONFIG_FILENAME_HOME,
    )

    # Resolve target path
    if args.target_dir is None:
        target = Path.home() / CONFIG_DIR_HOME / CONFIG_FILENAME_HOME
    else:
        target_dir = args.target_dir.resolve()
        if target_dir == Path.cwd().resolve():
            target = Path.cwd() / CONFIG_FILENAME_CWD
        else:
            target = target_dir / CONFIG_FILENAME_HOME

    # Find source
    try:
        source = find_config_source()
    except FileNotFoundError as e:
        logger.critical("Cannot locate any config file: %s", e)
        return 1

    source = source.resolve()
    target = target.resolve()

    logger.debug("Config source resolved to: %s", source)
    logger.debug("Config target resolved to: %s", target)

    # Guard: source == target
    if source == target:
        logger.error(
            "Source and target are the same file (%s) — nothing to do.", source
        )
        return 1

    execute: bool = args.execute

    if not execute:
        logger.info("DRY-RUN: would copy config")
        logger.info("  from: %s", source)
        logger.info("    to: %s", target)
        logger.info("Run with -y / --execute to apply.")
        return 0

    # Execute copy (.tmp-then-rename for atomicity)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, tmp)
        tmp.rename(target)
    except Exception as e:
        logger.critical("Failed to copy config: %s", e)
        tmp.unlink(missing_ok=True)
        return 1

    logger.info("Config copied")
    logger.info("  from: %s", source)
    logger.info("    to: %s", target)
    return 0


def _create_measure_subcommand(subparsers) -> None:
    """Create the 'measure' subcommand for standalone quality measurement."""
    p = subparsers.add_parser("measure", help="Measure quality metrics between source and encoded video(s)")
    p.add_argument(
        "source",
        type=Path,
        help=(
            "Reference (original/lossless) video file. "
            "ORDER MATTERS: swapping source and target produces incorrect metrics (VMAF is not symmetric)."
        ),
    )
    p.add_argument(
        "targets",
        type=Path,
        nargs="*",
        default=[],
        help=(
            "Zero or more encoded/distorted video files to evaluate against the source. "
            "Omit all to run in screenshots-only mode (no metric computation)."
        ),
    )
    _add_base_arguments(p)
    _add_crop_arguments(p)
    p.add_argument(
        "--metrics-sampling",
        type=int,
        default=DEFAULT_METRICS_SAMPLING,
        metavar="N",
        help=(
            "Metrics sampling factor: measure every N-th frame. "
            f"Min: 1 (every frame measured). Default: {DEFAULT_METRICS_SAMPLING}. Directly affects reliability of metrics. A tradeoff between precision and speed. "
            "Values above 30 are not recommended due to measurement volatility. 1 gives the highest precision but lowest speed. 2-4 are a good compromise. 5-10 start to become unreliable.."
        ),
    )
    p.add_argument(
        "--width",
        type=int,
        default=None,
        metavar="W",
        help=(
            "Scale both source and target to width W (preserving aspect ratio) during metric "
            "computation. Crop is applied first. Does not affect screenshots."
        ),
    )
    p.add_argument(
        "--screenshots",
        type=int,
        default=DEFAULT_SCREENSHOT_COUNT,
        metavar="N",
        help=f"Screenshots to capture from each video (default: {DEFAULT_SCREENSHOT_COUNT}, min 1)",
    )
    p.add_argument(
        "--every",
        type=str,
        default=None,
        metavar="DURATION",
        help=(
            "Capture one screenshot per interval (e.g. 30, 30s, 5m, 1h30m). "
            "Can be combined with --screenshots to cap the total count."
        ),
    )
    p.set_defaults(func=_cmd_measure)


def _cmd_measure(args: argparse.Namespace) -> int:
    """Execute the 'measure' subcommand."""
    from pyqenc.api import measure_quality

    try:
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    try:
        measure_quality(
            source_video         = args.source,
            target_videos        = args.targets,
            work_dir             = args.work_dir,
            crop_params          = crop_params,
            metrics_sampling     = args.metrics_sampling,
            screenshot_count     = args.screenshots,
            screenshot_interval  = args.every,
            width                = args.width,
        )
        return 0
    except FileNotFoundError as e:
        logger.critical("%s", e)
        return 1
    except ValueError as e:
        logger.critical("Invalid arguments: %s", e)
        return 1
    except Exception as e:
        logger.critical("Measure failed: %s", e, exc_info=True)
        return 1


def main() -> int:
    """Main CLI entry point.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Create main parser
    parser = argparse.ArgumentParser(
        prog="pyqenc",
        description="Quality-based video encoding pipeline with automatic cropping",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run mode (default) - see what would be done
  pyqenc auto source.mkv

  # Execute all phases with default settings
  pyqenc auto source.mkv -y

  # Custom quality target and strategies
  pyqenc auto source.mkv --quality-target vmaf-min:95 --strategies slow+h265-aq -y

  # Use all strategy combinations
  pyqenc auto source.mkv --strategies "" -y

  # Disable optimization (encode all strategies)
  pyqenc auto source.mkv --all-strategies -y

  # Disable automatic cropping
  pyqenc auto source.mkv --no-crop -y

  # Manual crop specification
  pyqenc auto source.mkv --crop "140 140" -y

  # Multiple strategies
  pyqenc auto source.mkv --strategies slow+h265-aq,veryslow+h264 -y

  # Keep intermediate files after completion (default)
  pyqenc auto source.mkv -y

  # Delete CRF attempt files as each chunk completes
  pyqenc auto source.mkv -y --cleanup

  # Delete all intermediate directories after full pipeline success
  pyqenc auto source.mkv -y --cleanup all
        """
    )

    # Add version flag
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {pyqenc.__version__}"
    )

    # Create subparsers
    subparsers = parser.add_subparsers(
        title="subcommands",
        description="Available pipeline phases",
        dest="subcommand",
        required=True
    )

    # Add subcommands
    _create_auto_subcommand(subparsers)
    _create_extract_subcommand(subparsers)
    _create_chunk_subcommand(subparsers)
    _create_encode_subcommand(subparsers)
    _create_audio_subcommand(subparsers)
    _create_merge_subcommand(subparsers)
    _create_config_subcommand(subparsers)
    _create_measure_subcommand(subparsers)

    # Parse arguments
    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)
    logger.info("Welcome to pyqenc v%s", pyqenc.__version__)

    # Set process priority
    _set_process_priority()

    # Install SIGINT handler early so CTRL+C always triggers immediate exit,
    # overriding asyncio's default handler which swallows the first keypress.
    import signal

    from pyqenc.metrics import flush_active_collector
    from pyqenc.utils.ffmpeg_runner import kill_all_ffmpeg

    def _sigint_handler(signum: int, frame: object) -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        kill_all_ffmpeg()
        flush_active_collector()
        logger.warning("Cancelled by user.")
        os._exit(130)

    signal.signal(signal.SIGINT, _sigint_handler)

    # Execute subcommand
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
