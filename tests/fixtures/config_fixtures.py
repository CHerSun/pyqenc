"""Configuration file fixtures for testing."""

from pathlib import Path
from typing import Any

# Sample configuration YAML content
SAMPLE_CONFIG_YAML = """
default_strategies:
  - "veryslow+h264*"
  - "slow+h265*"

codecs:
  h264-8bit:
    encoder: libx264
    pixel_format: yuv420p
    default_crf: 23
    crf_range: [0, 51]
    presets:
      - ultrafast
      - superfast
      - veryfast
      - faster
      - fast
      - medium
      - slow
      - slower
      - veryslow
      - placebo
    
  h265-10bit:
    encoder: libx265
    pixel_format: yuv420p10le
    default_crf: 20
    crf_range: [0, 51]
    presets:
      - ultrafast
      - superfast
      - veryfast
      - faster
      - fast
      - medium
      - slow
      - slower
      - veryslow
      - placebo

profiles:
  h264:
    codec: h264-8bit
    description: "Default h.264 8-bit encoding"
    extra_args: []
  
  h264-aq:
    codec: h264-8bit
    description: "h.264 with adaptive quantization"
    extra_args:
      - "-x264-params"
      - "aq-mode=3:aq-strength=0.8"
  
  h265:
    codec: h265-10bit
    description: "Default h.265 10-bit encoding"
    extra_args: []
  
  h265-aq:
    codec: h265-10bit
    description: "h.265 with adaptive quantization"
    extra_args:
      - "-x265-params"
      - "aq-mode=3:aq-strength=0.8"
  
  h265-anime:
    codec: h265-10bit
    description: "h.265 optimized for anime"
    extra_args:
      - "-x265-params"
      - "aq-mode=2:psy-rd=1.0:deblock=-1,-1"
"""

# Invalid configuration (missing required fields)
INVALID_CONFIG_YAML = """
codecs:
  h264-8bit:
    encoder: libx264
    # Missing pixel_format

profiles:
  h264:
    # Missing codec reference
    description: "Default h.264"
    extra_args: []
"""


def create_test_config(tmp_path: Path) -> Path:
    """Create a test configuration file.
    
    Args:
        tmp_path: Temporary directory path
        
    Returns:
        Path to created config file
    """
    config_path = tmp_path / "test_config.yaml"
    config_path.write_text(SAMPLE_CONFIG_YAML)
    return config_path


def create_invalid_config(tmp_path: Path) -> Path:
    """Create an invalid configuration file for testing validation.
    
    Args:
        tmp_path: Temporary directory path
        
    Returns:
        Path to created invalid config file
    """
    config_path = tmp_path / "invalid_config.yaml"
    config_path.write_text(INVALID_CONFIG_YAML)
    return config_path
