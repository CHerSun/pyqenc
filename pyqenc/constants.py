"""Module-level constants for pyqenc."""

TIMEOUT_SECONDS_SHORT = 10
"""Short timeout for quick operations"""
TIMEOUT_SECONDS_LONG = 300
"""Longer timeout for potentially slow operations"""
TIMEOUT_SECONDS_MAX = 3600
"""Maximum timeout for very long operations. To have at least some form of fallback."""

THRESHOLD_ATTEMPTS_WARNING = 10
"""Threshold for warning about excessive encoding attempts."""
PROGRESS_CHUNK_UNIT = " chunks"
"""Unit for progress bar when tracking chunk processing."""

TEMP_SUFFIX = ".tmp"
"""A suffix to append to temporary files during processing. This helps avoid confusion with final output files and allows for easy cleanup of incomplete files."""

# Disk space estimation constants
OVERHEAD_EXTRACTION_AND_AUDIO = 1.2
"""Multiplier for the source video size to account for overhead from extraction and audio processing. This includes temporary files created during these steps, which can be larger than the original source video stream due to formats used for intermediate processing."""
OVERHEAD_CHUNKING_LOSSLESS = 5.0
"""Multiplier for the source video size to account for overhead from lossless chunking using FFV1 all-intra. This format can be significantly larger than the original source video stream, especially for high-motion content, due to the nature of lossless compression."""
OVERHEAD_CHUNKING_REMUX    = 1.0
"""Multiplier for the source video size to account for overhead from remuxing (stream-copying). This is typically close to the original source video size."""
OVERHEAD_FOR_OPTIMIZATION = 0.5
"""Additional multiplier to account for overhead from the optimization phase, which includes multiple encoding attempts, metrics calculations, and final output. This is an estimate and can vary widely based on the number of attempts and strategies used."""
OVERHEAD_PER_STRATEGY = 2.5
"""Additional multiplier per encoding strategy to account for overhead from multiple attempts, metrics, and final output. This is an estimate and can vary widely based on the number of attempts and strategies used."""
OVERHEAD_TIGHT_MARGIN = 1.5
"""When the estimated required space is within this multiplier of the available space, a warning should be issued about tight disk space. This helps alert users to potential issues before they occur, allowing them to free up space or adjust their strategy before running out of disk space during processing."""

# Vertical delimiters
LINE_WIDTH  = 72
"""Horizontal line width for log blocks."""
THIN_LINE   = "─" * LINE_WIDTH
"""Think horizontal line to separate large blocks (phases) in logs."""
THICK_LINE  = "═" * LINE_WIDTH
"""Thick horizontal line to separate large blocks (phases) in logs."""

# Padding control
PADDING_FRAME_NUMBER = 6
"""Padding for frame numbers in chunk filenames for consistent sorting and readability. For example, with a padding of 6, frame 42 would be represented as '000042' in filenames."""
PADDING_CRF = "4.1f"
"""Padding for CRF values in log messages for consistent formatting."""

# CRF optimization controls
CRF_GRANULARITY = 0.5 # 0.5 might be too coarse; consider 0.2 or even 0.1 for finer search. Modifying this could require PADDING_CRF adjustment for log formatting.
"""Granularity for CRF adjustments during optimization. This determines the step size when adjusting CRF values to find the optimal quality/size balance."""

# Symbols for log messages
SUCCESS_SYMBOL_MINOR = "✔"
"""Symbol to indicate successful completion of a minor step, such as an individual chunk attempt."""
SUCCESS_SYMBOL_MAJOR = "✅"
"""Symbol to indicate successful completion of a major step, such as an entire strategy or optimization phase."""
FAILURE_SYMBOL_MINOR = "✘"
"""Symbol to indicate failure of a minor step, such as an individual chunk attempt."""
FAILURE_SYMBOL_MAJOR = "❌"
"""Symbol to indicate failure of a major step, such as an entire strategy or optimization phase."""
WARNING_SYMBOL = "⚠"
"""Symbol to indicate a warning condition, such as excessive encoding attempts or potential issues with disk space."""
RANGE_SEPARATOR = "-"
"""Separator used in filename patterns to indicate ranges, such as frame ranges or chunk ranges."""
TIME_SEPARATOR_SAFE = "꞉"
"""A visually similar but filesystem-safe separator for time components in filenames, replacing the standard colon (:) which can cause issues on some filesystems."""
TIME_SEPARATOR_MS = "․"
"""Separator for milliseconds in time representations, visually similar to the standard dot (.), but different symbol to avoid parsing collisions."""
BRACKET_LEFT = "｟"
"""Left bracket symbol for visually distinct log formatting."""
BRACKET_RIGHT = " ｠"
"""Right bracket symbol for visually distinct log formatting."""
