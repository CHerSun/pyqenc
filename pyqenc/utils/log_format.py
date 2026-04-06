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
    NEUTRAL_INDICATOR_SYMBOL,
    PADDING_QUALITY_NUMBER,
    SUCCESS_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MINOR,
    THICK_LINE,
    THIN_LINE,
    VISUAL_HASH_EMOJIS_WIDE,
)

if TYPE_CHECKING:
    from decimal import Decimal

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


def fmt_metric_summary(
    metrics_dict:    dict[str, float],
    quality_targets: "list[QualityTarget]",
) -> str:
    """Format a metric summary string, marking the worst-deficit metric.

    The metric with the smallest surplus (or largest deficit) — i.e. the one
    that most constrains the CRF search — is marked with ``BOTTLENECK_SYMBOL``
    (•) on a pass or ``FAILURE_SYMBOL_MINOR`` (✘) on a miss.  All other values are
    plain.  This makes it immediately visible which metric drove the next CRF
    selection.

    Args:
        metrics_dict:    Measured metrics keyed as ``"<metric>_<stat>"``.
        quality_targets: Quality targets used to determine worst deficit.

    Returns:
        Space-separated string where each value has a trailing symbol:
        ``•`` for the bottleneck (least surplus on pass),
        ``✘`` for the worst deficit (on miss),
        `` `` (space) for all others — keeping columns aligned across log lines.
        Example: ``"psnr_min=41.8✘ ssim_min=97.8  vmaf_min=95.9 "``
    """
    from pyqenc.quality import _find_worst_target
    found      = _find_worst_target(metrics_dict, quality_targets)
    worst_key  = f"{found[0].metric}_{found[0].statistic}" if found is not None else None
    worst_pass = found[1] >= 0 if found is not None else True

    parts: list[str] = []
    for k, v in metrics_dict.items():
        if k == worst_key:
            symbol = NEUTRAL_INDICATOR_SYMBOL if worst_pass else FAILURE_SYMBOL_MINOR
        else:
            symbol = " "
        parts.append(f"{k}={fmt_metric_value(v)}{symbol}")
    return " ".join(parts).strip()

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

def fmt_chunk_attempt_start(strategy: str, chunk_id: str, attempt: int, quality: "Decimal", quality_label: str = "CRF", use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, f"starting attempt #{attempt} with {quality_label} {str(quality).rjust(PADDING_QUALITY_NUMBER)} ...", use_visual_hash)

def fmt_chunk_attempt_result(strategy: str, chunk_id: str, attempt: int, msg: str, use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, f"attempt #{attempt}: {msg}", use_visual_hash)

def fmt_chunk_final(strategy: str, chunk_id: str, quality: "Decimal", attempts: int, quality_label: str = "CRF", use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, f"success {SUCCESS_SYMBOL_MAJOR} with {quality_label} {str(quality).rjust(PADDING_QUALITY_NUMBER)} after {attempts} attempts", use_visual_hash)

def fmt_key_value_table(kv_to_show: dict[str, str | list | object]) -> None:
    """Log a key-value table at INFO level with aligned columns.

    Value dispatch (checked in this order):
    1. ``isinstance(value, str)`` → single line, formatted as-is.
    2. ``isinstance(value, list)`` → multi-line: first item on the key line,
       subsequent items on continuation lines aligned to the value column
       (key column is blank).
    3. Anything else → single line via ``f"{value}"``.

    ``str`` is checked before ``list`` because ``str`` is iterable and would
    otherwise incorrectly satisfy a bare ``isinstance(v, list)`` check.

    Example output::

        source    /path/to/source.mkv
        targets   target_a.mkv
                  target_b.mkv
        crop      top=138 bottom=138
        sampling  10
    """
    max_key_len = max(len(k) for k in kv_to_show) + 1
    for key, value in kv_to_show.items():
        if isinstance(value, str):
            logger.info(f"{key:<{max_key_len}} {value}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                prefix = key if i == 0 else ""
                logger.info(f"{prefix:<{max_key_len}} {item}")
        else:
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




