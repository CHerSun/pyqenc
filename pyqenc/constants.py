"""Module-level constants for pyqenc."""
# CHerSun 2026

import re

THRESHOLD_ATTEMPTS_WARNING = 10
"""Threshold for warning about excessive encoding attempts."""

TEMP_SUFFIX = ".tmp"
"""A suffix to append to temporary files during processing. This helps avoid confusion with final output files and allows for easy cleanup of incomplete files."""

FINGERPRINT_DIGEST_SIZE_BYTES = 16
"""blake2b digest size (bytes) for every fingerprint token — 128-bit opaque identity."""

# Disk space estimation constants
OVERHEAD_TIGHT_MARGIN = 1.2
"""Multiplier applied to the max estimated space to derive the recommended space threshold.
A 20% buffer on top of the upper-bound estimate."""

# Pixel-based space estimation constants
BITS_PER_PIXEL_ENCODED = 0.10
"""Estimated bits per pixel for encoded video output (attempts, final). Covers a wide range of
content at typical quality targets. Tune this constant if estimates are consistently off."""
AVG_ATTEMPTS_PER_CHUNK = 4.1
"""Average number of CRF search attempts per chunk per strategy. Used to estimate space consumed
by intermediate attempt files during the encoding phase. Value was taken from metrics measured."""

# Fallback source-size multiplier (used only when stream pixel data is unavailable)
OVERHEAD_PER_STRATEGY_FALLBACK = 2.5
"""Fallback multiplier per encoding strategy relative to source size.
Used only when pixel-based estimation is not possible (no VideoStreamInfo available)."""

# Vertical delimiters
LINE_WIDTH  = 72
"""Horizontal line width for log blocks."""
THICK_LINE  = "═" * LINE_WIDTH
"""Thick horizontal line to separate large blocks (phases) in logs."""

METRIC_LOG_DECIMAL_PLACES = 1
"""Decimal places used when formatting metric values in log messages. Values are always truncated (floored), never rounded, to prevent a miss from displaying as a pass due to rounding."""

# Symbols for log messages
SUCCESS_SYMBOL_MINOR = "✔"
"""Symbol to indicate successful completion of a minor step, such as an individual chunk attempt."""
FAILURE_SYMBOL_MINOR = "✘"
"""Symbol to indicate failure of a minor step, such as an individual chunk attempt."""
SUCCESS_SYMBOL_MAJOR = "✅"
"""Symbol to indicate successful completion of a major step, such as an entire strategy or optimization phase."""
FAILURE_SYMBOL_MAJOR = "❌"
"""Symbol to indicate failure of a major step, such as an entire strategy or optimization phase."""
SKIPPED_SYMBOL = "⏭"
"""Symbol to indicate a skipped item (reused artifact) in progress bar text."""
WARNING_SYMBOL = "⚠"
"""Symbol to indicate a warning condition, such as excessive encoding attempts or potential issues with disk space."""
NEUTRAL_INDICATOR_SYMBOL = "•"
"""Symbol to mark the metric with the least surplus on a passing attempt — the bottleneck constraining CRF search."""
APPROXIMATE_INDICATOR_SYMBOL = "≈"
"""Symbol to mark a soft miss against an approximate bar — a fixed-run acceptance below the auto-elected anchor ruler: a matter of fact, not a failure (distinct from per-attempt ✘ misses and exhausted-search ❌)."""
RANGE_SEPARATOR = "-"
"""Separator used in filename patterns to indicate ranges, such as frame ranges or chunk ranges."""
TIME_SEPARATOR = ":"
"""Natural time-component separator for display names; replaced by TIME_SEPARATOR_SAFE on disk."""
TIME_SEPARATOR_SAFE = "꞉"
"""A visually similar but filesystem-safe separator for time components in filenames, replacing the standard colon (:) which can cause issues on some filesystems."""
TIME_SEPARATOR_MS = "․"
"""Separator for milliseconds in time representations, visually similar to the standard dot (.), but different symbol to avoid parsing collisions."""
DOTTED_KEY_SEPARATOR: str = "."
"""Separator used to join ``MetricKey`` prefix and suffix parts into a dotted metric key
(e.g. ``"encoding.h265"``).  This is the standard ASCII dot (U+002E) used exclusively
for metric key structure — distinct from file extension dots, ``TIME_SEPARATOR_MS``
(U+2024, used in filenames), and other uses of ``"."`` in the codebase."""

METRIC_KEY_QUALITY_MEASURE: str = "quality_measure"
"""Dotted-key suffix used to time quality metric measurement (VMAF/PSNR evaluation)
within a phase.  Combined with a ``MetricKey`` prefix it forms keys such as
``"encoding.quality_measure"`` and ``"merge.quality_measure"``.  Using a shared
constant ensures the suffix is identical across all phases that measure quality."""
BRACKET_LEFT = "｟"
BRACKET_RIGHT = " ｠"
UP_ARROW="↑"
DOWN_ARROW="↓"
LEFT_ARROW="←"
RIGHT_ARROW="→"

# Directory names for phase output
EXTRACTED_DIR          = "extracted"
"""Output directory for extracted streams (ExtractionPhase)."""
ENCODING_WORKSPACE_DIR = "encoding"
"""Working directory for CRF search attempt files (intermediate, per-strategy)."""
ENCODED_OUTPUT_DIR     = "encoded"
"""Output directory for finalized encoded artifacts (hard-linked winning attempts)."""
AUDIO_OUTPUT_DIR       = "audio"
"""Output directory for processed audio files (AudioPhase)."""
MERGED_OUTPUT_DIR      = "merged"
"""Output directory for merged outputs (MergePhase) — completing the
past-participle family (``extracted/``, ``encoded/``, ``merged/``): the merged
output is the merge phase's product, not a fully finalized container."""
MEASURE_DIR            = "measure"
"""Output subdirectory name for standalone measure artifacts."""

# Measure defaults
DEFAULT_SCREENSHOT_COUNT  = 20
"""Default number of screenshots captured from each video."""

# Config file locations (used by load_app_config and the `config` subcommand)
CONFIG_FILENAME_CWD  = "pyqenc.yaml"
"""Config filename searched in the current working directory."""
CONFIG_FILENAME_HOME = "config.yaml"
"""Config filename stored under the user-level config directory."""
CONFIG_DIR_HOME      = ".config/pyqenc"
"""Relative path under the user home directory for the user-level config."""

# Extraction artifact filenames
TIMESTAMPS_FILENAME = "timestamps.txt"
"""Filename for the per-frame PTS timestamp index produced by ExtractionPhase."""
CHAPTERS_FILENAME = "chapters.xml"
"""Filename for the extracted chapter edition produced by ExtractionPhase."""

# Artifact discovery patterns
CHUNK_NAME_PATTERN = re.compile(
    r"^(?:\d{2,}꞉\d{2}꞉\d{2}․\d{3})-(?:\d{2,}꞉\d{2}꞉\d{2}․\d{3})$"
)
"""Regex that validates and matches timestamp-based chunk file stems produced by
``_chunk_name_duration``.  A stem has the form
``HH꞉MM꞉SS․mmm-HH꞉MM꞉SS․mmm`` where ``꞉`` is ``TIME_SEPARATOR_SAFE`` and
``․`` is ``TIME_SEPARATOR_MS``."""

ENCODED_ATTEMPT_NAME_PATTERN = re.compile(
    r"^(?P<chunk_id>.+)\.(?P<resolution>\d+x\d+)\.q(?P<quality>[\d.]+)\.mkv$"
)
"""Regex that parses encoded attempt filenames produced by the quality-based naming
scheme.  Named groups: ``chunk_id``, ``resolution`` (e.g. ``1920x800``),
``quality`` (e.g. ``18.0``)."""

# Audio processing — filename conventions
CHAIN_FILENAME_SUFFIX = " chain="
"""Filename suffix marking a produced chain output: ``<stream name> chain=<name>.<ext>``.
The chain name that follows is parsed back out for exact-name artifact invalidation."""

# ffmpeg argument tokens (used when building audio chain commands)
FFMPEG_EXECUTABLE = "ffmpeg"
"""The ffmpeg executable name; the unified runner locates it as ``cmd[0]``."""

FFMPEG_ARG_INPUT    = "-i"
"""ffmpeg input-file flag."""
FFMPEG_ARG_MAP      = "-map"
"""ffmpeg stream-map flag."""
FFMPEG_ARG_SEEK     = "-ss"
"""ffmpeg seek flag; input-side (cue-point seek + decode to the exact frame)
when emitted before ``-i``."""
FFMPEG_ARG_DURATION = "-t"
"""ffmpeg duration-limit flag; bound to its input when emitted before ``-i``."""
FFMPEG_ARG_YES              = "-y"
"""ffmpeg overwrite-output flag. Injected by the unified runner into every
request — a stale ``.tmp`` from a crashed run must never hang ffmpeg on a prompt."""
FFMPEG_ARG_FILTER_COMPLEX   = "-filter_complex"
"""ffmpeg multi-input filter-graph flag."""
FFMPEG_ARG_VF               = "-vf"
"""ffmpeg video-filter-chain flag; its value is a comma-joined filter chain."""
FFMPEG_ARG_MAP_CHAPTERS     = "-map_chapters"
"""ffmpeg chapter-mapping flag. The runner injects it into every request with
``FFMPEG_MAP_CHAPTERS_DISABLED``: chapters copy to the output regardless of
``-map``, and no ffmpeg output in this pipeline intentionally carries them."""
FFMPEG_MAP_CHAPTERS_DISABLED = "-1"
"""``-map_chapters`` value that copies no chapters from the input."""
FFMPEG_ARG_AF       = "-af"
"""ffmpeg audio-filter-chain flag; its value is a comma-joined filter chain."""
FFMPEG_ARG_CODEC_A  = "-c:a"
"""ffmpeg audio-codec flag."""
FFMPEG_ARG_BITRATE_A = "-b:a"
"""ffmpeg audio-bitrate flag."""
FFMPEG_ARG_FORMAT   = "-f"
"""ffmpeg output-format (muxer) flag."""

FFMPEG_MAP_FIRST_VIDEO = "0:v:0"
"""Maps the first video stream of the (single) input — null-count passes."""

FFMPEG_SELECTOR_PREFIX = "0:"
"""Input-index prefix of a ``-map`` selector built from a track id
(``0:<track_id>`` — input 0, absolute stream index); consumed by the stream
model's ``as_input()`` adapter."""

FFMPEG_CODEC_COPY = "copy"
"""Stream-copy codec token — remux-style extraction and null-count passes."""

FFMPEG_NULL_MUXER = "null"
"""The ``null`` muxer, used with a ``-`` sink for measurement passes that write no file."""
FFMPEG_NULL_SINK  = "-"
"""stdout sink placeholder paired with the ``null`` muxer for measurement passes."""

# ---------------------------------------------------------------------------
# Output-container muxers
# ---------------------------------------------------------------------------
# The runner writes every file output to a ``.tmp`` sibling (atomicity), and the
# ``.tmp`` extension carries no container hint — so each call site supplies an
# explicit ``-f <muxer>``; the runner default is ``matroska``.

FFMPEG_MUXER_MATROSKA = "matroska"
"""Default ``-f`` muxer for ``.tmp`` outputs — Matroska (all video call sites)."""
FFMPEG_MUXER_DATA     = "data"
"""``-f`` muxer for raw stream-copy outputs — attached-picture attachments
dumped as their verbatim bytes (the ``.tmp`` extension hides nothing to sniff)."""
FFMPEG_MUXER_FLAC     = "flac"
FFMPEG_MUXER_IPOD     = "ipod"
"""``-f`` muxer for ``.m4a`` audio-only containers — ffmpeg's conventional
choice (not derivable from the extension)."""
FFMPEG_MUXER_IMAGE2   = "image2"
"""``-f`` muxer for single-frame PNG screenshot outputs written through the
``.tmp`` protocol (the ``.tmp`` extension hides the image container hint)."""

AF_CHAIN_SEPARATOR = ","
"""ffmpeg's audio-filter-chain separator; chain fragments are joined with it."""

FLAC_CODEC     = "flac"
"""Default audio codec when a chain declares no ``encode`` filter (lossless, bitrate-agnostic)."""
FLAC_EXTENSION = "flac"
"""Default output extension when a chain declares no ``encode`` filter."""

M4A_EXTENSION = "m4a"
"""Output extension for an AAC-in-MP4 audio container."""

OUTPUT_FORMAT_MUXERS: dict[str, str] = {
    FLAC_EXTENSION: FFMPEG_MUXER_FLAC,   # flac -> flac
    M4A_EXTENSION:  FFMPEG_MUXER_IPOD,   # m4a  -> ipod
}
"""Maps an audio output extension (without the dot) to the ffmpeg ``-f`` muxer to
use for its ``.tmp`` output. The chain executor looks up the effective encode
extension here and passes the muxer to the runner as ``output_format``; a missing
extension falls back to the runner default (Matroska)."""

FILENAME_FORBIDDEN_CHARS: frozenset[str] = frozenset('<>:"/\\|?*')
"""Characters forbidden in a chain name because they are unsafe in filenames on
common filesystems (Windows especially). A chain name containing any of these —
or any control character (U+0000–U+001F) — is rejected at config load."""

FILENAME_CONTROL_CHARS: frozenset[str] = frozenset(chr(code) for code in range(0x20))
"""Control characters (U+0000–U+001F) — unsafe in filenames alongside
:data:`FILENAME_FORBIDDEN_CHARS`; shared by every filesystem-name check."""

FILENAME_SANITIZATION_REPLACEMENT = "_"
"""Replacement character for :func:`pyqenc.utils.naming.sanitize_filesystem_text`
— media-sourced free text is sanitized by replacement, never rejected."""

SELECTOR_KEY_LANG  = "lang"
"""Conventional-string token key for an audio track's language (e.g. ``lang=eng``)."""
SELECTOR_KEY_CH    = "ch"
"""Conventional-string token key for an audio track's channel layout (e.g. ``ch=5.1(side)``)."""
SELECTOR_KEY_TITLE = "title"
"""Conventional-string token key for an audio track's title (e.g. ``title=Surround``)."""

# Progress display
STDERR_TAIL_LINES = 20
"""Number of recent stderr lines to retain in the rolling buffer used by
``_drain_stderr``.  These lines are available for error logging after the
subprocess exits."""

# Visual hash emoji pool — East-Asian-Width "W" codepoints only (2 terminal
# columns each in monospace fonts); no variation-selector suffixes (U+FE0F),
# which render inconsistently.

VISUAL_HASH_EMOJIS_WIDE: list[str] = [
    # Animals
    "\U0001F436","\U0001F431","\U0001F42D","\U0001F430","\U0001F43B","\U0001F43C",
    "\U0001F42F","\U0001F981","\U0001F42E","\U0001F437","\U0001F438","\U0001F435",
    "\U0001F414","\U0001F427","\U0001F426","\U0001F43A","\U0001F434","\U0001F41D",
    "\U0001F41B","\U0001F40C","\U0001F41E","\U0001F422","\U0001F40D","\U0001F419",
    "\U0001F42C","\U0001F433","\U0001F40B","\U0001F40A","\U0001F405","\U0001F406",
    "\U0001F418","\U0001F98A","\U0001F99D","\U0001F9A8","\U0001F9A1","\U0001F9A5",
    "\U0001F994","\U0001F43E","\U0001F983","\U0001F99A","\U0001F99C","\U0001F9A2",
    "\U0001F9A9","\U0001F407","\U0001F98C","\U0001F999","\U0001F998",# "\U0001F9AC" - bison "🦬" doesn't render properly often
    "\U0001F402","\U0001F403","\U0001F404","\U0001F40E","\U0001F40F","\U0001F411",
    "\U0001F410","\U0001F415","\U0001F429","\U0001F9AE","\U0001F408","\U0001F413",
    "\U0001F9A4","\U0001F985","\U0001F986","\U0001F989","\U0001F987","\U0001F417",
    "\U0001F98B","\U0001F41C","\U0001F99F","\U0001F997","\U0001F982","\U0001F98E",
    "\U0001F996","\U0001F995","\U0001F993","\U0001F9A7","\U0001F99B", # "\U0001F9A3" - mammoth "🦣" doesn't render properly often
    "\U0001F98F","\U0001F42A","\U0001F42B","\U0001F992","\U0001F416",
    # Food & drink
    "\U0001F34E","\U0001F34A","\U0001F34B","\U0001F347","\U0001F353","\U0001F352",
    "\U0001F351","\U0001F95D","\U0001F346","\U0001F955","\U0001F33D","\U0001F344",
    "\U0001F95C","\U0001F35E","\U0001F9C0","\U0001F356","\U0001F355","\U0001F354",
    "\U0001F32E","\U0001F35C","\U0001F363","\U0001F369","\U0001F36A","\U0001F382",
    "\U0001F36B","\U0001F36C","\U0001F36D","\U0001F9C1",
    # Nature / weather / space
    "\U0001F338","\U0001F33A","\U0001F33B","\U0001F339","\U0001F340","\U0001F33F",
    "\U0001F335","\U0001F334","\U0001F30A","\U0001F525","\U0001F4A7","\U000026A1",
    "\U0001F308","\U0001F319","\U00002B50","\U0001F31F","\U0001F4AB","\U0001F30D",
    "\U0001F30B","\U0001F341",
    # Vehicles
    "\U0001F697","\U0001F695","\U0001F699","\U0001F68C","\U0001F68E","\U0001F693",
    "\U0001F691","\U0001F692","\U0001F690","\U0001F69A","\U0001F69B","\U0001F69C",
    "\U0001F6B2","\U0001F680","\U0001F6F8","\U0001F681","\U0001F6F6","\U000026F5",
    "\U0001F682","\U0001F6A2",
    # Objects / tools
    "\U0001F48E","\U0001F48D","\U0001F451","\U0001F511","\U0001F514","\U0001F3B5",
    "\U0001F3B8","\U0001F3B9","\U0001F3BA","\U0001F3BB","\U0001F941","\U0001F3AE",
    "\U0001F3B2","\U0001F3AF","\U0001F3B3","\U0001F3C6","\U0001F947","\U0001F381",
    "\U0001F380","\U0001F388","\U0001F389","\U0001F52D","\U0001F52C","\U0001F4A1",
    "\U0001F526",
    # Sports
    "\U000026BD","\U0001F3C0","\U0001F3C8","\U000026BE","\U0001F3BE","\U0001F3D0",
    "\U0001F3C9","\U0001F3B1","\U0001F3D3","\U0001F3F8","\U0001F94A","\U0001F94B",
    "\U0001F3BF","\U0001F3C4","\U0001F93F","\U0001F9D7","\U0001F3C7","\U0001F938",
    "\U0001F93C",
    # Clothing / accessories
    "\U0001F452","\U0001F3A9","\U0001F9E2","\U0001F453","\U0001F97D","\U0001F302",
    "\U0001F45C","\U0001F45D","\U0001F392","\U0001F9F3","\U0001F4BC","\U0001F45F",
    "\U0001F460","\U0001F461","\U0001F462","\U0001F97E","\U0001F9E4","\U0001F9E3",
    "\U0001F9E5",
    # Buildings / places
    "\U0001F3E0","\U0001F3E1","\U0001F3E2","\U0001F3E3","\U0001F3E4","\U0001F3E5",
    "\U0001F3E6","\U0001F3E8","\U0001F3E9","\U0001F3EA","\U0001F3EB","\U0001F3EC",
    "\U0001F3ED","\U0001F3EF","\U0001F3F0","\U000026EA","\U0001F54C","\U0001F54D",
    "\U0001F5FC","\U0001F5FD","\U0001F5FF",
    # Fantasy / mythology
    "\U0001F9D9","\U0001F9DD","\U0001F9DB","\U0001F9DF","\U0001F9DE","\U0001F9DC",
    "\U0001F9DA","\U0001F47B","\U0001F480","\U0001F47D","\U0001F47E","\U0001F916",
    "\U0001F383","\U0001F984","\U0001F432","\U0001F409","\U0001F9FF","\U0001F52E",
    "\U0001F9F2",
    # Hands / gestures
    "\U0001F44B","\U0001F91A","\U0000270B","\U0001F446","\U0001F447","\U0001F44D",
    "\U0001F44E","\U0000270A","\U0001F44A","\U0001F91B","\U0001F91C","\U0001F44F",
    "\U0001F64C","\U0001F932","\U0001F91D","\U0001F64F",
    # Symbols / tech
    "\U0001F6AB","\U000026D4","\U0001F51E","\U0001F515","\U0001F507","\U0001F508",
    "\U0001F509","\U0001F50A","\U0001F4E2","\U0001F4E3","\U0001F50B","\U0001F50C",
    "\U0001F4BB","\U0001F4BE","\U0001F4BF","\U0001F4C0","\U0001F4F7","\U0001F4F8",
    "\U0001F4F9","\U0001F3A5",
]
"""290 full-width emojis (East Asian Width = W) for visual hash prefixes in chunk log lines.
Each occupies exactly 2 terminal columns in a monospace font, ensuring consistent alignment."""
