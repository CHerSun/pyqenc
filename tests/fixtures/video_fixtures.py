"""Video file fixtures for testing."""

import os
from pathlib import Path

from pyqenc.utils.long_path import LongPath

# Sample videos are located in the samples/ directory
SAMPLE_VIDEOS_DIR = Path("samples")
SAMPLE_E2E_VIDEO = SAMPLE_VIDEOS_DIR / "sample-e2e.mkv"
SAMPLE_SUNRISE_VIDEO = SAMPLE_VIDEOS_DIR / "sample-sunrise-4k.mkv"

REAL_SAMPLE_ENV = "PYQENC_REAL_SAMPLE"
"""Environment variable naming a real, uncut video (any MKV you own) for
tests that need clean real content. The tests depending on it skip while it
is unset or the file is missing."""


def get_sample_video_path() -> Path:
    """Get path to a sample video file for testing.

    Returns:
        Path to the e2e sample video (see
        ``tests/fixtures/build_e2e_sample.py`` for how it is built).
    """
    return SAMPLE_E2E_VIDEO


def get_4k_sample_video_path() -> Path:
    """Get path to a 4K sample video file for testing.

    Returns:
        Path to 4K sample video (sunrise)
    """
    return SAMPLE_SUNRISE_VIDEO


def sample_video_exists() -> bool:
    """Check if sample video files exist.

    Returns:
        True if at least one sample video exists
    """
    return SAMPLE_E2E_VIDEO.exists() or SAMPLE_SUNRISE_VIDEO.exists()


def get_real_sample_path() -> Path:
    """Get path to the real sample video named by ``PYQENC_REAL_SAMPLE``.

    Returns:
        Path to the real sample video.
    """
    return LongPath(os.environ[REAL_SAMPLE_ENV])


def real_sample_exists() -> bool:
    """Check if the real sample video is configured and present.

    Returns:
        True if ``PYQENC_REAL_SAMPLE`` names an existing file
    """
    value = os.environ.get(REAL_SAMPLE_ENV)
    return bool(value) and Path(value).exists()
