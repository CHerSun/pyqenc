"""
Log formatting helpers for uniform chunk attempt and optimization output.

All public functions return plain strings or lists of strings — no logging
side-effects — so callers decide the log level.

Exception: ``emit_phase_banner`` and ``log_recovery_line`` are side-effecting
helpers that accept a logger and emit directly, since they are always called
at ``info`` level and the pattern is too mechanical to benefit from separation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pyqenc.constants import (
    BRACKET_LEFT,
    BRACKET_RIGHT,
    FAILURE_SYMBOL_MAJOR,
    FAILURE_SYMBOL_MINOR,
    PADDING_CRF,
    SUCCESS_SYMBOL_MAJOR,
    SUCCESS_SYMBOL_MINOR,
    THICK_LINE,
    THIN_LINE,
)

if TYPE_CHECKING:
    from pyqenc.models import QualityTarget
    from pyqenc.phases.optimization import StrategyTestResult

logger = logging.getLogger(__name__)


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


def _fmt_chunk_prefix(strategy: str, chunk_id: str) -> str:
    return f"{BRACKET_LEFT}{strategy}{BRACKET_RIGHT} {chunk_id}"

def fmt_chunk(strategy: str, chunk_id: str, msg: str) -> str:
    return _fmt_chunk_prefix(strategy, chunk_id) + f" {msg}"

def fmt_chunk_start(strategy: str, chunk_id: str) -> str:
    return fmt_chunk(strategy, chunk_id, "starting ...")

def fmt_chunk_attempt_start(strategy: str, chunk_id: str, attempt: int, crf: float) -> str:
    return fmt_chunk(strategy, chunk_id, f"starting attempt #{attempt} with CRF {crf:{PADDING_CRF}} ...")

def fmt_chunk_attempt_result(strategy: str, chunk_id: str, attempt: int, msg: str) -> str:
    return fmt_chunk(strategy, chunk_id, f"attempt #{attempt}: {msg}")

def fmt_chunk_final(strategy: str, chunk_id: str, crf: float, attempts: int) -> str:
    return fmt_chunk(strategy, chunk_id, f"success {SUCCESS_SYMBOL_MAJOR} with CRF {crf:{PADDING_CRF}} after {attempts} attempts")

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
    return f"{value:.1f} {symbol}"


def fmt_merge_summary_optimal(
    output_file:          Path,
    size_bytes:           int,
    reference_size_bytes: int | None,
    quality_targets:      list["QualityTarget"],
    metrics:              dict[str, float],
    targets_met:          bool,
) -> list[str]:
    """Return a key-value block summary for a single-strategy (optimal) merge.

    Args:
        output_file:          Path to the merged output file.
        size_bytes:           Size of the output file in bytes.
        reference_size_bytes: Size of the reference (extracted) video in bytes, or ``None``.
        quality_targets:      Quality targets used during encoding.
        metrics:              Final measured metric values (normalized, flat dict).
        targets_met:          Whether all quality targets were met.

    Returns:
        List of log lines forming the summary block.
    """
    size_str = _fmt_size_mb(size_bytes)
    if reference_size_bytes is not None:
        ref_str     = _fmt_size_mb(reference_size_bytes)
        savings_str = _fmt_savings(size_bytes, reference_size_bytes)
        size_line   = f"{size_str} MB  (saved {savings_str} vs {ref_str} MB reference)"
    else:
        size_line = f"{size_str} MB"

    # Build targets line: "vmaf-min≥85 → 91.2 ✔   ssim-min≥95 → 96.1 ✔"
    target_parts: list[str] = []
    for target in quality_targets:
        value_str = _fmt_target_value(target, metrics, targets_met)
        target_parts.append(f"{target} → {value_str}")
    targets_line = "   ".join(target_parts) if target_parts else "N/A"

    strategy_name = output_file.stem  # e.g. "О чём говорят мужчины Blu-Ray (1080p) (1) slow_h265"

    lines: list[str] = [
        THICK_LINE,
        "MERGE SUMMARY",
        THICK_LINE,
        f"  Output    : {output_file.name}",
        f"  Size      : {size_line}",
    ]
    if quality_targets:
        lines.append(f"  Targets   : {targets_line}")
    lines.append(THICK_LINE)
    return lines


def fmt_merge_summary_all(
    output_files:         dict[str, Path],
    sizes_bytes:          dict[str, int],
    reference_size_bytes: int | None,
    quality_targets:      list["QualityTarget"],
    final_metrics:        dict[str, dict[str, float]],
    targets_met:          dict[str, bool],
) -> list[str]:
    """Return a table summary for an all-strategies merge.

    Args:
        output_files:         Mapping of strategy name → output file path.
        sizes_bytes:          Mapping of strategy name → output file size in bytes.
        reference_size_bytes: Size of the reference (extracted) video in bytes, or ``None``.
        quality_targets:      Quality targets used during encoding.
        final_metrics:        Mapping of strategy name → flat metrics dict.
        targets_met:          Mapping of strategy name → whether targets were met.

    Returns:
        List of log lines forming the summary table.
    """
    ref_header = ""
    if reference_size_bytes is not None:
        ref_header = f"  (reference: {_fmt_size_mb(reference_size_bytes)} MB)"

    # Column widths
    strategy_col_w = max((len(s) for s in output_files), default=8)
    strategy_col_w = max(strategy_col_w, 8)
    size_col_w     = 9   # "Size (MB)"
    saved_col_w    = 7   # "Saved"

    # Build target column headers: "vmaf-min≥85"
    target_headers = [str(t) for t in quality_targets]
    target_col_ws  = [max(len(h), 11) for h in target_headers]

    # Header row
    header_parts = [
        f"  {'Strategy':<{strategy_col_w}}",
        f"  {'Size (MB)':>{size_col_w}}",
    ]
    if reference_size_bytes is not None:
        header_parts.append(f"  {'Saved':>{saved_col_w}}")
    for h, w in zip(target_headers, target_col_ws):
        header_parts.append(f"  {h:>{w}}")

    sep_parts = [
        f"  {'─' * strategy_col_w}",
        f"  {'─' * size_col_w}",
    ]
    if reference_size_bytes is not None:
        sep_parts.append(f"  {'─' * saved_col_w}")
    for w in target_col_ws:
        sep_parts.append(f"  {'─' * w}")

    lines: list[str] = [
        THICK_LINE,
        f"MERGE SUMMARY{ref_header}",
        THICK_LINE,
        "".join(header_parts),
        "".join(sep_parts),
    ]

    for strategy in sorted(output_files):
        output_file  = output_files[strategy]
        size_b       = sizes_bytes.get(strategy, 0)
        metrics      = final_metrics.get(strategy, {})
        met          = targets_met.get(strategy, False)

        size_str    = _fmt_size_mb(size_b)
        row_parts   = [
            f"  {strategy:<{strategy_col_w}}",
            f"  {size_str:>{size_col_w}}",
        ]
        if reference_size_bytes is not None:
            savings_str = _fmt_savings(size_b, reference_size_bytes)
            row_parts.append(f"  {savings_str:>{saved_col_w}}")
        for target, w in zip(quality_targets, target_col_ws):
            val_str = _fmt_target_value(target, metrics, met)
            row_parts.append(f"  {val_str:>{w}}")

        lines.append("".join(row_parts))

    lines.append(THICK_LINE)
    return lines
