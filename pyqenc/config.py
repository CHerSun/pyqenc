"""
Configuration management for the encoding pipeline.

This module handles loading, validating, and providing access to encoding
profiles and codec configurations.
"""

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pyqenc.models import CodecConfig, StrategyConfig


@dataclass
class AudioConversionProfile:
    """Codec/bitrate/extension profile for the final audio delivery conversion step.

    Attributes:
        codec:     ffmpeg audio codec name (e.g. ``"aac"``).
        bitrate:   Target bitrate string (e.g. ``"192k"``).
        extension: Output file extension including the leading dot (e.g. ``".aac"``).
    """

    codec:     str
    bitrate:   str
    extension: str


@dataclass
class AudioOutputConfig:
    """Configuration for the audio output / conversion phase.

    Attributes:
        convert_filter: Regex string; processed audio files whose name matches
                        are passed to the ``ConversionStrategy`` finalizer.
        profiles:       Map of channel-layout key (e.g. ``"2.0"``) to
                        :class:`AudioConversionProfile`.
    """

    convert_filter: str
    profiles:       dict[str, AudioConversionProfile]


@dataclass
class StreamFilterConfig:
    """Include/exclude regex filters applied to all stream types during extraction.

    Attributes:
        include: Regex string selecting streams by would-be output filename.
                 ``None`` means include all streams.
        exclude: Regex string rejecting streams by would-be output filename.
                 ``None`` means exclude no streams.  Exclusion takes precedence.
    """

    include: str | None
    exclude: str | None


@dataclass
class EncodingProfile:
    """Encoding profile definition.

    Attributes:
        name: Profile name
        codec: Codec name this profile uses
        description: Human-readable description
        extra_args: Additional FFmpeg arguments
    """

    name: str
    codec: str
    description: str
    extra_args: list[str]


class ConfigManager:
    """Manager for encoding configuration.

    Handles loading codec and profile definitions from YAML configuration,
    with fallback to built-in defaults.
    """

    def __init__(self, config_path: Path | None = None):
        """Initialize configuration manager.

        Args:
            config_path: Optional path to custom configuration file.
                        If None, searches default locations and falls back to built-in config.
        """
        self._config: dict[str, Any] = {}
        self._codecs: dict[str, CodecConfig] = {}
        self._profiles: dict[str, EncodingProfile] = {}
        self._default_strategies: list[str] = []

        self._load_config(config_path)
        self._parse_config()

    def _load_config(self, config_path: Path | None) -> None:
        """Load configuration from file or use defaults.

        Args:
            config_path: Optional path to configuration file
        """
        if config_path and config_path.exists():
            # Load from specified path
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
            return

        # Search default locations
        search_paths = [
            Path.cwd() / "pyqenc.yaml",
            Path.home() / ".config" / "pyqenc" / "config.yaml",
        ]

        for path in search_paths:
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f)
                return

        # Fall back to built-in default_config.yaml
        default_config_path = Path(__file__).parent / "default_config.yaml"
        if default_config_path.exists():
            with open(default_config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
        else:
            raise FileNotFoundError(
                f"Could not find default configuration at {default_config_path}. "
                f"Searched paths: {search_paths}"
            )

    def _parse_config(self) -> None:
        """Parse loaded configuration into structured objects."""
        # Parse codecs
        codecs_data = self._config.get("codecs", {})
        for name, codec_data in codecs_data.items():
            self._codecs[name] = CodecConfig(
                name=name,
                encoder=codec_data["encoder"],
                pixel_format=codec_data["pixel_format"],
                default_crf=codec_data["default_crf"],
                crf_range=tuple(codec_data["crf_range"]),
                presets=codec_data.get("presets", [])
            )

        # Parse profiles
        profiles_data = self._config.get("profiles", {})
        for name, profile_data in profiles_data.items():
            self._profiles[name] = EncodingProfile(
                name=name,
                codec=profile_data["codec"],
                description=profile_data.get("description", ""),
                extra_args=profile_data.get("extra_args", [])
            )

        # Parse default strategies
        self._default_strategies = self._config.get("default_strategies", [])

    def get_default_strategies(self) -> list[str]:
        """Get default strategy patterns from configuration.

        Returns:
            List of default strategy patterns (e.g., ["veryslow+h264*", "slow+h265*"])
        """
        return self._default_strategies.copy()

    def get_codec(self, name: str) -> CodecConfig:
        """Retrieve codec configuration by name.

        Args:
            name: Codec name (e.g., 'h264-8bit', 'h265-10bit')

        Returns:
            CodecConfig instance

        Raises:
            ValueError: If codec not found
        """
        if name not in self._codecs:
            raise ValueError(
                f"Unknown codec '{name}'. Available codecs: {list(self._codecs.keys())}"
            )
        return self._codecs[name]

    def get_profile(self, name: str) -> EncodingProfile:
        """Retrieve profile by name.

        Args:
            name: Profile name (e.g., 'h265-aq', 'h264-anime')

        Returns:
            EncodingProfile instance

        Raises:
            ValueError: If profile not found
        """
        if name not in self._profiles:
            raise ValueError(
                f"Unknown profile '{name}'. Available profiles: {list(self._profiles.keys())}"
            )
        return self._profiles[name]

    def list_codecs(self) -> list[str]:
        """List all available codec names.

        Returns:
            List of codec names
        """
        return list(self._codecs.keys())

    def list_profiles(self, codec: str | None = None) -> list[str]:
        """List all available profile names.

        Args:
            codec: Optional codec name to filter profiles

        Returns:
            List of profile names
        """
        if codec is None:
            return list(self._profiles.keys())

        return [
            name for name, profile in self._profiles.items()
            if profile.codec == codec
        ]

    def list_presets(self, codec: str) -> list[str]:
        """List presets supported by specific codec.

        Args:
            codec: Codec name (e.g., 'h264-8bit', 'h265-10bit')

        Returns:
            List of preset names supported by this codec

        Raises:
            ValueError: If codec not found
        """
        codec_config = self.get_codec(codec)
        return codec_config.presets.copy()

    def validate_strategy(self, strategy: str) -> bool:
        """Validate strategy string format.

        Args:
            strategy: Strategy string (e.g., 'slow+h265-aq', 'slow+h265*', 'slow', '+h265*', '')

        Returns:
            True if valid, False otherwise
        """
        try:
            self.parse_strategy(strategy)
            return True
        except ValueError:
            return False

    def parse_strategy(self, strategy: str) -> list[StrategyConfig]:
        """Parse strategy string into list of encoder configurations.

        Supports flexible specifications:
        - "slow+h265-aq": specific preset+profile
        - "slow+h265*": preset with profile wildcard (all h265 profiles)
        - "slow": all profiles with slow preset (validated per codec)
        - "+h265*": all presets with h265 profiles (wildcard, from h265-10bit codec)
        - "+h265-aq": all presets with specific profile (from h265-10bit codec)
        - "": all preset+profile combinations (all codecs, all presets per codec)

        Args:
            strategy: Strategy string

        Returns:
            List of StrategyConfig instances

        Raises:
            ValueError: If strategy format is invalid or components not found
        """
        # Handle empty string - all combinations
        if strategy == "":
            return self._expand_all_combinations()

        # Parse strategy format
        if "+" in strategy:
            preset_part, profile_part = strategy.split("+", 1)
        else:
            # No '+' means preset only (all profiles with this preset)
            preset_part = strategy
            profile_part = "*"

        # Handle preset-only case (e.g., "slow")
        if preset_part and not profile_part:
            profile_part = "*"

        # Handle profile-only case (e.g., "+h265*" or "+h265-aq")
        if not preset_part and profile_part:
            return self._expand_profile_pattern(None, profile_part)

        # Handle preset+profile case
        return self._expand_preset_profile(preset_part, profile_part)

    def _expand_all_combinations(self) -> list[StrategyConfig]:
        """Expand empty strategy to all preset+profile combinations.

        Returns:
            List of all possible StrategyConfig instances
        """
        configs = []

        for profile_name, profile in self._profiles.items():
            codec = self.get_codec(profile.codec)
            for preset in codec.presets:
                configs.append(StrategyConfig(
                    preset=preset,
                    profile=profile_name,
                    codec=codec,
                    profile_args=profile.extra_args
                ))

        return configs

    def _expand_profile_pattern(
        self,
        preset: str | None,
        profile_pattern: str
    ) -> list[StrategyConfig]:
        """Expand profile pattern with optional preset.

        Args:
            preset: Preset name or None for all presets
            profile_pattern: Profile pattern (may contain wildcards)

        Returns:
            List of StrategyConfig instances
        """
        # Find matching profiles
        matching_profiles = []
        if "*" in profile_pattern:
            # Wildcard matching
            for profile_name in self._profiles.keys():
                if fnmatch.fnmatch(profile_name, profile_pattern):
                    matching_profiles.append(profile_name)
        else:
            # Exact match
            if profile_pattern in self._profiles:
                matching_profiles.append(profile_pattern)
            else:
                raise ValueError(
                    f"Unknown profile '{profile_pattern}'. "
                    f"Available profiles: {list(self._profiles.keys())}"
                )

        if not matching_profiles:
            raise ValueError(
                f"No profiles match pattern '{profile_pattern}'. "
                f"Available profiles: {list(self._profiles.keys())}"
            )

        # Generate configs
        configs = []
        for profile_name in matching_profiles:
            profile = self._profiles[profile_name]
            codec = self.get_codec(profile.codec)

            if preset is None:
                # All presets for this profile's codec
                for p in codec.presets:
                    configs.append(StrategyConfig(
                        preset=p,
                        profile=profile_name,
                        codec=codec,
                        profile_args=profile.extra_args
                    ))
            else:
                # Validate preset is supported by this codec
                if preset not in codec.presets:
                    raise ValueError(
                        f"Preset '{preset}' not supported by codec '{codec.name}'. "
                        f"Supported presets: {codec.presets}"
                    )
                configs.append(StrategyConfig(
                    preset=preset,
                    profile=profile_name,
                    codec=codec,
                    profile_args=profile.extra_args
                ))

        return configs

    def _expand_preset_profile(
        self,
        preset: str,
        profile_pattern: str
    ) -> list[StrategyConfig]:
        """Expand preset+profile pattern.

        Args:
            preset: Preset name
            profile_pattern: Profile pattern (may contain wildcards or be '*' for all)

        Returns:
            List of StrategyConfig instances
        """
        # Handle wildcard for all profiles
        if profile_pattern == "*":
            configs = []
            for profile_name, profile in self._profiles.items():
                codec = self.get_codec(profile.codec)
                # Validate preset is supported by this codec
                if preset in codec.presets:
                    configs.append(StrategyConfig(
                        preset=preset,
                        profile=profile_name,
                        codec=codec,
                        profile_args=profile.extra_args
                    ))

            if not configs:
                raise ValueError(
                    f"Preset '{preset}' is not supported by any codec. "
                    f"Available codecs: {list(self._codecs.keys())}"
                )

            return configs

        # Use profile pattern expansion with specific preset
        return self._expand_profile_pattern(preset, profile_pattern)

    def expand_strategies(self, strategies: list[str] | None) -> list[StrategyConfig]:
        """Expand strategy specifications into full list of configurations.

        Args:
            strategies: List of strategy patterns, or None for default from config

        Returns:
            List of fully resolved StrategyConfig objects
        """
        # Use default strategies if none provided
        if strategies is None:
            strategies = self.get_default_strategies()

        # Expand each strategy pattern
        all_configs = []
        for strategy in strategies:
            configs = self.parse_strategy(strategy)
            all_configs.extend(configs)

        # Remove duplicates while preserving order
        seen = set()
        unique_configs = []
        for config in all_configs:
            key = (config.preset, config.profile)
            if key not in seen:
                seen.add(key)
                unique_configs.append(config)

        return unique_configs

    def get_audio_output_config(self) -> AudioOutputConfig:
        """Parse and return the ``audio_output`` configuration section.

        Returns:
            :class:`AudioOutputConfig` populated from the loaded config.

        Raises:
            KeyError: If the ``audio_output`` section or required keys are missing.
        """
        section: dict[str, Any] = self._config["audio_output"]
        raw_profiles: dict[str, Any] = section["profiles"]
        profiles = {
            layout: AudioConversionProfile(
                codec     = p["codec"],
                bitrate   = p["bitrate"],
                extension = p["extension"],
            )
            for layout, p in raw_profiles.items()
        }
        return AudioOutputConfig(
            convert_filter = section["convert_filter"],
            profiles       = profiles,
        )

    def get_metrics_sampling(self) -> int:
        """Return the metrics sampling factor from config.

        Reads ``config["metrics"]["sampling"]`` with a fallback of ``10``
        for backward compatibility with user configs that predate this setting.

        Returns:
            Metrics sampling factor (minimum 1, default 10).
        """
        return int(self._config.get("metrics", {}).get("sampling", 10))

    def get_stream_filter(self) -> StreamFilterConfig:
        """Parse and return the ``streams`` filtering configuration section.

        Returns:
            :class:`StreamFilterConfig` populated from the loaded config.
            Both ``include`` and ``exclude`` default to ``None`` when absent.
        """
        section: dict[str, Any] = self._config.get("streams", {})
        return StreamFilterConfig(
            include = section.get("include"),
            exclude = section.get("exclude"),
        )
