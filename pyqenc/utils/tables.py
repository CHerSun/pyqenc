"""Plain-text table formatting — the single alignment mechanism (TODO §119).

Logger-plain by contract: no ANSI, no borders, 2-space column gaps, dash
rule rows under the headers. Widths are ``len()``-based, so cells must
contain only NARROW (single-cell) glyphs — wide or ambiguous-width glyphs
are out of scope for table cells by convention.

Two structural capabilities beyond plain columns:

- **Bare columns** (empty ``header``): content-only zones — no header text
  and no rule-row segment (blank space instead), for low-weight trailing
  zones such as marks.
- **Pipe-preserving rules**: a column whose rendered cells contain ``|``
  renders its rule with ``+`` at the same offsets, dashes elsewhere
  (``---------+------``) — the crossing marks the sub-column break while
  keeping the visual emphasis on the column; data rows keep `` | ``. The
  template is the column's first piped data cell (header as fallback) —
  data rows define the sub-column structure.
"""
# CHerSun 2026

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

COLUMN_GAP = "  "
"""The gap between adjacent columns."""

RULE_CHAR = "-"
"""The dash character filling rule rows."""

RULE_CROSSING_CHAR = "+"
"""Marks a sub-column divider crossing in rule rows (data rows keep `` | ``)."""

TRUNCATION_ELLIPSIS = "…"
"""Marks the cut point in truncated cells."""

_SUFFIX_GROUP = re.compile(r"\s\([^()]*\)\s*$")
"""A trailing parenthesized group — `h265+ultrafast (anchor)` — kept intact by truncation."""


@dataclass(frozen=True)
class Column:
    """One column's rendering contract.

    Attributes:
        header:    Header text; the empty string marks a BARE column
                   (content-only: no header text, no rule-row segment).
        align:     Cell and header alignment.
        min_width: Width floor (dynamic width never drops below it).
        max_width: Width ceiling; longer cells truncate suffix-safely (a
                   trailing parenthesized group always survives).
    """

    header:    str
    align:     Literal["left", "right"] = "left"
    min_width: int = 0
    max_width: int | None = None


def _truncate(cell: str, width: int) -> str:
    """Shorten *cell* to *width*, keeping a trailing parenthesized suffix intact."""
    if len(cell) <= width:
        return cell
    suffix = _SUFFIX_GROUP.search(cell)
    suffix_text = suffix.group(0) if suffix else ""
    head_width = width - len(suffix_text) - len(TRUNCATION_ELLIPSIS)
    if head_width <= 0:
        return cell[:width]
    return cell[:head_width].rstrip() + TRUNCATION_ELLIPSIS + suffix_text


def _rule_segment(column: Column, cells: Sequence[str], width: int) -> str:
    """The column's rule-row span: dashes, with ``+`` where a piped cell has one."""
    if not column.header:
        return " " * width
    template = next((c for c in (*cells, column.header) if "|" in c), None)
    if template is None:
        return RULE_CHAR * width
    aligned = template.rjust(width) if column.align == "right" else template.ljust(width)
    return "".join(RULE_CROSSING_CHAR if ch == "|" else RULE_CHAR for ch in aligned)


def format_table(
    columns: Sequence[Column],
    rows:     Sequence[Sequence[str]],
    *,
    indent:   str = "  ",
) -> list[str]:
    """Render a plain-text table: header line, rule line, then one line per row.

    Args:
        columns: The column contracts, in order; a row's cells must match
                 them one-to-one.
        rows:    Pre-formatted cell strings per row (compound cells — sizes
                 with deltas, sub-column pairs — arrive already composed).
        indent:  Prefix for every line.

    Returns:
        The table as lines, each right-stripped (trailing bare-column space
        never survives).
    """
    assert all(len(row) == len(columns) for row in rows), \
        f"row arity must match columns ({len(columns)})"

    widths:  list[int] = []
    col_cells: list[list[str]] = []
    for ci, column in enumerate(columns):
        cells = [row[ci] for row in rows]
        if column.max_width is not None:
            cells = [_truncate(cell, column.max_width) for cell in cells]
        width = max([column.min_width, *(len(cell) for cell in cells)])
        if column.header:
            width = max(width, len(column.header))
        widths.append(width)
        col_cells.append(cells)

    def _cell(text: str, column: Column, width: int) -> str:
        return text.rjust(width) if column.align == "right" else text.ljust(width)

    def _line(cells: Sequence[str]) -> str:
        return (indent + COLUMN_GAP.join(cells)).rstrip()

    header_cells = [
        _cell(column.header, column, width) if column.header else " " * width
        for column, width in zip(columns, widths)
    ]
    lines = [_line(header_cells)]
    lines.append(_line([
        _rule_segment(column, col_cells[ci], width)
        for ci, (column, width) in enumerate(zip(columns, widths))
    ]))
    for row_index in range(len(rows)):
        lines.append(_line([
            _cell(col_cells[ci][row_index], column, width)
            for ci, (column, width) in enumerate(zip(columns, widths))
        ]))
    return lines
