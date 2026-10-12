"""The unified comparison table — one persisted row shape, one renderer
(unified-quality-summaries spec).

Owns ``StrategyRow`` (THE row shape persisted by ``optimization.yaml`` and
``merge.yaml``) and the comparison renderer: the single place the table's
cell text exists. Phases assemble row populations, rulers, and marks; the
renderer composes every cell — the folded size cell, the sub-column delta
pairs, the ruler cues, the convention line — and hands finished rows to
``format_table``.

Mechanics contract (Req 6): processing runs aggregate facts into rows and
persist them; fast-exit runs load the rows and re-render. Deltas, %Δ, and
marks exist only at render time — persisted rows stay absolute facts.
"""
# CHerSun 2026

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from pyqenc.constants import FIXED_COMPARISON_STATS
from pyqenc.models import QualityTarget
from pyqenc.quality import (
    MetricStatKey,
    MetricStatMap,
    MetricType,
    StatisticType,
)
from pyqenc.utils.log_format import fmt_size_mb
from pyqenc.utils.tables import Column, format_table

# ---------------------------------------------------------------------------
# Presentation constants (Req 5.2 — named, switchable without renderer changes)
# ---------------------------------------------------------------------------

RULER_GLYPH = "◎"
"""Marks the row every delta reads against."""

SOURCE_CUE = "(src)"
"""The source row's size sub-cell, in place of a self-delta."""

ANCHOR_SUFFIX = "(anchor)"
"""Appended to the anchor strategy's name."""

MISSING_VALUE = "—"
"""Rendered where a compared statistic is absent from a row."""

SELECTED_GLYPH = "◀"
"""Selection mark (optimization) / recommendation mark (merge)."""

VERDICT_PASS_GLYPH = "✔"
VERDICT_MISS_GLYPH = "✘"
"""The live quality verdict beside the glyph (search merge only)."""

NAME_HEADER = "Strategy"
NAME_MAX_WIDTH = 30
"""Ceiling for the strategy-name column; suffix-safe truncation applies."""

SIZE_HEADER = "Size (MB)"

CONVENTION_SEPARATOR = " · "
"""Between the convention line's segments."""

SUB_COLUMN_SEPARATOR = " | "
"""Between a metric column's compared statistics (Req 2.8 — the divider)."""

COMPARISON_STATS: tuple[StatisticType, ...] = tuple(
    StatisticType(token) for token in FIXED_COMPARISON_STATS
)
"""The fixed-mode compared statistics (search compares the targeted set)."""


# ---------------------------------------------------------------------------
# The persisted row shape (Req 1.3)
# ---------------------------------------------------------------------------

class StrategyRow(BaseModel):
    """One per-strategy row of a comparison table — THE persisted shape.

    Breadth is the per-phase caveat (Req 6.8): optimization rows carry the
    full worst-chunk statistics (the ruler re-derivation substrate); merge
    rows carry the rendered basis only. The renderer tolerates missing
    keys with ``—``.

    Attributes:
        strategy:   Display name of the strategy.
        size_bytes: Total size of the population's outputs (bytes).
        metrics:    Measured values keyed by the metric-statistic pair type —
                    typed keys in memory, canonical strings on disk.
    """

    strategy:   str
    size_bytes: int
    metrics:    MetricStatMap = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Render-time composition (never persisted)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceInfo:
    """The source row (merge populations only).

    Attributes:
        stem:       Source filename stem.
        size_bytes: Source size in bytes.
    """

    stem:       str
    size_bytes: int


@dataclass(frozen=True)
class Ruler:
    """What every delta reads against (Req 2).

    Attributes:
        kind:    ``targets`` (search — the configured quality targets) or
                 ``anchor`` (fixed — a strategy from the same rows).
        targets: The target set (``kind == "targets"``).
        anchor:  The anchor strategy's display name (``kind == "anchor"``).
    """

    kind:    Literal["targets", "anchor"]
    targets: tuple[QualityTarget, ...] = ()
    anchor:  str = ""


@dataclass(frozen=True)
class Mark:
    """One strategy's marks (Req 4 — data-driven, derived by the phase).

    Attributes:
        selected: Carries the ``◀`` selection/recommendation glyph.
        verdict:  ``True``/``False`` render the live quality verdict beside
                  the glyph (search merge only); ``None`` renders nothing.
    """

    selected: bool = False
    verdict:  bool | None = None


@dataclass(frozen=True)
class ComparisonTable:
    """The renderer's complete input (Req 6.3 — typed models only).

    Attributes:
        rows:               Strategy rows (the renderer sorts by size
                            ascending; ties break by name).
        ruler:              The ruler (targets in search, anchor in fixed),
                            or ``None`` for uncompared absolute rendering.
        source:             The source row (merge only; carries the target
                            values in search mode — its own metric cells are
                            undefined by construction).
        marks:              Marks per strategy name.
        mark_legend:        The ``◀`` legend word for the convention line
                            (``selected`` at optimization, ``recommended`` at
                            merge).
        size_tolerance_pct: When set, stated in the convention line so cut
                            rows' size deltas read against it.
    """

    rows:               list[StrategyRow]
    ruler:              Ruler | None
    source:             SourceInfo | None = None
    marks:              Mapping[str, Mark] = field(default_factory=dict)
    mark_legend:        str = "selected"
    size_tolerance_pct: float | None = None


# ---------------------------------------------------------------------------
# Number formatting (Req 2.8 — uniform)
# ---------------------------------------------------------------------------

def _format_metric(value: float) -> str:
    """One decimal, no sign — the ruler row's absolute values."""
    return f"{value:.1f}"


def _format_delta(value: float) -> str:
    """One decimal, signed — zero renders bare (``0.0``)."""
    if round(value, 1) == 0.0:
        return "0.0"
    return f"{value:+.1f}"


def _format_pct_delta(value: float) -> str:
    """The size sub-cell's percent delta — one decimal, signed, ``%``."""
    if round(value, 1) == 0.0:
        return "0.0%"
    return f"{value:+.1f}%"


# ---------------------------------------------------------------------------
# Metric columns (Req 2.4 — the compared statistics per metric)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _MetricColumn:
    """One metric's compared statistics and ruler values.

    Attributes:
        metric:       The metric.
        stats:        Compared statistics in the closed set's order.
        ruler_values: The ruler's absolute value per statistic (empty for
                      ruler-less rendering).
    """

    metric:       MetricType
    stats:        tuple[StatisticType, ...]
    ruler_values: dict[StatisticType, float]


_STAT_ORDER = {statistic: index for index, statistic in enumerate(StatisticType)}
"""The closed set's declaration order (headers list stats in it)."""

_METRIC_ORDER = {metric: index for index, metric in enumerate(MetricType)}
"""The canonical metric column order (independent of dict arrival order)."""


def _metric_columns(table: ComparisonTable) -> list[_MetricColumn]:
    """Derive the compared statistics per metric from the ruler."""
    ruler = table.ruler
    if ruler is not None and ruler.kind == "targets":
        by_metric: dict[MetricType, dict[StatisticType, float]] = {}
        for target in ruler.targets:
            key = target.key()
            by_metric.setdefault(key.metric, {})[key.statistic] = target.value
        return sorted(
            (
                _MetricColumn(
                    metric       = metric,
                    stats        = tuple(sorted(values, key=_STAT_ORDER.__getitem__)),
                    ruler_values = dict(values),
                )
                for metric, values in by_metric.items()
            ),
            key=lambda c: _METRIC_ORDER[c.metric],
        )
    # Fixed mode (anchor) and ruler-less rendering compare the fixed
    # comparison statistics, of every metric the carrier holds. The carrier
    # is the anchor row; ruler-less falls back to the union across rows
    # (absolute rendering of what the population measured).
    carrier: MetricStatMap = {}
    if ruler is not None and ruler.kind == "anchor":
        anchor_row = next((r for r in table.rows if r.strategy == ruler.anchor), None)
        if anchor_row is not None:
            carrier = anchor_row.metrics
    else:
        for row in table.rows:
            carrier = {**carrier, **row.metrics}
    metrics = sorted(
        {key.metric for key in carrier if key.statistic in COMPARISON_STATS},
        key=_METRIC_ORDER.__getitem__,
    )
    return [
        _MetricColumn(
            metric       = metric,
            stats        = COMPARISON_STATS,
            ruler_values = {
                key.statistic: value
                for key, value in carrier.items()
                if key.metric == metric and key.statistic in COMPARISON_STATS
            },
        )
        for metric in metrics
    ]


# ---------------------------------------------------------------------------
# Cell composition
# ---------------------------------------------------------------------------

def _size_sub_value(table: ComparisonTable, row: StrategyRow, basis_size: int, is_basis: bool) -> str:
    """The size cell's parenthesized sub-value (Req 2.5).

    The basis row carries no self-delta; the merge source shows the cue in
    its own line instead (composed by the caller).
    """
    if is_basis:
        return ""
    if basis_size <= 0:
        return MISSING_VALUE
    return f"({_format_pct_delta((row.size_bytes - basis_size) / basis_size * 100)})"


def _metric_sub_values(
    column: _MetricColumn,
    row:    StrategyRow | None,
    ruler:  Ruler | None,
) -> dict[StatisticType, str]:
    """One line's rendered sub-values for a metric column.

    Args:
        column: The metric column (stats + ruler values).
        row:    The strategy row, or ``None`` for the ruler-carrier line
                (the source row at merge / the targets row at optimization).
        ruler:  The table's ruler (``None`` renders absolutes).
    """
    if row is None:
        # The ruler-carrier line shows the ruler's absolutes: the target
        # values (search); fixed mode has none here (the anchor row — a
        # strategy row — carries them).
        if ruler is not None and ruler.kind == "targets":
            return {s: _format_metric(v) for s, v in column.ruler_values.items()}
        return {s: MISSING_VALUE for s in column.stats}
    if ruler is None:
        return {
            s: _format_metric(row.metrics[MetricStatKey(column.metric, s)])
            if MetricStatKey(column.metric, s) in row.metrics
            else MISSING_VALUE
            for s in column.stats
        }
    is_ruler_row = ruler.kind == "anchor" and row.strategy == ruler.anchor
    rendered: dict[StatisticType, str] = {}
    for s in column.stats:
        measured = row.metrics.get(MetricStatKey(column.metric, s))
        if measured is None:
            rendered[s] = MISSING_VALUE
        elif is_ruler_row or s not in column.ruler_values:
            rendered[s] = _format_metric(measured)
        else:
            rendered[s] = _format_delta(measured - column.ruler_values[s])
    return rendered


def _compose_sub_columns(
    column:     _MetricColumn,
    sub_widths: Mapping[StatisticType, int],
    line_values: Mapping[StatisticType, str] | None,
) -> str:
    """Compose one line of a metric column from its sub-values.

    The header line (``line_values is None``) prefixes the metric name to
    the first statistic's label; every line shares the same sub-widths, so
    the dividers align vertically (Req 2.8) and header/cell totals match.
    """
    parts: list[str] = []
    for index, statistic in enumerate(column.stats):
        width = sub_widths[statistic]
        if line_values is None:
            label = f"{statistic.display:>{width}}"
            if index == 0:
                label = f"{column.metric.info.name} {label}"
            parts.append(label)
        else:
            parts.append(line_values.get(statistic, MISSING_VALUE).rjust(width))
    return SUB_COLUMN_SEPARATOR.join(parts)


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------

def _convention_line(table: ComparisonTable) -> str:
    """The line above the table: ruler identity, bases, mark legend (Req 2.9)."""
    ruler = table.ruler
    size_basis = "source" if table.source is not None else "best size"
    if ruler is None:
        segments = ["ruler: none — absolutes", f"size Δ% vs {size_basis}"]
    else:
        if ruler.kind == "targets":
            ruler_text, delta_text = f"configured targets {RULER_GLYPH}", "targets"
        else:
            ruler_text, delta_text = f"{ruler.anchor} (anchor) {RULER_GLYPH}", "anchor"
        segments = [
            f"ruler: {ruler_text}",
            f"Δ vs {delta_text}",
            f"size Δ% vs {size_basis}",
        ]
    segments.append(f"{SELECTED_GLYPH} {table.mark_legend}")
    if table.size_tolerance_pct is not None:
        segments.append(f"size tolerance {table.size_tolerance_pct:g}%")
    return CONVENTION_SEPARATOR.join(segments)


def render_comparison_table(table: ComparisonTable) -> list[str]:
    """Render the full table block: convention line, table, blank line.

    Args:
        table: The complete render input (rows, ruler, source, marks).

    Returns:
        Lines ending with one blank line (Req 1.6 — anything after comes
        from the caller, below the table).
    """
    ruler = table.ruler
    sorted_rows = sorted(table.rows, key=lambda r: (r.size_bytes, r.strategy))
    metric_columns = _metric_columns(table)

    # --- metric lines: sub-values per (line, column), then shared widths ---
    has_carrier_line = table.source is not None or (ruler is not None and ruler.kind == "targets")
    carrier_values = (
        [_metric_sub_values(c, None, ruler) for c in metric_columns]
        if has_carrier_line else []
    )
    row_values = [[_metric_sub_values(c, row, ruler) for c in metric_columns] for row in sorted_rows]

    scan_lines = ([carrier_values] if carrier_values else []) + row_values
    sub_widths_per: list[dict[StatisticType, int]] = []
    for ci, column in enumerate(metric_columns):
        widths: dict[StatisticType, int] = {}
        for statistic in column.stats:
            candidates = [len(statistic.display)]
            for line in scan_lines:
                candidates.append(len(line[ci].get(statistic, MISSING_VALUE)))
            widths[statistic] = max(candidates)
        sub_widths_per.append(widths)

    # First sub-label carries the metric name; widen it into sub-1's width.
    for column, widths in zip(metric_columns, sub_widths_per):
        first = column.stats[0]
        widths[first] = max(widths[first], len(column.metric.info.name) + 1 + len(first.display))

    # --- size cells: fixed-length composition (MB + paren sub-field) ---
    if table.source is not None:
        basis_size = table.source.size_bytes
    elif sorted_rows:
        basis_size = sorted_rows[0].size_bytes
    else:
        basis_size = 0
    size_cells = [
        _size_sub_value(table, row, basis_size, row is sorted_rows[0] and table.source is None)
        for row in sorted_rows
    ]
    mb_strings = [fmt_size_mb(r.size_bytes) for r in sorted_rows]
    mb_width = max([len(SIZE_HEADER), *(len(s) for s in mb_strings)], default=len(SIZE_HEADER))
    sub_width = max([len(SOURCE_CUE), *(len(s) for s in size_cells)], default=len(SOURCE_CUE))

    def _size_cell(mb: str, sub: str) -> str:
        return f"{mb:>{mb_width}} {sub:>{sub_width}}"

    table_rows: list[list[str]] = []

    # --- the ruler-carrier line (source row at merge / targets row at opt) ---
    if table.source is not None:
        name = f"{table.source.stem} (src)"
        if ruler is not None and ruler.kind == "targets":
            name = f"{RULER_GLYPH} {name}"
        source_cell = _size_cell(fmt_size_mb(table.source.size_bytes), SOURCE_CUE)
        table_rows.append([
            name,
            source_cell,
            *[_compose_sub_columns(c, sub_widths_per[i], carrier_values[i])
              for i, c in enumerate(metric_columns)],
            "",
        ])
    elif ruler is not None and ruler.kind == "targets":
        table_rows.append([
            f"{RULER_GLYPH} targets",
            _size_cell("", ""),
            *[_compose_sub_columns(c, sub_widths_per[i], carrier_values[i])
              for i, c in enumerate(metric_columns)],
            "",
        ])

    # --- strategy rows ---
    for index, (row, values) in enumerate(zip(sorted_rows, row_values)):
        name = row.strategy
        if ruler is not None and ruler.kind == "anchor" and row.strategy == ruler.anchor:
            name = f"{RULER_GLYPH} {name} {ANCHOR_SUFFIX}"
        mark = table.marks.get(row.strategy, Mark())
        mark_text = (SELECTED_GLYPH if mark.selected else "") + (
            f" {VERDICT_PASS_GLYPH if mark.verdict else VERDICT_MISS_GLYPH}"
            if mark.verdict is not None else ""
        )
        table_rows.append([
            name,
            _size_cell(mb_strings[index], size_cells[index]),
            *[_compose_sub_columns(c, sub_widths_per[i], values[i])
              for i, c in enumerate(metric_columns)],
            mark_text.strip(),
        ])

    columns_def: list[Column] = [
        Column(NAME_HEADER, max_width=NAME_MAX_WIDTH),
        Column(SIZE_HEADER, align="right"),
    ]
    for ci, column in enumerate(metric_columns):
        columns_def.append(
            Column(_compose_sub_columns(column, sub_widths_per[ci], None), align="right")
        )
    columns_def.append(Column("", align="right"))

    lines = ["", f"  {_convention_line(table)}"]
    lines.extend(format_table(columns_def, table_rows, indent="  "))
    lines.append("")
    return lines
