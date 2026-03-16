"""Video file fixtures for testing."""

from pathlib import Path

# Sample videos are located in the samples/ directory
SAMPLE_VIDEOS_DIR = Path("samples")
SAMPLE_LION_VIDEO = SAMPLE_VIDEOS_DIR / "sample-lion-fullhd.mkv"
SAMPLE_SUNRISE_VIDEO = SAMPLE_VIDEOS_DIR / "sample-sunrise-4k.mkv"


def get_sample_video_path() -> Path:
    """Get path to a sample video file for testing.
    
    Returns:
        Path to sample video (lion fullhd)
    """
    return SAMPLE_LION_VIDEO


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
    return SAMPLE_LION_VIDEO.exists() or SAMPLE_SUNRISE_VIDEO.exists()
