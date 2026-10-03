"""
Log formatting helpers for uniform chunk attempt and optimization output.

All public functions return plain strings or lists of strings — no logging
side-effects — so callers decide the log level.

Exception: ``emit_phase_banner`` and ``log_recovery_line`` are side-effecting
helpers that accept a logger and emit directly, since they are always called
at ``info`` level and the pattern is too mechanical to benefit from separation.
"""
# CHerSun 2026

import decimal
import hashlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from pyqenc.constants import (
    BRACKET_LEFT,
    BRACKET_RIGHT,
    FAILURE_SYMBOL_MINOR,
    METRIC_LOG_DECIMAL_PLACES,
    NEUTRAL_INDICATOR_SYMBOL,
    SUCCESS_SYMBOL_MAJOR,
    THICK_LINE,
    VISUAL_HASH_EMOJIS_WIDE,
)
from pyqenc.state import ArtifactState

if TYPE_CHECKING:
    from decimal import Decimal

    from pyqenc.phase import Artifact

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
    metrics_dict: dict[str, float],
    worst_key:    str | None,
    worst_passed: bool,
) -> str:
    """Format a metric summary string, marking the worst-deficit metric.

    The metric with the smallest surplus (or largest deficit) — i.e. the one
    that most constrains the CRF search — is marked with ``BOTTLENECK_SYMBOL``
    (•) on a pass or ``FAILURE_SYMBOL_MINOR`` (✘) on a miss.  All other values are
    plain.  This makes it immediately visible which metric drove the next CRF
    selection.

    Args:
        metrics_dict: Measured metrics keyed as ``"<metric>_<stat>"``.
        worst_key:    Key of the worst-deficit metric, or ``None`` if unknown.
        worst_passed: ``True`` when the worst metric still passed its target.

    Returns:
        Space-separated string where each value has a trailing symbol:
        ``•`` for the bottleneck (least surplus on pass),
        ``✘`` for the worst deficit (on miss),
        `` `` (space) for all others — keeping columns aligned across log lines.
        Example: ``"psnr_min=41.8✘ ssim_min=97.8  vmaf_min=95.9 "``
    """

    parts: list[str] = []
    for k, v in metrics_dict.items():
        if k == worst_key:
            symbol = NEUTRAL_INDICATOR_SYMBOL if worst_passed else FAILURE_SYMBOL_MINOR
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


def emit_phase_start(name: str, log: logging.Logger) -> None:
    """Emit the soft separator for banner-less phases.

    A blank line plus a lowercase start line — clear section separation at a
    fraction of the banner's visual weight, so a banner-less phase's output
    can never read as a continuation of the previous phase's.

    Args:
        name: Phase name (e.g. ``"probe"``).
        log:  Logger instance belonging to the calling phase module.
    """
    log.info("")
    log.info("Starting %s...", name)


def log_recovery_line(
    log:       logging.Logger,
    artifacts: list[Artifact],
    unit:      str = "artifact",
) -> str:
    """Log the recovery summary and return the same human-readable message.

    Takes the phase's INTERNAL artifact ledger (pre-filter, including
    ``wanted=False`` entries) — NOT ``PhaseResult.artifacts``.  Derives all
    counts itself; callers never compute recovery counts locally.

    The counts are: ``total`` (every internal row — internal artifacts and
    ``wanted=False`` rows included), ``wanted`` (the selected rows), and
    ``complete``/``partial``/``absent`` (counted over the wanted rows only).
    The identity ``wanted == complete + partial + absent`` always holds. All
    counts are always shown, even when zero.

    The suffix states what the template does with this ledger, keyed on the
    pending count (``partial + absent``): ``all reused`` when nothing is
    pending (the template fast-exits to ``REUSED`` — this line is the only
    uniform full-reuse signal a phase emits), ``resuming`` when reusable work
    exists alongside the pending remainder, ``nothing to reuse`` when no
    wanted row is complete.

    The returned string is the single source of truth for the recovery message:
    callers assign it directly to ``PhaseResult.message`` rather than building a
    separate message via a per-phase helper.

    Args:
        log:       Logger instance belonging to the calling phase module.
        artifacts: The phase's internal artifact list (including ``wanted=False``
                   entries).
        unit:      Singular noun for the artifact type (e.g. ``"chunk"``,
                   ``"pair"``, ``"attempt"``). Reserved for callers that
                   want a noun other than the default; it does not affect the
                   counts.

    Returns:
        The emitted recovery message, identical to the logged line.
    """
    total    = len(artifacts)
    wanted   = sum(1 for a in artifacts if a.wanted)
    complete = sum(1 for a in artifacts if a.wanted and a.state == ArtifactState.COMPLETE)
    partial  = sum(1 for a in artifacts if a.wanted and a.state == ArtifactState.PARTIAL)
    absent   = sum(1 for a in artifacts if a.wanted and a.state == ArtifactState.ABSENT)

    if partial + absent == 0:
        suffix = "all reused"
    elif complete > 0:
        suffix = "resuming"
    else:
        suffix = "nothing to reuse"
    message = (
        f"Recovery: {total} total, {wanted} wanted"
        f" ({complete} complete, {partial} partial, {absent} absent)"
        f" — {suffix}"
    )
    log.info(message)
    return message


def visual_hash(strategy: str, chunk_id: str) -> str:
    """Return 1 full-width emoji deterministically derived from strategy+chunk_id.

    Uses MD5 of ``"{strategy}:{chunk_id}"`` truncated to 4 bytes as the hash.
    The result is stable across runs and unique per (strategy, chunk_id) pair
    within the pool size (290 emojis), making parallel log lines visually
    distinguishable at a glance.

    Args:
        strategy: Encoding strategy name (e.g. ``"h264+veryslow"``).
        chunk_id: Chunk timestamp range identifier.
    """
    h = int.from_bytes(
        hashlib.md5(f"{strategy}:{chunk_id}".encode()).digest()[:4], "big"
    )
    return VISUAL_HASH_EMOJIS_WIDE[h % len(VISUAL_HASH_EMOJIS_WIDE)]


def fmt_chunk(strategy: str, chunk_id: str, msg: str, use_visual_hash: bool = True) -> str:
    prefix = f"{visual_hash(strategy, chunk_id)} " if use_visual_hash else ""
    return f"{prefix}{BRACKET_LEFT}{strategy}{BRACKET_RIGHT} {chunk_id} {msg}"

def fmt_chunk_start(strategy: str, chunk_id: str, use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, "starting ...", use_visual_hash)

def fmt_chunk_attempt_start(strategy: str, chunk_id: str, attempt: int, quality: Decimal, quality_label: str = "CRF", use_visual_hash: bool = True, quality_padding: int = 4) -> str:
    return fmt_chunk(strategy, chunk_id, f"starting attempt #{attempt} with {quality_label} {str(quality).rjust(quality_padding)} ...", use_visual_hash)

def fmt_chunk_attempt_result(strategy: str, chunk_id: str, attempt: int, msg: str, use_visual_hash: bool = True) -> str:
    return fmt_chunk(strategy, chunk_id, f"attempt #{attempt}: {msg}", use_visual_hash)

def fmt_chunk_final(strategy: str, chunk_id: str, quality: Decimal, attempts: int, quality_label: str = "CRF", use_visual_hash: bool = True, quality_padding: int = 4, limited_by: str | None = None, status: str | None = None) -> str:
    """The per-chunk acceptance line — success, miss, or exhaustion.

    The single shape for every accepted winner: ``{status} with {label} {q}
    after N attempts — limited by {limiter}``. *status* defaults to
    ``success ✅``; callers pass a severity-carrying status — ``miss ≈``
    (fixed-mode ruler miss — the auto-elected anchor is approximate, a
    matter of fact) or ``exhausted ❌`` (search ran out of candidates short
    of user-requested quality — a real, bypassable problem) — keeping all
    acceptance lines uniform in shape while distinct in severity.
    """
    status_text = status if status is not None else f"success {SUCCESS_SYMBOL_MAJOR}"
    limit_note = f" — limited by {limited_by}" if limited_by is not None else ""
    return fmt_chunk(strategy, chunk_id, f"{status_text} with {quality_label} {str(quality).rjust(quality_padding)} after {attempts} attempts{limit_note}", use_visual_hash)

def fmt_key_value_table(kv_to_show: Mapping[str, object]) -> None:
    """Log a key-value table at INFO level with aligned columns.

    Value dispatch (checked in this order):
    1. ``str`` → single line, formatted as-is.
    2. ``list`` → multi-line: first item on the key line,
       subsequent items on continuation lines aligned to the value column
       (key column is blank).
    3. Anything else → single line via str().

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

def fmt_size_mb(size_bytes: int) -> str:
    """Format *size_bytes* as MB with a narrow-space thousands separator.

    One decimal place below 1000 MB, none at or above (column width stays
    stable for large outputs).

    Example: 4_231_400_000 → ``"4 031"``
    """
    mb = size_bytes / (1024 * 1024)
    # Format with comma thousands separator then swap to narrow no-break space (U+202F). Use single decimal place for <1000 MB values.
    return f"{mb:,.1f}".replace(",", "\u202f") if mb < 1000 else f"{mb:,.0f}".replace(",", "\u202f")



