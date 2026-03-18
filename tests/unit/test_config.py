"""Unit tests for configuration management."""

from pathlib import Path

import pytest
from pyqenc.config import ConfigManager, EncodingProfile

from tests.fixtures.config_fixtures import create_invalid_config, create_test_config


class TestConfigManager:
    """Tests for ConfigManager loading and validation."""

    def test_load_default_config(self):
        """Test loading built-in default configuration."""
        config = ConfigManager()

        # Check codecs loaded
        assert "h264-8bit" in config.list_codecs()
        assert "h265-10bit" in config.list_codecs()

        # Check profiles loaded
        profiles = config.list_profiles()
        assert "h264" in profiles
        assert "h265" in profiles
        assert "h265-aq" in profiles
        assert "h265-anime" in profiles

    def test_load_custom_config(self, tmp_path):
        """Test loading custom configuration file."""
        config_path = create_test_config(tmp_path)
        config = ConfigManager(config_path)

        # Verify loaded
        assert "h264-8bit" in config.list_codecs()
        assert "h265-10bit" in config.list_codecs()

    def test_get_default_strategies(self):
        """Test retrieving default strategies from configuration."""
        config = ConfigManager()

        strategies = config.get_default_strategies()
        assert isinstance(strategies, list)
        assert len(strategies) > 0
        assert "veryslow+h264*" in strategies
        assert "slow+h265*" in strategies

    def test_get_codec(self):
        """Test retrieving codec configuration."""
        config = ConfigManager()

        codec = config.get_codec("h265-10bit")
        assert codec.name == "h265-10bit"
        assert codec.encoder == "libx265"
        assert codec.pixel_format == "yuv420p10le"
        assert codec.default_crf == 20.0
        assert codec.crf_range == (0.0, 51.0)
        assert len(codec.presets) > 0
        assert "slow" in codec.presets
        assert "veryslow" in codec.presets

    def test_get_codec_not_found(self):
        """Test retrieving non-existent codec raises ValueError."""
        config = ConfigManager()

        with pytest.raises(ValueError, match="Unknown codec"):
            config.get_codec("nonexistent")

    def test_get_profile(self):
        """Test retrieving profile configuration."""
        config = ConfigManager()

        profile = config.get_profile("h265-aq")
        assert profile.name == "h265-aq"
        assert profile.codec == "h265-10bit"
        assert "-x265-params" in profile.extra_args

    def test_get_profile_not_found(self):
        """Test retrieving non-existent profile raises ValueError."""
        config = ConfigManager()

        with pytest.raises(ValueError, match="Unknown profile"):
            config.get_profile("nonexistent")

    def test_list_presets(self):
        """Test listing presets for specific codec."""
        config = ConfigManager()

        h264_presets = config.list_presets("h264-8bit")
        assert "slow" in h264_presets
        assert "veryslow" in h264_presets
        assert "medium" in h264_presets

        h265_presets = config.list_presets("h265-10bit")
        assert "slow" in h265_presets
        assert "veryslow" in h265_presets

    def test_list_profiles_filtered(self):
        """Test listing profiles filtered by codec."""
        config = ConfigManager()

        h264_profiles = config.list_profiles(codec="h264-8bit")
        assert "h264" in h264_profiles
        assert "h265" not in h264_profiles

        h265_profiles = config.list_profiles(codec="h265-10bit")
        assert "h265" in h265_profiles
        assert "h265-aq" in h265_profiles
        assert "h265-anime" in h265_profiles
        assert "h264" not in h265_profiles

    def test_parse_strategy_specific(self):
        """Test parsing specific preset+profile strategy."""
        config = ConfigManager()

        strategies = config.parse_strategy("slow+h265-aq")
        assert len(strategies) == 1
        strategy = strategies[0]
        assert strategy.preset == "slow"
        assert strategy.profile == "h265-aq"
        assert strategy.codec.name == "h265-10bit"
        assert "-x265-params" in strategy.profile_args

    def test_parse_strategy_preset_with_wildcard(self):
        """Test parsing preset with profile wildcard (e.g., 'slow+h265*')."""
        config = ConfigManager()

        strategies = config.parse_strategy("slow+h265*")
        assert len(strategies) == 3  # h265, h265-aq, h265-anime

        profile_names = {s.profile for s in strategies}
        assert "h265" in profile_names
        assert "h265-aq" in profile_names
        assert "h265-anime" in profile_names

        # All should use slow preset
        for strategy in strategies:
            assert strategy.preset == "slow"
            assert strategy.codec.name == "h265-10bit"

    def test_parse_strategy_preset_only(self):
        """Test parsing preset only (e.g., 'slow')."""
        config = ConfigManager()

        strategies = config.parse_strategy("slow")
        assert len(strategies) > 0

        # Should include profiles from both codecs that support 'slow'
        profile_names = {s.profile for s in strategies}
        assert "h264" in profile_names or "h264-aq" in profile_names
        assert "h265" in profile_names or "h265-aq" in profile_names

        # All should use slow preset
        for strategy in strategies:
            assert strategy.preset == "slow"

    def test_parse_strategy_profile_wildcard_only(self):
        """Test parsing profile wildcard only (e.g., '+h265*')."""
        config = ConfigManager()

        strategies = config.parse_strategy("+h265*")
        assert len(strategies) > 0

        # Should include all presets with h265 profiles
        profile_names = {s.profile for s in strategies}
        assert "h265" in profile_names
        assert "h265-aq" in profile_names
        assert "h265-anime" in profile_names

        # Should have multiple presets
        presets = {s.preset for s in strategies}
        assert len(presets) > 1
        assert "slow" in presets
        assert "veryslow" in presets

    def test_parse_strategy_profile_only(self):
        """Test parsing specific profile only (e.g., '+h265-aq')."""
        config = ConfigManager()

        strategies = config.parse_strategy("+h265-aq")
        assert len(strategies) > 0

        # All should use h265-aq profile
        for strategy in strategies:
            assert strategy.profile == "h265-aq"
            assert strategy.codec.name == "h265-10bit"

        # Should have multiple presets
        presets = {s.preset for s in strategies}
        assert len(presets) > 1
        assert "slow" in presets
        assert "veryslow" in presets

    def test_parse_strategy_empty_string(self):
        """Test parsing empty string (all combinations)."""
        config = ConfigManager()

        strategies = config.parse_strategy("")
        assert len(strategies) > 0

        # Should include all profiles
        profile_names = {s.profile for s in strategies}
        assert "h264" in profile_names
        assert "h265" in profile_names
        assert "h265-aq" in profile_names

        # Should include all presets
        presets = {s.preset for s in strategies}
        assert "slow" in presets
        assert "veryslow" in presets
        assert "medium" in presets

    def test_parse_strategy_invalid_preset(self):
        """Test parsing strategy with invalid preset raises ValueError."""
        config = ConfigManager()

        with pytest.raises(ValueError, match="not supported"):
            config.parse_strategy("invalid+h265-aq")

    def test_parse_strategy_invalid_profile(self):
        """Test parsing strategy with invalid profile raises ValueError."""
        config = ConfigManager()

        with pytest.raises(ValueError, match="Unknown profile"):
            config.parse_strategy("slow+invalid")

    def test_validate_strategy(self):
        """Test strategy validation."""
        config = ConfigManager()

        assert config.validate_strategy("slow+h265-aq")
        assert config.validate_strategy("slow+h265*")
        assert config.validate_strategy("slow")
        assert config.validate_strategy("+h265*")
        assert config.validate_strategy("")
        assert not config.validate_strategy("invalid+h265-aq")
        assert not config.validate_strategy("slow+invalid")

    def test_expand_strategies_with_none(self):
        """Test expanding strategies with None uses defaults."""
        config = ConfigManager()

        strategies = config.expand_strategies(None)
        assert len(strategies) > 0

        # Should use default strategies from config
        # Default is ["veryslow+h264*", "slow+h265*"]
        profile_names = {s.profile for s in strategies}
        assert any("h264" in p for p in profile_names)
        assert any("h265" in p for p in profile_names)

    def test_expand_strategies_with_list(self):
        """Test expanding list of strategy patterns."""
        config = ConfigManager()

        strategies = config.expand_strategies(["slow+h265-aq", "veryslow+h264"])
        assert len(strategies) == 2

        # Check first strategy
        assert strategies[0].preset == "slow"
        assert strategies[0].profile == "h265-aq"

        # Check second strategy
        assert strategies[1].preset == "veryslow"
        assert strategies[1].profile == "h264"

    def test_expand_strategies_removes_duplicates(self):
        """Test that duplicate strategies are removed."""
        config = ConfigManager()

        # Specify same strategy twice
        strategies = config.expand_strategies(["slow+h265-aq", "slow+h265-aq"])
        assert len(strategies) == 1
        assert strategies[0].preset == "slow"
        assert strategies[0].profile == "h265-aq"
