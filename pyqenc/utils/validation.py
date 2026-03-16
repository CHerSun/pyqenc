"""
Input validation for pipeline configuration and parameters.

This module provides validation functions for all user inputs including
file paths, quality targets, strategies, and external tool availability.
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pyqenc.models import CropParams, QualityTarget

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """Exception raised for validation errors."""
    pass


class Validator:
    """Input validator for pipeline configuration.

    Provides validation methods for all user inputs and system requirements.
    """

    # Quality target pattern: metric-statistic:value
    # Examples: vmaf-min:95, ssim-med:0.98, psnr-min:45
    QUALITY_TARGET_PATTERN = re.compile(
        r'^(vmaf|ssim|psnr)-(min|med|median|max):(\d+\.?\d*)$',
        re.IGNORECASE
    )

    # Strategy pattern: preset+profile
    # Examples: slow+h265-aq, veryslow+h264-anime
    STRATEGY_PATTERN = re.compile(
        r'^([a-z]+)\+([a-z0-9\-]+)$',
        re.IGNORECASE
    )

    # Valid encoder presets
    VALID_PRESETS = {
        'ultrafast', 'superfast', 'veryfast', 'faster', 'fast',
        'medium', 'slow', 'slower', 'veryslow', 'placebo'
    }

    # Valid metrics
    VALID_METRICS = {'vmaf', 'ssim', 'psnr'}

    # Valid statistics
    VALID_STATISTICS = {'min', 'med', 'median', 'max'}

    def __init__(self, config_manager: Any | None = None):
        """Initialize validator.

        Args:
            config_manager: Optional ConfigManager for profile validation
        """
        self.config_manager = config_manager

    def validate_source_video(self, source_video: Path) -> None:
        """Validate source video exists and is readable.

        Args:
            source_video: Path to source video file

        Raises:
            ValidationError: If source video is invalid
        """
        if not source_video.exists():
            raise ValidationError(f"Source video not found: {source_video}")

        if not source_video.is_file():
            raise ValidationError(f"Source video is not a file: {source_video}")

        if not self._is_readable(source_video):
            raise ValidationError(f"Source video is not readable: {source_video}")

        # Check file size (warn if very small)
        size_mb = source_video.stat().st_size / (1024 * 1024)
        if size_mb < 1:
            logger.warning(f"Source video is very small ({size_mb:.2f} MB)")

    def validate_working_directory(self, work_dir: Path, create: bool = True) -> None:
        """Validate working directory is writable.

        Args:
            work_dir: Path to working directory
            create: Whether to create directory if it doesn't exist

        Raises:
            ValidationError: If working directory is invalid
        """
        if work_dir.exists():
            if not work_dir.is_dir():
                raise ValidationError(f"Working directory path exists but is not a directory: {work_dir}")

            if not self._is_writable(work_dir):
                raise ValidationError(f"Working directory is not writable: {work_dir}")
        else:
            if create:
                try:
                    work_dir.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise ValidationError(f"Cannot create working directory: {work_dir} - {e}")
            else:
                raise ValidationError(f"Working directory does not exist: {work_dir}")

    def validate_external_tools(self) -> None:
        """Validate external tools (ffmpeg, mkvtoolnix) are available.

        Raises:
            ValidationError: If required tools are not available
        """
        # Check ffmpeg
        if not self._check_command('ffmpeg'):
            raise ValidationError(
                "ffmpeg not found. Please install ffmpeg and ensure it's in your PATH.\n"
                "Installation instructions: https://ffmpeg.org/download.html"
            )

        # Check ffprobe
        if not self._check_command('ffprobe'):
            raise ValidationError(
                "ffprobe not found. Please install ffmpeg (includes ffprobe) and ensure it's in your PATH.\n"
                "Installation instructions: https://ffmpeg.org/download.html"
            )

        # Check mkvmerge
        if not self._check_command('mkvmerge'):
            raise ValidationError(
                "mkvmerge not found. Please install MKVToolNix and ensure it's in your PATH.\n"
                "Installation instructions: https://mkvtoolnix.download/"
            )

        # Check mkvextract
        if not self._check_command('mkvextract'):
            raise ValidationError(
                "mkvextract not found. Please install MKVToolNix (includes mkvextract) and ensure it's in your PATH.\n"
                "Installation instructions: https://mkvtoolnix.download/"
            )

        logger.debug("All external tools validated successfully")

    def validate_quality_target(self, target_str: str) -> QualityTarget:
        """Validate and parse quality target string.

        Args:
            target_str: Quality target string (e.g., "vmaf-min:95")

        Returns:
            Parsed QualityTarget

        Raises:
            ValidationError: If quality target format is invalid
        """
        match = self.QUALITY_TARGET_PATTERN.match(target_str.strip())
        if not match:
            raise ValidationError(
                f"Invalid quality target format: '{target_str}'\n"
                f"Expected format: metric-statistic:value\n"
                f"Examples: vmaf-min:95, ssim-med:0.98, psnr-min:45"
            )

        metric, statistic, value_str = match.groups()
        metric = metric.lower()
        statistic = statistic.lower()

        # Normalize 'med' to 'median'
        if statistic == 'med':
            statistic = 'median'

        # Validate metric
        if metric not in self.VALID_METRICS:
            raise ValidationError(
                f"Invalid metric: '{metric}'\n"
                f"Valid metrics: {', '.join(sorted(self.VALID_METRICS))}"
            )

        # Validate statistic
        if statistic not in self.VALID_STATISTICS:
            raise ValidationError(
                f"Invalid statistic: '{statistic}'\n"
                f"Valid statistics: {', '.join(sorted(self.VALID_STATISTICS))}"
            )

        # Parse and validate value
        try:
            value = float(value_str)
        except ValueError:
            raise ValidationError(f"Invalid quality target value: '{value_str}' (must be a number)")

        # Validate value ranges
        if metric == 'ssim':
            if not 0.0 <= value <= 1.0:
                raise ValidationError(
                    f"SSIM value must be between 0.0 and 1.0, got: {value}\n"
                    f"Example: ssim-min:0.98"
                )
        elif metric == 'vmaf':
            if not 0.0 <= value <= 100.0:
                raise ValidationError(
                    f"VMAF value must be between 0 and 100, got: {value}\n"
                    f"Example: vmaf-min:95"
                )
        elif metric == 'psnr':
            if value < 0.0:
                raise ValidationError(
                    f"PSNR value must be positive, got: {value}\n"
                    f"Example: psnr-min:45"
                )
            if value > 100.0:
                logger.warning(f"PSNR value is very high ({value} dB), this may be unrealistic")

        return QualityTarget(metric=metric, statistic=statistic, value=value)

    def validate_quality_targets(self, targets_str: str) -> list[QualityTarget]:
        """Validate and parse multiple quality targets.

        Args:
            targets_str: Comma-separated quality targets (e.g., "vmaf-min:95,ssim-med:0.98")

        Returns:
            List of parsed QualityTarget objects

        Raises:
            ValidationError: If any quality target is invalid
        """
        if not targets_str or not targets_str.strip():
            raise ValidationError("Quality targets cannot be empty")

        targets = []
        for target_str in targets_str.split(','):
            target_str = target_str.strip()
            if target_str:
                targets.append(self.validate_quality_target(target_str))

        if not targets:
            raise ValidationError("At least one quality target must be specified")

        return targets

    def validate_strategy(self, strategy_str: str) -> tuple[str, str]:
        """Validate and parse strategy string.

        Args:
            strategy_str: Strategy string (e.g., "slow+h265-aq")

        Returns:
            Tuple of (preset, profile)

        Raises:
            ValidationError: If strategy format is invalid
        """
        match = self.STRATEGY_PATTERN.match(strategy_str.strip())
        if not match:
            raise ValidationError(
                f"Invalid strategy format: '{strategy_str}'\n"
                f"Expected format: preset+profile\n"
                f"Examples: slow+h265-aq, veryslow+h264-anime"
            )

        preset, profile = match.groups()
        preset = preset.lower()
        profile = profile.lower()

        # Validate preset
        if preset not in self.VALID_PRESETS:
            raise ValidationError(
                f"Invalid preset: '{preset}'\n"
                f"Valid presets: {', '.join(sorted(self.VALID_PRESETS))}"
            )

        # Validate profile (if config manager available)
        if self.config_manager:
            try:
                self.config_manager.get_profile(profile)
            except KeyError:
                available_profiles = self.config_manager.list_profiles()
                raise ValidationError(
                    f"Unknown profile: '{profile}'\n"
                    f"Available profiles: {', '.join(sorted(available_profiles))}"
                )

        return preset, profile

    def validate_strategies(self, strategies_str: str) -> list[tuple[str, str]]:
        """Validate and parse multiple strategies.

        Args:
            strategies_str: Comma-separated strategies (e.g., "slow+h265-aq,veryslow+h265-anime")

        Returns:
            List of (preset, profile) tuples

        Raises:
            ValidationError: If any strategy is invalid
        """
        if not strategies_str or not strategies_str.strip():
            raise ValidationError("Strategies cannot be empty")

        strategies = []
        for strategy_str in strategies_str.split(','):
            strategy_str = strategy_str.strip()
            if strategy_str:
                strategies.append(self.validate_strategy(strategy_str))

        if not strategies:
            raise ValidationError("At least one strategy must be specified")

        return strategies

    def validate_crop_params(self, crop_str: str) -> CropParams:
        """Validate and parse crop parameter string.

        Args:
            crop_str: Crop parameters (e.g., "140 140" or "140 140 0 0")

        Returns:
            Parsed CropParams

        Raises:
            ValidationError: If crop format is invalid
        """
        parts = crop_str.strip().split()

        if len(parts) not in (2, 4):
            raise ValidationError(
                f"Invalid crop format: '{crop_str}'\n"
                f"Expected format: 'top bottom' or 'top bottom left right'\n"
                f"Examples: '140 140' or '140 140 0 0'"
            )

        try:
            values = [int(p) for p in parts]
        except ValueError:
            raise ValidationError(
                f"Invalid crop values: '{crop_str}' (all values must be integers)"
            )

        # Validate non-negative
        if any(v < 0 for v in values):
            raise ValidationError(
                f"Invalid crop values: '{crop_str}' (all values must be non-negative)"
            )

        # Parse based on number of values
        if len(values) == 2:
            return CropParams(top=values[0], bottom=values[1], left=0, right=0)
        else:
            return CropParams(top=values[0], bottom=values[1], left=values[2], right=values[3])

    def validate_regex_pattern(self, pattern: str, pattern_name: str = "pattern") -> None:
        """Validate regex pattern is valid.

        Args:
            pattern: Regex pattern string
            pattern_name: Name of pattern for error messages

        Raises:
            ValidationError: If regex pattern is invalid
        """
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValidationError(
                f"Invalid {pattern_name} regex pattern: '{pattern}'\n"
                f"Error: {e}"
            )

    def validate_max_parallel(self, max_parallel: int) -> None:
        """Validate max parallel processes value.

        Args:
            max_parallel: Maximum parallel processes

        Raises:
            ValidationError: If value is invalid
        """
        if max_parallel < 1:
            raise ValidationError(
                f"Invalid max_parallel value: {max_parallel} (must be at least 1)"
            )

        if max_parallel > 16:
            logger.warning(
                f"max_parallel value is very high ({max_parallel}), "
                f"this may overload your system"
            )

    def validate_log_level(self, log_level: str) -> None:
        """Validate log level value.

        Args:
            log_level: Log level string

        Raises:
            ValidationError: If log level is invalid
        """
        valid_levels = {'debug', 'info', 'warning', 'critical'}
        log_level_lower = log_level.lower()

        if log_level_lower not in valid_levels:
            raise ValidationError(
                f"Invalid log level: '{log_level}'\n"
                f"Valid levels: {', '.join(sorted(valid_levels))}"
            )

    @staticmethod
    def _is_readable(path: Path) -> bool:
        """Check if path is readable.

        Args:
            path: Path to check

        Returns:
            True if readable, False otherwise
        """
        try:
            with open(path, 'rb') as f:
                f.read(1)
            return True
        except (OSError, PermissionError):
            return False

    @staticmethod
    def _is_writable(path: Path) -> bool:
        """Check if directory is writable.

        Args:
            path: Directory path to check

        Returns:
            True if writable, False otherwise
        """
        try:
            test_file = path / '.write_test'
            test_file.touch()
            test_file.unlink()
            return True
        except (OSError, PermissionError):
            return False

    @staticmethod
    def _check_command(command: str) -> bool:
        """Check if command is available in PATH.

        Args:
            command: Command name to check

        Returns:
            True if command is available, False otherwise
        """
        return shutil.which(command) is not None


def validate_all(
    source_video: Path,
    work_dir: Path,
    quality_targets_str: str,
    strategies_str: str,
    config_manager: Any | None = None,
    crop_str: str | None = None,
    video_filter: str | None = None,
    audio_filter: str | None = None,
    max_parallel: int = 2,
    log_level: str = "info"
) -> tuple[list[QualityTarget], list[tuple[str, str]], CropParams | None]:
    """Validate all pipeline inputs.

    Args:
        source_video: Path to source video
        work_dir: Working directory path
        quality_targets_str: Quality targets string
        strategies_str: Strategies string
        config_manager: Optional ConfigManager for profile validation
        crop_str: Optional crop parameters string
        video_filter: Optional video filter regex
        audio_filter: Optional audio filter regex
        max_parallel: Maximum parallel processes
        log_level: Log level

    Returns:
        Tuple of (quality_targets, strategies, crop_params)

    Raises:
        ValidationError: If any validation fails
    """
    validator = Validator(config_manager)

    # Validate external tools first
    validator.validate_external_tools()

    # Validate paths
    validator.validate_source_video(source_video)
    validator.validate_working_directory(work_dir, create=True)

    # Validate quality targets and strategies
    quality_targets = validator.validate_quality_targets(quality_targets_str)
    strategies = validator.validate_strategies(strategies_str)

    # Validate optional parameters
    crop_params = None
    if crop_str:
        crop_params = validator.validate_crop_params(crop_str)

    if video_filter:
        validator.validate_regex_pattern(video_filter, "video filter")

    if audio_filter:
        validator.validate_regex_pattern(audio_filter, "audio filter")

    validator.validate_max_parallel(max_parallel)
    validator.validate_log_level(log_level)

    return quality_targets, strategies, crop_params
