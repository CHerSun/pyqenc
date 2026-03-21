"""CLI interface for the quality-based encoding pipeline."""
# CHerSun 2026

import argparse
import logging
import sys
from pathlib import Path

import psutil

import pyqenc
from pyqenc.constants import CRF_GRANULARITY, FAILURE_SYMBOL_MAJOR, SUCCESS_SYMBOL_MAJOR
from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
    PipelineConfig,
    QualityTarget,
)
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
    """Parse comma-separated quality targets.

    Args:
        targets_str: Comma-separated targets like "vmaf-min:95,ssim-med:98"

    Returns:
        List of target strings
    """
    return [t.strip() for t in targets_str.split(",") if t.strip()]


def _parse_strategies(strategies_str: str | None) -> list[str] | None:
    """Parse comma-separated encoding strategies.

    Args:
        strategies_str: Comma-separated strategies like "slow+h265-aq,veryslow+h264-anime",
                       or None to use config defaults, or empty string for all combinations

    Returns:
        List of strategy strings, or None if not specified (use config defaults)
    """
    if strategies_str is None:
        return None

    if strategies_str.strip() == "":
        return [""]  # Empty string means all combinations

    return [s.strip() for s in strategies_str.split(",") if s.strip()]


def _parse_cleanup_level(cleanup_value: str | None) -> CleanupLevel:
    """Parse the --cleanup flag value into a ``CleanupLevel``.

    Args:
        cleanup_value: ``None`` (flag absent), ``""`` / ``"intermediate"``
                       (flag present, no argument), or ``"all"``.

    Returns:
        Corresponding ``CleanupLevel`` enum value.

    Raises:
        argparse.ArgumentTypeError: If the value is not recognised.
    """
    if cleanup_value is None:
        return CleanupLevel.NONE
    if cleanup_value.lower() in ("", "intermediate"):
        return CleanupLevel.INTERMEDIATE
    if cleanup_value.lower() == "all":
        return CleanupLevel.ALL
    raise argparse.ArgumentTypeError(
        f"Invalid --cleanup value '{cleanup_value}'. Expected no argument or 'all'."
    )


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add common arguments shared across subcommands.

    Args:
        parser: Argument parser to add arguments to
    """
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("./work"),
        help="Working directory for intermediate files and state (default: ./work)"
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "critical"],
        default="info",
        help="Logging level (default: info)"
    )
    parser.add_argument(
        "-y", "--execute",
        action="store_true",
        default=False,
        help="Execute phases (default: dry-run). Without this flag only a dry-run is performed."
    )
    parser.add_argument(
        "--cleanup",
        nargs="?",
        const="intermediate",
        metavar="all",
        help=(
            "Cleanup level for intermediate files. "
            "--cleanup (no argument): delete workspace files per artifact after completion. "
            "--cleanup all: also delete remaining intermediate directories after full pipeline success."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "When a source-file mismatch is detected in execute mode (-y), "
            "delete all intermediate artifacts and reset state, then continue "
            "with the new source file. Has no effect without -y."
        ),
    )


def _add_quality_arguments(parser: argparse.ArgumentParser) -> None:
    """Add quality-related arguments.

    Args:
        parser: Argument parser to add arguments to
    """
    parser.add_argument(
        "--quality-target",
        type=str,
        default="vmaf-min:93,vmaf-med:96",
        help="Quality targets (e.g., 'vmaf-min:95,ssim-med:98') (default: vmaf-min:93,vmaf-med:96). NOTE: all metrics are scaled to 0-100 range, so targets should be specified accordingly (e.g., ssim-med:98 means 0.98 raw SSIM)."
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
        default=None,
        metavar="N",
        help=(
            "Metrics sampling factor: measure every N-th frame. "
            "Min: 1 (every frame measured). Default: 10 (recommended balance of speed and precision). "
            "Values above 30 are not recommended due to measurement volatility."
        ),
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
    _add_common_arguments(auto_parser)
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
    """Create the 'extract' subcommand for stream extraction.

    Args:
        subparsers: Subparsers object to add command to
    """
    extract_parser = subparsers.add_parser(
        "extract",
        help="Extract video and audio streams from source MKV"
    )
    extract_parser.add_argument(
        "source",
        type=Path,
        help="Source MKV video file"
    )
    _add_common_arguments(extract_parser)
    _add_filter_arguments(extract_parser)
    _add_crop_arguments(extract_parser)
    extract_parser.set_defaults(func=_cmd_extract)


def _create_chunk_subcommand(subparsers) -> None:
    """Create the 'chunk' subcommand for video chunking.

    Args:
        subparsers: Subparsers object to add command to
    """
    chunk_parser = subparsers.add_parser(
        "chunk",
        help="Split video into scene-based chunks"
    )
    chunk_parser.add_argument(
        "video",
        type=Path,
        help="Video file to chunk"
    )
    _add_common_arguments(chunk_parser)
    chunk_parser.add_argument(
        "--scene-threshold",
        type=float,
        default=0.3,
        help="Scene detection sensitivity 0.0-1.0 (default: 0.3)"
    )
    chunk_parser.add_argument(
        "--min-scene-length",
        type=int,
        default=24,
        help="Minimum frames per chunk (default: 24)"
    )
    chunk_parser.add_argument(
        "--remux-chunking",
        action="store_true",
        help=(
            "Use stream-copy (remux) for chunking instead of the default FFV1 lossless re-encode. "
            "Trades frame-perfect chunk boundaries for faster chunking and smaller intermediate files."
        ),
    )
    chunk_parser.set_defaults(func=_cmd_chunk)


def _create_encode_subcommand(subparsers) -> None:
    """Create the 'encode' subcommand for chunk encoding.

    Args:
        subparsers: Subparsers object to add command to
    """
    encode_parser = subparsers.add_parser(
        "encode",
        help="Encode chunks to meet quality targets"
    )
    encode_parser.add_argument(
        "chunks_dir",
        type=Path,
        help="Directory containing chunks to encode"
    )
    _add_common_arguments(encode_parser)
    _add_quality_arguments(encode_parser)
    encode_parser.set_defaults(func=_cmd_encode)


def _create_audio_subcommand(subparsers) -> None:
    """Create the 'audio' subcommand for audio processing.

    Args:
        subparsers: Subparsers object to add command to
    """
    audio_parser = subparsers.add_parser(
        "audio",
        help="Process audio streams with normalization"
    )
    audio_parser.add_argument(
        "audio_dir",
        type=Path,
        help="Directory containing audio files to process"
    )
    _add_common_arguments(audio_parser)
    _add_audio_convert_arguments(audio_parser)
    audio_parser.set_defaults(func=_cmd_audio)


def _create_merge_subcommand(subparsers) -> None:
    """Create the 'merge' subcommand for final video merging.

    Args:
        subparsers: Subparsers object to add command to
    """
    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge encoded chunks and audio into final MKV files"
    )
    merge_parser.add_argument(
        "encoded_dir",
        type=Path,
        help="Directory containing encoded chunks organized by strategy"
    )
    merge_parser.add_argument(
        "audio_dir",
        type=Path,
        help="Directory containing processed audio files"
    )
    _add_common_arguments(merge_parser)
    merge_parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory for final MKV files (default: work_dir/final)"
    )
    merge_parser.add_argument(
        "--verify-frames",
        action="store_true",
        default=True,
        help="Verify frame count matches source (default: True)"
    )
    merge_parser.set_defaults(func=_cmd_merge)


def _cmd_auto(args: argparse.Namespace) -> int:
    """Execute the 'auto' subcommand.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pyqenc.api import run_pipeline
    from pyqenc.config import ConfigManager

    logger.info("Starting automatic pipeline execution")

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
    crop_params: CropParams | None = None
    if hasattr(args, "crop") and args.crop:
        try:
            crop_params = CropParams.parse(args.crop)
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
    kv_to_show = {
        "Source:": args.source,
        "Work directory:": args.work_dir,
        "CRF granularity:": CRF_GRANULARITY,
        "Cropping:": "disabled" if args.no_crop else f"manual ({crop_params})" if crop_params else "automatic",
        "Strategies:": "using defaults from config file" if strategies is None \
                      else "all combinations" if strategies == [""] \
                      else ", ".join(s.name for s in resolved_strategies),
        "Targets:": ", ".join(str(t) for t in quality_targets),
        "Work mode:": "DRY-RUN (no changes will be made)" if not execute else "EXECUTE",
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
        all_strategies=args.all_strategies,
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
    """Execute the 'extract' subcommand.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pyqenc.api import extract_streams

    logger.info("Starting stream extraction")
    logger.info(f"Source: {args.source}")

    # Parse execute flag
    execute = args.execute

    try:
        result = extract_streams(
            source_video=args.source,
            output_dir=args.work_dir / "extracted",
            include=args.include if hasattr(args, "include") else None,
            exclude=args.exclude if hasattr(args, "exclude") else None,
            dry_run=not execute,
        )

        if result.success:
            logger.info("Extraction completed successfully")
            return 0
        else:
            logger.critical(f"Extraction failed: {result.error}")
            return 1
    except Exception as e:
        logger.critical(f"Extraction failed: {e}", exc_info=True)
        return 1


def _cmd_chunk(args: argparse.Namespace) -> int:
    """Execute the 'chunk' subcommand.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pyqenc.api import chunk_video
    from pyqenc.models import ChunkingMode

    logger.info("Starting video chunking")
    logger.info(f"Video: {args.video}")

    # Parse execute flag
    execute = args.execute

    chunking_mode = ChunkingMode.REMUX if args.remux_chunking else ChunkingMode.LOSSLESS

    try:
        result = chunk_video(
            video_file=args.video,
            output_dir=args.work_dir / "chunks",
            scene_threshold=args.scene_threshold,
            min_scene_length=args.min_scene_length,
            chunking_mode=chunking_mode,
            dry_run=not execute,
        )

        if result.success:
            logger.info("Chunking completed successfully")
            return 0
        else:
            logger.critical(f"Chunking failed: {result.error}")
            return 1
    except Exception as e:
        logger.critical(f"Chunking failed: {e}", exc_info=True)
        return 1


def _cmd_encode(args: argparse.Namespace) -> int:
    """Execute the 'encode' subcommand.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pyqenc.api import encode_chunks

    logger.info("Starting chunk encoding")
    logger.info(f"Chunks directory: {args.chunks_dir}")

    # Parse execute flag
    execute = args.execute

    # Parse quality targets and strategies
    _quality_target_strs = _parse_quality_targets(args.quality_target)
    try:
        quality_targets = [QualityTarget.parse(t) for t in _quality_target_strs]
    except ValueError as e:
        logger.critical(f"Invalid quality target: {e}")
        return 1
    strategies = _parse_strategies(args.strategies)

    try:
        result = encode_chunks(
            chunks_dir=args.chunks_dir,
            strategies=strategies,
            quality_targets=quality_targets,
            work_dir=args.work_dir,
            max_parallel=args.max_parallel,
            dry_run=not execute,
        )

        if result.success:
            logger.info("Encoding completed successfully")
            return 0
        else:
            logger.critical(f"Encoding failed: {result.error}")
            return 1
    except Exception as e:
        logger.critical(f"Encoding failed: {e}", exc_info=True)
        return 1


def _cmd_audio(args: argparse.Namespace) -> int:
    """Execute the 'audio' subcommand.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pyqenc.api import process_audio

    logger.info("Starting audio processing")
    logger.info(f"Audio directory: {args.audio_dir}")

    # Parse execute flag
    execute = args.execute

    try:
        result = process_audio(
            audio_dir=args.audio_dir,
            output_dir=args.work_dir / "audio",
            audio_convert=args.audio_convert,
            audio_codec=args.audio_codec,
            audio_base_bitrate=args.audio_bitrate,
            dry_run=not execute,
        )

        if result.success:
            logger.info("Audio processing completed successfully")
            return 0
        else:
            logger.critical(f"Audio processing failed: {result.error}")
            return 1
    except Exception as e:
        logger.critical(f"Audio processing failed: {e}", exc_info=True)
        return 1


def _cmd_merge(args: argparse.Namespace) -> int:
    """Execute the 'merge' subcommand.

    Args:
        args: Parsed command-line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    from pyqenc.api import merge_final

    logger.info("Starting final merge")
    logger.info(f"Encoded directory: {args.encoded_dir}")
    logger.info(f"Audio directory: {args.audio_dir}")

    # Parse execute flag
    execute = args.execute

    # Determine output directory
    output_dir = args.output_dir if hasattr(args, "output_dir") and args.output_dir else args.work_dir / "final"

    try:
        result = merge_final(
            encoded_dir=args.encoded_dir,
            audio_dir=args.audio_dir,
            output_dir=output_dir,
            verify_frames=args.verify_frames if hasattr(args, "verify_frames") else True,
            dry_run=not execute,
        )

        if result.success:
            if result.output_files:
                logger.info(f"Merge completed successfully: {len(result.output_files)} file(s)")
                for strategy, output_file in result.output_files.items():
                    logger.info(f"  {strategy}: {output_file}")
                    if strategy in result.frame_counts:
                        logger.info(f"    Frame count: {result.frame_counts[strategy]}")
            else:
                logger.info("Merge completed (no new files created)")
            return 0
        else:
            logger.critical(f"Merge failed: {result.error}")
            return 1
    except Exception as e:
        logger.critical(f"Merge failed: {e}", exc_info=True)
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

    # Parse arguments
    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)

    # Set process priority
    _set_process_priority()

    # Execute subcommand
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
