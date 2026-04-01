"""
Log formatting helpers for uniform chunk attempt and optimization output.

All public functions return plain strings or lists of strings — no logging
side-effects — so callers decide the log level.

Exception: ``emit_phase_banner`` and ``log_recovery_line`` are side-effecting
helpers that accept a logger and emit directly, since they are always called
at ``info`` level and the pattern is too mechanical to benefit from separation.
"""
# CHerSun 2026

from __future__ import annotations

import decimal
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import (
    BRACKET_LEFT,
    BRACKET_RIGHT,
    FAILURE_SYMBOL_MAJOR,
    FAILURE_SYMBOL_MINOR,
    METRIC_LOG_DECIMAL_PLACES,
    PADDING_CRF,
    SUCCESS_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MINOR,
    THICK_LINE,
    THIN_LINE,
    VISUAL_HASH_EMOJIS_WIDE,
)

if TYPE_CHECKING:
    from pyqenc.models import QualityTarget
    from pyqenc.phases.optimization import StrategyTestResult

logger = logging.getLogger(__name__)

# Quantizer for floor-truncating metric values to METRIC_LOG_DECIMAL_PLACES.
# Built once at import time so formatting stays cheap.
_METRIC_LOG_QUANTIZER = decimal.Decimal(10) ** -METRIC_LOG_DECIMAL_PLACES


def fmt_metric_value(value: float) -> str:
    """Format a metric float for log display, truncating (flooring) to ``METRIC_LOG_DECIMAL_PLACES``.

    Uses ``decimal.ROUND_FLOOR`` so a miss can never display as a pass due to
    rounding up.  E.g. ``92.9999`` → ``"93.0"`` with normal rounding, but
    ``"92.9"`` with this function.

    Args:
        value: Raw metric float (e.g. VMAF score).

    Returns:
        Truncated string representation with ``METRIC_LOG_DECIMAL_PLACES`` decimal places.
    """
    return str(decimal.Decimal(str(value)).quantize(_METRIC_LOG_QUANTIZER, rounding=decimal.ROUND_FLOOR))

def emit_phase_banner(name: str, log: logging.Logger) -> None:
    """Emit the standard thick-line banner for a phase.

    Args:
        name: Phase name in UPPER CASE (e.g. ``"EXTRACTION"``).
        log:  Logger instance belonging to the calling phase module.
    """
    log.info(THICK_LINE)
    log.info(name)
    log.info(THICK_LINE)


def log_recovery_line(
    log:      logging.Logger,
    complete: int,
    pending:  int,
    stale:    int = 0,
    unit:     str = "artifact",
) -> None:
    """Emit the standard single-line recovery summary.

    Args:
        log:      Logger instance belonging to the calling phase module.
        complete: Number of artifacts already complete.
        pending:  Number of artifacts needing work (ABSENT or ARTIFACT_ONLY).
        stale:    Number of stale artifacts (present but parameters changed).
        unit:     Singular noun for the artifact type (e.g. ``"chunk"``,
                  ``"pair"``, ``"strategy result"``).  Pluralised by appending
                  ``"s"`` when count ≠ 1.
    """
    def _plural(n: int) -> str:
        return f"{n} {unit}{'s' if n != 1 else ''}"

    if pending == 0 and stale == 0:
        log.info("Recovery: %s complete, 0 pending — reusing", _plural(complete))
    elif complete == 0 and stale == 0:
        log.info("Recovery: 0 complete, %s pending — full run needed", _plural(pending))
    else:
        parts = [f"{_plural(complete)} complete", f"{_plural(pending)} pending"]
        if stale:
            parts.append(f"{stale} stale")
        suffix = "resuming" if complete > 0 else "full run needed"
        log.info("Recovery: %s — %s", ", ".join(parts), suffix)


def visual_hash(strategy: str, chunk_id: str) -> str:
    """Return 1 full-width emoji deterministically derived from strategy+chunk_id.

    Uses MD5 of ``"{strategy}:{chunk_id}"`` truncated to 4 bytes as the hash.
    The result is stable across runs and unique per (strategy, chunk_id) pair
    within the pool size (290 emojis), making parallel log lines visually
    distinguishable at a glance.

    Args:
        strategy: Encoding strategy name (e.g. ``"veryslow+h264"``).
        chunk_id: Chunk timestamp range identifier.
    """
    h = int.from_bytes(
        hashlib.md5(f"{strategy}:{chunk_id}".encode()).digest()[:4], "big"
    )
    return VISUAL_HASH_EMOJIS_WIDE[h % len(VISUAL_HASH_EMOJIS_WIDE)]


def _fmt_chunk_prefix(strategy: str, chunk_id: str, use_visual_hash: bool = True) -> str:
    prefix = f"{visual_hash(strategy, chunk_id)} " if use_visual_hash else ""
    return f"{prefix}{BRACKET_LEFT}{strategy}{BRACKET_RIGHT} {chunk_id}"

def fmt_chunk(strategy: str, chunk_id: str, msg: str, use_visual_hash: bool = True) -> str:
    return _fmt_chunk_prefix(strategy, chunk_id, use_visual_hash) + f" {msg}"

def fmt_chunk_start(strategy: str, chunk_id: str, use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, "starting ...", use_visual_hash)

def fmt_chunk_attempt_start(strategy: str, chunk_id: str, attempt: int, crf: float, use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, f"starting attempt #{attempt} with CRF {crf:{PADDING_CRF}} ...", use_visual_hash)

def fmt_chunk_attempt_result(strategy: str, chunk_id: str, attempt: int, msg: str, use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, f"attempt #{attempt}: {msg}", use_visual_hash)

def fmt_chunk_final(strategy: str, chunk_id: str, crf: float, attempts: int, use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, f"success {SUCCESS_SYMBOL_MAJOR} with CRF {crf:{PADDING_CRF}} after {attempts} attempts", use_visual_hash)

def fmt_strategy_result_block(
    strategy:      str,
    avg_crf:       float,
    total_size_mb: float,
    num_chunks:    int,
    passed:        bool,
    error:         str | None = None,
) -> list[str]:
    """Return a visually distinct block of log lines for one strategy result.

    The block is bordered by ``─`` delimiter lines (72 chars wide).

    Args:
        strategy:      Strategy name.
        avg_crf:       Average CRF across test chunks.
        total_size_mb: Total size of encoded test chunks in MB.
        num_chunks:    Number of test chunks encoded.
        passed:        Whether all chunks met quality targets.
        error:         Optional error message if the strategy failed.

    Returns:
        List of log lines (caller emits each at the desired level).
    """
    status_icon = f"{SUCCESS_SYMBOL_MAJOR} PASSED" if passed else f"{FAILURE_SYMBOL_MAJOR} FAILED"
    lines: list[str] = [
        THIN_LINE,
        f"Strategy result: {strategy}",
        f"  Status    : {status_icon}",
        f"  Avg CRF   : {avg_crf:.2f}",
        f"  Total size: {total_size_mb:.2f} MB  ({num_chunks} chunks)",
    ]
    if error:
        lines.append(f"  Error     : {error}")
    lines.append(THIN_LINE)
    return lines

def fmt_key_value_table(kv_to_show):
    """Format a dictionary of key-value pairs into aligned log lines for display as a table."""
    max_key_len = max(len(k) for k in kv_to_show.keys())+1
    for key, value in kv_to_show.items():
        logger.info(f"{key:<{max_key_len}} {value}")


# ---------------------------------------------------------------------------
# Merge summary helpers
# ---------------------------------------------------------------------------

def _fmt_size_mb(size_bytes: int) -> str:
    """Format *size_bytes* as MB with a narrow-space thousands separator.

    Example: 4_231_400_000 → ``"4 031.4"``
    """
    mb = size_bytes / (1024 * 1024)
    # Format with comma thousands separator then swap to narrow no-break space (U+202F). Use single decimal place for <1000 MB values.
    return f"{mb:,.1f}".replace(",", "\u202f") if mb < 1000 else f"{mb:,.0f}".replace(",", "\u202f")


def _fmt_savings(size_bytes: int, reference_size_bytes: int) -> str:
    """Return savings percentage string, e.g. ``"77.0%"``."""
    if reference_size_bytes <= 0:
        return "N/A"
    saved = (1 - size_bytes / reference_size_bytes) * 100
    return f"{saved:.1f}%"


def _fmt_target_value(
    target:      "QualityTarget",
    metrics:     dict[str, float],
    targets_met: bool | None,
) -> str:
    """Return a formatted metric value with pass/fail symbol for *target*.

    Returns ``"N/A"`` when the metric key is absent from *metrics*.
    """
    key   = f"{target.metric}_{target.statistic}"
    value = metrics.get(key)
    if value is None:
        return "N/A"
    symbol = SUCCESS_SYMBOL_MINOR if value >= target.value else FAILURE_SYMBOL_MINOR
    return f"{fmt_metric_value(value)} {symbol}"




