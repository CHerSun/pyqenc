"""CLI interface for the quality-based encoding pipeline."""
# CHerSun 2026

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from pyqenc.app_config import AppConfig

import pyqenc
from pyqenc.constants import (
    DEFAULT_SCREENSHOT_COUNT,
    FAILURE_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MAJOR,
)
from pyqenc.models import (
    ChunkingMode,
    CleanupLevel,
    CropParams,
)
from pyqenc.state import ArtifactState
from pyqenc.utils.log_format import fmt_key_value_table
from pyqenc.utils.logging import setup_logging
from pyqenc.utils.long_path import LongPath

logger = logging.getLogger(__name__)


def _set_process_priority() -> None:
    """Set main process priority to below normal."""
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
        logger.debug("Process priority set to below normal")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to set process priority: {e}")


def _parse_quality_targets(targets_str: str) -> list[str]:
    """Parse comma-separated quality targets, i.e. "vmaf-min:95,ssim-med:98" into a list of strings."""
    return [t.strip() for t in targets_str.split(",") if t.strip()]


def _parse_strategies(strategies_str: str | None) -> list[str] | None:
    """Parse comma-separated encoding strategies into a list of pattern strings.

    Returns ``None`` meaning "use defaults from config", or a non-empty list
    of pattern strings to pass to the strategy expander.

    Raises:
        ValueError: If the strategies string is empty or contains only whitespace
            (an empty profile part is invalid in the pattern syntax).
    """
    if strategies_str is None:
        return None
    stripped = strategies_str.strip()
    if not stripped:
        raise ValueError(
            "Empty --strategies value is not allowed. "
            "Omit --strategies to use the defaults from config, "
            "or use '*' to select all profiles with their default presets."
        )
    return [s.strip() for s in stripped.split(",") if s.strip()]


def _parse_cleanup_level(cleanup_value: str | None) -> CleanupLevel:
    """Parse the --cleanup flag value into a ``CleanupLevel``.

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


def _resolve_crop_params(args: argparse.Namespace) -> CropParams | None:
    """Parse crop parameters from CLI args into a ``CropParams`` instance.

    Returns:
        An explicit ``CropParams`` (including empty/no-op) if ``--crop`` was given.
        ``None`` as a sentinel meaning "auto-resolve from job.yaml".

    Raises:
        ValueError: On bad ``--crop`` format.
    """
    crop_str = getattr(args, "crop", None)
    if crop_str:
        return CropParams.parse(crop_str)
    return None


# ---------------------------------------------------------------------------
# Argument group helpers
# ---------------------------------------------------------------------------

def _add_base_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments universal to ALL subcommands."""
    parser.add_argument(
        "--work-dir",
        type=LongPath,
        default=LongPath("."),
        help="Working directory for intermediate files and state (default: .)",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "critical"],
        default="info",
        help="Logging level (default: info)",
    )


def _add_pipeline_arguments(parser: argparse.ArgumentParser) -> None:
    """Add arguments common to all pipeline-phase subcommands (not used by measure/config)."""
    parser.add_argument(
        "-y", "--execute",
        action="store_true",
        default=False,
        help="Execute phases (default: dry-run). Without this flag only a dry-run is performed.",
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
    parser.add_argument(
        "--no-metrics",
        action="store_true",
        default=False,
        help="Suppress process metrics.yaml output (pipeline run stats). Does not affect quality metrics measurements.",
    )


def _add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    """Add stream filter arguments (used by subcommands that depend on ExtractionPhase)."""
    parser.add_argument(
        "--include",
        type=str,
        help="Regex pattern to include streams across all types (e.g. '.*eng.*')",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        help="Regex pattern to exclude streams across all types (e.g. 'attachment')",
    )


def _add_crop_arguments(parser: argparse.ArgumentParser) -> None:
    """Add crop arguments (used by subcommands that depend on ExtractionPhase)."""
    parser.add_argument(
        "--crop",
        type=str,
        metavar="CROP",
        help=(
            "Manual crop parameters: 'top,bottom' or 'top,bottom,left,right'. "
            "Use '0,0' to disable automatic black border detection and cropping."
        ),
    )


def _add_chunking_arguments(parser: argparse.ArgumentParser) -> None:
    """Add chunking arguments (used by subcommands that depend on ChunkingPhase)."""
    parser.add_argument(
        "--chunking-mode",
        choices=["lossless", "remux"],
        default=None,
        dest="chunking_mode",
        metavar="CHUNKING_MODE",
        help=(
            "Chunking method: 'lossless' (default) re-encodes chunks to FFV1 for frame-perfect boundaries; "
            "'remux' uses stream-copy for faster chunking and smaller intermediate files but boundaries snap to the nearest I-frame."
        ),
    )
    parser.add_argument(
        "--scene-threshold",
        type=float,
        default=None,
        help="Scene detection sensitivity 0.0-1.0 (default: from config)",
    )
    parser.add_argument(
        "--min-scene-length",
        type=int,
        default=None,
        help="Minimum frames per chunk (default: from config)",
    )


_QUALITY_TARGET_HELP: str = (
    "Quality targets as comma-separated metric-stat:value pairs "
    "(e.g. 'vmaf-min:95,ssim-med:98,vif-min:95'). "
    "All metrics are normalized to 0–100 where 100 = lossless. "
    "Landmarks: VMAF 95+ good, SSIM 98+ good, PSNR 40–60 typical, VIF 95+ good. "
    "Note: 'min' targets are unreliable with subsampling (factor>1) — "
    "worst frames may be missed. Prefer 'p05' or 'med' for reliable targeting. "
    "If not specified, uses default from config file."
)


def _add_quality_arguments(parser: argparse.ArgumentParser) -> None:
    """Add quality/encoding arguments (used by subcommands that depend on EncodingPhase)."""
    parser.add_argument(
        "--targets",
        type=str,
        default=None,
        metavar="QUALITY_TARGETS",
        dest="targets",
        help=_QUALITY_TARGET_HELP,
    )
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help=(
            "Encoding strategies as comma-separated profile[+preset] patterns "
            "(e.g. 'h265*', 'h265-aq+slow,h264+veryslow'). "
            "Profile part is required; preset part is optional — omit it to use "
            "each codec's default preset. Use '*' for all profiles with their "
            "default presets, or '*+*' for all profiles with all presets. "
            "If not specified, uses defaults from config file."
        ),
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        dest="no_optimize",
        help="Disable optimization phase and produce output for all strategies.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        dest="concurrency",
        help="Maximum concurrent encoding processes (default: from config).",
    )
    parser.add_argument(
        "--sampling",
        type=int,
        default=None,
        metavar="N",
        dest="metrics_sampling",
        help=(
            "Frame sampling factor for quality metrics measurement: measure every N-th frame. "
            "Min: 1 (every frame measured). Default: from config. "
            "Directly affects reliability of metrics. Values above 20 are not recommended."
        ),
    )
    parser.add_argument(
        "--no-visual-hash",
        action="store_true",
        help="Disable emoji visual hash prefix on chunk log lines (default: enabled).",
    )


# ---------------------------------------------------------------------------
# Config assembly helper
# ---------------------------------------------------------------------------

def _build_config(args: argparse.Namespace) -> "AppConfig":
    """Load app config and apply all CLI overrides present in *args*.

    Only attributes that are actually defined on *args* are applied, so the
    same helper works correctly for every subcommand regardless of which
    argument groups were added to its parser.

    Returns:
        Fully assembled ``AppConfig`` with CLI overrides applied and strategies
        resolved.
    """
    from pyqenc.app_config import load_app_config

    config = load_app_config()

    # --- extraction ---
    if getattr(args, "include", None) is not None:
        config.extraction.include = args.include
    if getattr(args, "exclude", None) is not None:
        config.extraction.exclude = args.exclude

    # --- chunking ---
    chunking_val = getattr(args, "chunking_mode", None)
    if chunking_val is not None:
        config.chunking.mode = (
            ChunkingMode.REMUX if chunking_val == ChunkingMode.REMUX.value
            else ChunkingMode.LOSSLESS
        )
    if getattr(args, "scene_threshold", None) is not None:
        config.chunking.scene_threshold = args.scene_threshold
    if getattr(args, "min_scene_length", None) is not None:
        config.chunking.min_scene_length = args.min_scene_length

    # --- encoding / quality ---
    quality_target_str = getattr(args, "targets", None)
    if quality_target_str is not None:
        config.encoding.targets = _parse_quality_targets(quality_target_str)

    strategies = _parse_strategies(getattr(args, "strategies", None))
    if strategies is not None:
        config.encoding.strategies = strategies

    if getattr(args, "no_optimize", False):
        config.encoding.optimize = False

    concurrency = getattr(args, "concurrency", None)
    if concurrency is not None:
        config.encoding.concurrency = concurrency

    metrics_sampling = getattr(args, "metrics_sampling", None)
    if metrics_sampling is not None:
        config.measurement.sampling = metrics_sampling

    no_visual_hash = getattr(args, "no_visual_hash", False)
    config.encoding.visual_hash = not no_visual_hash

    # Re-resolve strategies so resolved_strategies reflects all overrides.
    config.encoding.resolve(config.codecs, config.profiles)

    return config


# ---------------------------------------------------------------------------
# Subcommand definitions
# ---------------------------------------------------------------------------

def _create_auto_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Create the 'auto' subcommand for full pipeline execution."""
    p = subparsers.add_parser(
        "auto",
        help="Execute complete pipeline from extraction to final merge",
    )
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_filter_arguments(p)
    _add_crop_arguments(p)
    _add_chunking_arguments(p)
    _add_quality_arguments(p)
    p.set_defaults(func=_cmd_auto)


def _create_extract_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Create the 'extract' subcommand (runs up to and including ExtractionPhase)."""
    p = subparsers.add_parser(
        "extract",
        help="Extract video and audio streams from source MKV",
    )
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_filter_arguments(p)
    _add_crop_arguments(p)
    p.set_defaults(func=_cmd_extract)


def _create_chunk_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Create the 'chunk' subcommand (runs up to and including ChunkingPhase)."""
    p = subparsers.add_parser(
        "chunk",
        help="Split extracted video into scene-based chunks",
    )
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_filter_arguments(p)
    _add_crop_arguments(p)
    _add_chunking_arguments(p)
    p.set_defaults(func=_cmd_chunk)


def _create_encode_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Create the 'encode' subcommand (runs up to and including EncodingPhase)."""
    p = subparsers.add_parser(
        "encode",
        help="Encode chunks to meet quality targets",
    )
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_filter_arguments(p)
    _add_crop_arguments(p)
    _add_chunking_arguments(p)
    _add_quality_arguments(p)
    p.set_defaults(func=_cmd_encode)


def _create_audio_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Create the 'audio' subcommand (runs up to and including AudioPhase)."""
    p = subparsers.add_parser(
        "audio",
        help="Process audio streams with normalization",
    )
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_filter_arguments(p)
    p.set_defaults(func=_cmd_audio)


def _create_merge_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Create the 'merge' subcommand (runs up to and including MergePhase)."""
    p = subparsers.add_parser(
        "merge",
        help="Merge encoded chunks and audio into final MKV files",
    )
    p.add_argument("source", type=Path, help="Source MKV video file")
    _add_base_arguments(p)
    _add_pipeline_arguments(p)
    _add_filter_arguments(p)
    _add_crop_arguments(p)
    _add_chunking_arguments(p)
    _add_quality_arguments(p)
    p.set_defaults(func=_cmd_merge)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_auto(args: argparse.Namespace) -> int:
    """Execute the 'auto' subcommand."""
    from pyqenc.api import run_pipeline

    logger.info("Starting automatic pipeline execution...")
    logger.info("")

    try:
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    config  = _build_config(args)
    execute = args.execute
    cleanup = _parse_cleanup_level(args.cleanup)

    # Build display values from resolved config
    strategies     = _parse_strategies(getattr(args, "strategies", None))
    resolved_strats = config.encoding.resolved_strategies
    strategy_display = (
        "using defaults from config file" if strategies is None
        else ", ".join(s.name for s in resolved_strats)
    )
    kv_to_show = {
        "Source:":         args.source,
        "Work directory:": args.work_dir,
        "Cropping:":       f"manual ({crop_params})" if crop_params else "automatic",
        "Strategies:":     strategy_display,
        "Targets:":        ", ".join(str(t) for t in config.encoding.resolved_targets),
        "Work mode:":      "DRY-RUN (no changes will be made)" if not execute else "EXECUTE",
    }
    fmt_key_value_table(kv_to_show)
    logger.info("")

    try:
        result = run_pipeline(
            config      = config,
            source      = args.source,
            work_dir    = args.work_dir,
            force       = args.force,
            cleanup     = cleanup,
            no_metrics  = args.no_metrics,
            dry_run     = not execute,
            crop_params = crop_params,
        )
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
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    config  = _build_config(args)
    cleanup = _parse_cleanup_level(args.cleanup)

    try:
        result = extract_streams(
            config      = config,
            source      = args.source,
            work_dir    = args.work_dir,
            force       = args.force,
            cleanup     = cleanup,
            no_metrics  = args.no_metrics,
            dry_run     = not args.execute,
            crop_params = crop_params,
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

    logger.info("Starting video chunking")
    logger.info(f"Source: {args.source}")

    try:
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    config  = _build_config(args)
    cleanup = _parse_cleanup_level(args.cleanup)

    try:
        result = chunk_video(
            config      = config,
            source      = args.source,
            work_dir    = args.work_dir,
            force       = args.force,
            cleanup     = cleanup,
            no_metrics  = args.no_metrics,
            dry_run     = not args.execute,
            crop_params = crop_params,
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

    try:
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    config  = _build_config(args)
    cleanup = _parse_cleanup_level(args.cleanup)

    try:
        result = encode_chunks(
            config      = config,
            source      = args.source,
            work_dir    = args.work_dir,
            force       = args.force,
            cleanup     = cleanup,
            no_metrics  = args.no_metrics,
            dry_run     = not args.execute,
            crop_params = crop_params,
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

    config  = _build_config(args)
    cleanup = _parse_cleanup_level(args.cleanup)

    try:
        result = process_audio(
            config      = config,
            source      = args.source,
            work_dir    = args.work_dir,
            force       = args.force,
            cleanup     = cleanup,
            no_metrics  = args.no_metrics,
            dry_run     = not args.execute,
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
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    config  = _build_config(args)
    cleanup = _parse_cleanup_level(args.cleanup)

    try:
        result = merge_final(
            config      = config,
            source      = args.source,
            work_dir    = args.work_dir,
            force       = args.force,
            cleanup     = cleanup,
            no_metrics  = args.no_metrics,
            dry_run     = not args.execute,
            crop_params = crop_params,
        )
        if result.is_complete:
            completed = [a for a in result.artifacts if a.state == ArtifactState.COMPLETE]
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


# ---------------------------------------------------------------------------
# Config subcommand
# ---------------------------------------------------------------------------

def _create_config_subcommand(subparsers: argparse._SubParsersAction) -> None:
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
        "--work-dir",
        type=LongPath,
        default=LongPath("."),
        help="Working directory (default: .)",
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

    from pyqenc.app_config import load_app_config
    from pyqenc.constants import (
        CONFIG_DIR_HOME,
        CONFIG_FILENAME_CWD,
        CONFIG_FILENAME_HOME,
    )

    if args.target_dir is None:
        target = Path.home() / CONFIG_DIR_HOME / CONFIG_FILENAME_HOME
    else:
        target_dir = args.target_dir.resolve()
        if target_dir == Path.cwd().resolve():
            target = Path.cwd() / CONFIG_FILENAME_CWD
        else:
            target = target_dir / CONFIG_FILENAME_HOME

    _config = load_app_config()
    source  = _config._source_paths[-1].resolve()  # type: ignore[attr-defined]
    target  = target.resolve()

    logger.debug("Config source resolved to: %s", source)
    logger.debug("Config target resolved to: %s", target)

    if source == target:
        logger.error("Source and target are the same file (%s) — nothing to do.", source)
        return 1

    if not args.execute:
        logger.info("DRY-RUN: would copy config")
        logger.info("  from: %s", source)
        logger.info("    to: %s", target)
        logger.info("Run with -y / --execute to apply.")
        return 0

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, tmp)
        tmp.rename(target)
    except Exception as e:  # noqa: BLE001
        logger.critical("Failed to copy config: %s", e)
        tmp.unlink(missing_ok=True)
        return 1

    logger.info("Config copied")
    logger.info("  from: %s", source)
    logger.info("    to: %s", target)
    return 0


# ---------------------------------------------------------------------------
# Measure subcommand
# ---------------------------------------------------------------------------

def _create_measure_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Create the 'measure' subcommand for standalone quality measurement."""
    p = subparsers.add_parser(
        "measure",
        help="Measure quality metrics between source and encoded video(s)",
    )
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
        "--sampling",
        type=int,
        default=None,
        metavar="N",
        dest="metrics_sampling",
        help=(
            "Frame sampling factor for quality metrics measurement: measure every N-th frame. "
            "Min: 1 (every frame measured). Default: from config. "
            "Directly affects reliability of metrics. A tradeoff between precision and speed."
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
        help=f"Screenshots to capture from each video (default: {DEFAULT_SCREENSHOT_COUNT}, min 1). In interval mode (--every), acts as a cap.",
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
    p.add_argument(
        "--include-edges",
        action="store_true",
        default=False,
        dest="screenshot_include_edges",
        help="Include frame 0 and the last frame in screenshot positions (count mode only, default: off).",
    )
    p.set_defaults(func=_cmd_measure)


def _cmd_measure(args: argparse.Namespace) -> int:
    """Execute the 'measure' subcommand."""
    from pyqenc.api import measure_quality
    from pyqenc.app_config import load_app_config

    try:
        crop_params = _resolve_crop_params(args)
    except ValueError as e:
        logger.critical(f"Invalid crop parameters: {e}")
        return 1

    _config          = load_app_config()
    metrics_sampling = (
        args.metrics_sampling if args.metrics_sampling is not None
        else _config.measurement.sampling
    )

    try:
        measure_quality(
            source_video             = args.source,
            work_dir                 = args.work_dir,
            target_videos            = args.targets,
            crop_params              = crop_params,
            metrics_sampling         = metrics_sampling,
            screenshot_count         = args.screenshots,
            screenshot_interval      = args.every,
            screenshot_include_edges = args.screenshot_include_edges,
            width                    = args.width,
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Main CLI entry point.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
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
  pyqenc auto source.mkv --targets vmaf-min:95 --strategies h265-aq -y

  # Use all profiles with their default presets
  pyqenc auto source.mkv --strategies "*" -y

  # Disable optimization (encode all strategies)
  pyqenc auto source.mkv --no-optimize -y

  # Disable automatic cropping
  pyqenc auto source.mkv --crop "0,0" -y

  # Manual crop specification
  pyqenc auto source.mkv --crop "140,140" -y

  # Multiple strategies
  pyqenc auto source.mkv --strategies h265-aq,h264 -y

  # Delete CRF attempt files as each chunk completes
  pyqenc auto source.mkv -y --cleanup

  # Delete all intermediate directories after full pipeline success
  pyqenc auto source.mkv -y --cleanup all

  # Run only up to audio processing (extracts first if needed)
  pyqenc audio source.mkv -y

  # Run only up to extraction with custom stream filters
  pyqenc extract source.mkv --include ".*eng.*" -y
        """,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {pyqenc.__version__}",
    )

    subparsers = parser.add_subparsers(
        title="subcommands",
        description="Available pipeline phases",
        dest="subcommand",
        required=True,
    )

    _create_auto_subcommand(subparsers)
    _create_extract_subcommand(subparsers)
    _create_chunk_subcommand(subparsers)
    _create_encode_subcommand(subparsers)
    _create_audio_subcommand(subparsers)
    _create_merge_subcommand(subparsers)
    _create_config_subcommand(subparsers)
    _create_measure_subcommand(subparsers)

    args = parser.parse_args()

    setup_logging(args.log_level)
    logger.info("Welcome to pyqenc v%s", pyqenc.__version__)

    _set_process_priority()

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

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
