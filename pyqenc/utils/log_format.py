"""
Log formatting helpers for uniform chunk attempt and optimization output.

All public functions return plain strings or lists of strings — no logging
side-effects — so callers decide the log level.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pyqenc.constants import (
    BRACKET_LEFT,
    BRACKET_RIGHT,
    FAILURE_SYMBOL_MAJOR,
    PADDING_CRF,
    SUCCESS_SYMBOL_MAJOR,
    THICK_LINE,
    THIN_LINE,
)

if TYPE_CHECKING:
    from pyqenc.phases.optimization import StrategyTestResult

logger = logging.getLogger(__name__)


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

def fmt_optimization_summary(
    optimal:  str,
    results:  dict[str, StrategyTestResult],
) -> list[str]:
    """Return a visually distinct final optimization summary block.

    Includes the winning strategy and a comparison table of all strategies
    sorted by average file size ascending.  Bordered by ``═`` delimiter lines.

    Args:
        optimal:  Name of the selected optimal strategy.
        results:  Mapping of strategy name → :class:`StrategyTestResult`.

    Returns:
        List of log lines.
    """
    lines: list[str] = [
        THICK_LINE,
        "OPTIMIZATION SUMMARY",
        THICK_LINE,
        f"  Optimal strategy : {optimal}",
        "",
        "  Comparison (all strategies, sorted by size):",
        f"  {'Strategy':<30}  {'Avg CRF':>8}  {'Size (MB)':>10}  {'Status':>8}",
        f"  {'-'*30}  {'-'*8}  {'-'*10}  {'-'*8}",
    ]

    sorted_results = sorted(
        results.values(),
        key=lambda r: r.avg_file_size if r.avg_file_size > 0 else float("inf"),
    )

    for res in sorted_results:
        marker    = " ◀ optimal" if res.strategy == optimal else ""
        status    = "passed" if res.all_passed else "failed"
        size_mb   = res.avg_file_size / (1024 * 1024) if res.avg_file_size > 0 else 0.0
        lines.append(
            f"  {res.strategy:<30}  {res.avg_crf:>8.2f}  {size_mb:>10.2f}  {status:>8}{marker}"
        )

    lines.append(THICK_LINE)
    return lines

def fmt_key_value_table(kv_to_show):
    """Format a dictionary of key-value pairs into aligned log lines for display as a table."""
    max_key_len = max(len(k) for k in kv_to_show.keys())+1
    for key, value in kv_to_show.items():
        logger.info(f"{key:<{max_key_len}} {value}")
