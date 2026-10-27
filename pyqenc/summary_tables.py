"""The unified comparison table — one persisted row shape, one renderer
(unified-quality-summaries spec).

Owns ``StrategyRow`` (THE row shape persisted by ``optimization.yaml`` and
``merge.yaml``) and the comparison renderer: the single place the table's
cell text exists. Phases assemble row populations, the comparison basis
(the quality targets or the anchor), and marks; the
renderer composes every cell — the folded size cell, the sub-column delta
pairs, the anchor/targets cues, the absolute-pair markers, the basis and
legend lines — and hands finished rows to ``format_table``. Column widths
are value-driven: the header phrase rides above the values without
inflating the statistic sub-widths.

Mechanics contract (Req 6): processing runs aggregate facts into rows and
persist them; fast-exit runs load the rows and re-render. Deltas, %Δ, and
marks exist only at render time — persisted rows stay absolute facts.
"""
# CHerSun 2026

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from pyqenc.models import QualityTarget
from pyqenc.quality_metrics import (
    FIXED_COMPARISON_PAIRS,
    MetricStatKey,
    MetricType,
    StatisticType,
    TargetedMetrics,
    TargetedMetricsYaml,
)
from pyqenc.utils.log_format import fmt_size_mb
from pyqenc.utils.tables import Column, format_table

# ---------------------------------------------------------------------------
# Presentation constants (Req 5.2 — named, switchable without renderer changes)
# ---------------------------------------------------------------------------

ANCHOR_GLYPH = "◎"
"""Marks the row every delta reads against."""

SOURCE_CUE = "(src)"
"""The source row's size sub-cell, in place of a self-delta."""

ANCHOR_SUFFIX = "(anchor)"
"""Appended to the anchor strategy's name."""

MISSING_VALUE = "—"
"""Rendered where a compared statistic is absent from a row."""

ABSOLUTE_MARKER = "◆"
"""Prefixes an absolute-value pair (``◆ 93.2 | 97.1``) — pure attention,
semantically empty; deltas carry their ``+``/``-`` signs instead."""

NEGLIGIBLE_GLYPH = "≈"
"""Prefixes deltas inside the metric's negligible band (acceptance_delta)."""

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


# ---------------------------------------------------------------------------
# The persisted row shape (Req 1.3)
# ---------------------------------------------------------------------------

class StrategyRow(BaseModel):
    """One per-strategy row of a comparison table — THE persisted shape.

    Breadth is the per-phase caveat (Req 6.8): optimization rows carry the
    full worst-chunk statistics (the anchor-targets re-derivation substrate); merge
    rows carry the rendered basis only. The renderer tolerates missing
    keys with ``—``.

    Attributes:
        strategy:   Display name of the strategy.
        size_bytes: Total size of the population's outputs (bytes).
        metrics:    The targeted metric subset — the role instance in
                    memory, canonical ``vmaf_median`` string keys on disk.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    strategy:   str
    size_bytes: int
    metrics:    TargetedMetricsYaml = Field(default_factory=TargetedMetrics)


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

    The comparison basis is exactly one of ``targets`` (search mode) and
    ``anchor`` (fixed mode) — mutually exclusive; a table with neither is
    uncompared and renders absolutes.

    Attributes:
        rows:               Strategy rows (the renderer sorts by size
                            ascending; ties break by name).
        targets:            The configured quality targets (search mode) —
                            the search-run basis.
        anchor:             The anchor strategy's display name (fixed mode)
                            — the fixed-run basis.
        source:             The source row (merge only; carries the target
                            values in search mode — its own metric cells are
                            undefined by construction).
        marks:              Marks per strategy name.
        mark_legend:        The ``◀`` legend word for the convention line
                            (``selected`` at optimization, ``recommended`` at
                            merge).
        size_tolerance_pct: The size tolerance band (percent) — stated in
                            the legend; in-band positive size deltas
                            render ``≈``.
    """

    rows:                list[StrategyRow]
    targets:             tuple[QualityTarget, ...] | None = None
    anchor:              str | None                  = None
    source:              SourceInfo | None           = None
    marks:               Mapping[str, Mark] = field(default_factory=dict)
    mark_legend:         str = "selected"
    size_tolerance_pct:  float | None = None


# ---------------------------------------------------------------------------
# Number formatting (Req 2.8 — uniform)
# ---------------------------------------------------------------------------

def _format_metric(value: float) -> str:
    """One decimal, no sign — an ABSOLUTE value (the pair-level ``◆`` marker
    distinguishes absolutes from ``+``/``-`` deltas)."""
    return f"{value:.1f}"


def _format_delta(value: float, negligible_band: float) -> str:
    """One decimal, signed — the negligible band renders ``≈`` instead.

    True zero renders bare (``0.0``); a nonzero delta inside
    *negligible_band* renders ``≈``-prefixed with the sign dropped at
    zero (``≈0.0``, ``≈0.1``, ``≈-0.1``); everything else keeps the sign
    (``+0.4``, ``-1.2``, ``+0.0``).
    """
    if value == 0.0:
        return "0.0"
    if abs(value) < negligible_band:
        text = f"{value:.1f}"
        return f"{NEGLIGIBLE_GLYPH}{'0.0' if text == '-0.0' else text}"
    return f"{value:+.1f}"


def _format_pct_delta(value: float, tolerance_pct: float | None) -> str:
    """The size sub-cell's percent delta — one decimal, signed, ``%``.

    A positive delta inside *tolerance_pct* renders ``≈``-prefixed (the
    one-sided selection band the tolerance keeps); everything else keeps
    the sign, and zero-rounding outside the band stays bare.
    """
    if tolerance_pct is not None and 0 < value < tolerance_pct:
        return f"{NEGLIGIBLE_GLYPH}{value:.1f}%"
    if round(value, 1) == 0.0:
        return "0.0%"
    return f"{value:+.1f}%"


# ---------------------------------------------------------------------------
# Metric columns (Req 2.4 — the compared statistics per metric)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _MetricColumn:
    """One metric's compared statistics and reference values.

    Attributes:
        metric:          The metric.
        stats:           Compared statistics in the closed set's order.
        reference_values: The comparison basis' absolute value per statistic
                         (the targets' values in search, the anchor's in
                         fixed; empty for uncompared rendering).
    """

    metric:           MetricType
    stats:            tuple[StatisticType, ...]
    reference_values: dict[StatisticType, float]


_STAT_ORDER = {statistic: index for index, statistic in enumerate(StatisticType)}
"""The closed set's declaration order (headers list stats in it)."""

_METRIC_ORDER = {metric: index for index, metric in enumerate(MetricType)}
"""The canonical metric column order (independent of dict arrival order)."""


def _metric_columns(table: ComparisonTable) -> list[_MetricColumn]:
    """Derive the compared statistics per metric from the comparison basis."""
    if table.targets is not None:
        by_metric: dict[MetricType, dict[StatisticType, float]] = {}
        for target in table.targets:
            key = target.identity
            by_metric.setdefault(key.metric, {})[key.statistic] = target.value
        return sorted(
            (
                _MetricColumn(
                    metric           = metric,
                    stats            = tuple(sorted(values, key=_STAT_ORDER.__getitem__)),
                    reference_values = dict(values),
                )
                for metric, values in by_metric.items()
            ),
            key=lambda c: _METRIC_ORDER[c.metric],
        )
    # Fixed mode (anchor) and uncompared rendering compare the fixed
    # comparison pairs, of every metric the carrier holds. The carrier
    # is the anchor row; uncompared falls back to the union across rows
    # (absolute rendering of what the population measured).
    carrier: TargetedMetrics = TargetedMetrics()
    if table.anchor is not None:
        anchor_row = next((r for r in table.rows if r.strategy == table.anchor), None)
        if anchor_row is not None:
            carrier = anchor_row.metrics
    else:
        union: dict[MetricStatKey, float] = {}
        for row in table.rows:
            union.update(row.metrics)
        carrier = TargetedMetrics(union)
    metrics = sorted(
        {key.metric for key in carrier if key in FIXED_COMPARISON_PAIRS},
        key=_METRIC_ORDER.__getitem__,
    )
    return [
        _MetricColumn(
            metric           = metric,
            stats            = tuple(
                pair.statistic for pair in FIXED_COMPARISON_PAIRS if pair.metric == metric
            ),
            reference_values = {
                key.statistic: value
                for key, value in carrier.items()
                if key.metric == metric and key in FIXED_COMPARISON_PAIRS
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
    pct = (row.size_bytes - basis_size) / basis_size * 100
    return f"({_format_pct_delta(pct, table.size_tolerance_pct)})"


def _metric_sub_values(
    column: _MetricColumn,
    row:    StrategyRow | None,
    table:  ComparisonTable,
) -> dict[StatisticType, str]:
    """One line's rendered sub-values for a metric column.

    Args:
        column: The metric column (stats + reference values).
        row:    The strategy row, or ``None`` for the carrier line
                (the source row at merge / the targets line at optimization).
        table:  The table (the comparison basis; uncompared renders
                absolutes).
    """
    if row is None:
        # The carrier line shows the basis' absolutes: the target
        # values (search); fixed mode has none here (the anchor row — a
        # strategy row — carries them).
        if table.targets is not None:
            return _mark_absolute(column, {
                s: _format_metric(v) for s, v in column.reference_values.items()
            })
        return {s: MISSING_VALUE for s in column.stats}
    if table.targets is None and table.anchor is None:
        # Uncompared tables render absolutes UNMARKED — the marker's job is
        # distinguishing absolutes from deltas, and there are no deltas.
        return {
            s: _format_metric(row.metrics[MetricStatKey(column.metric, s)])
            if MetricStatKey(column.metric, s) in row.metrics
            else MISSING_VALUE
            for s in column.stats
        }
    is_anchor_row = table.anchor is not None and row.strategy == table.anchor
    rendered: dict[StatisticType, str] = {}
    for s in column.stats:
        measured = row.metrics.get(MetricStatKey(column.metric, s))
        if measured is None:
            rendered[s] = MISSING_VALUE
        elif is_anchor_row or s not in column.reference_values:
            rendered[s] = _format_metric(measured)
        else:
            rendered[s] = _format_delta(
                measured - column.reference_values[s],
                column.metric.info.acceptance_delta,
            )
    return _mark_absolute(column, rendered) if is_anchor_row else rendered


def _mark_absolute(
    column: _MetricColumn,
    values: dict[StatisticType, str],
) -> dict[StatisticType, str]:
    """Prefix the pair's first statistic with the absolute marker.

    ``◆ value1 | value2`` — one attention glyph before the pair; only the
    first statistic is prefixed, and the value-driven sub-widths absorb it.
    """
    if not values:
        return values
    first = column.stats[0]
    if first in values:
        values[first] = f"{ABSOLUTE_MARKER} {values[first]}"
    return values


def _compose_sub_columns(
    column:     _MetricColumn,
    sub_widths: Mapping[StatisticType, int],
    line_values: Mapping[StatisticType, str],
) -> str:
    """Compose one DATA line of a metric column.

    The values ride UNDER the metric name — no reserved name zone on data
    lines. Sub-values right-align in value-driven sub-widths, so every
    data line has the same length and the ``|`` dividers align across
    data rows (the header's own pipe may sit further right, under the
    name overhang).
    """
    parts = [
        line_values.get(statistic, MISSING_VALUE).rjust(sub_widths[statistic])
        for statistic in column.stats
    ]
    return SUB_COLUMN_SEPARATOR.join(parts)


def _metric_header_text(
    column:     _MetricColumn,
    sub_widths: Mapping[StatisticType, int],
) -> str:
    """The metric column's header cell — the name, then the stat labels.

    The first label sits flush after the name (bare); later labels
    right-align in their value-driven sub-widths — the header reads as one
    compact phrase while the column's right edge still matches the data.
    """
    name_zone = f"{column.metric.info.name}  "
    labels = [
        statistic.display if index == 0 else statistic.display.rjust(sub_widths[statistic])
        for index, statistic in enumerate(column.stats)
    ]
    return name_zone + SUB_COLUMN_SEPARATOR.join(labels)


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------

def _convention_lines(table: ComparisonTable) -> list[str]:
    """The lines above the table: the mode's basis line, then the legend.

    Fixed runs name the elected anchor, search runs the configured targets
    (naming the targeted metrics); uncompared tables carry no basis line.
    The legend line states the delta bases, the negligible-band glyph, the
    mark legend, and the size tolerance when set.
    """
    compared = table.targets is not None or table.anchor is not None
    size_basis = f"size Δ% vs {'source' if table.source is not None else 'best size'}"
    approx_active = compared or table.size_tolerance_pct is not None
    if not compared:
        head = []
        segments = [size_basis]
    elif table.targets is not None:
        metrics = " · ".join(
            metric.info.name
            for metric in MetricType
            if any(t.identity.metric == metric for t in table.targets)
        )
        head = [f"targets: {ANCHOR_GLYPH}  configured ({metrics})"]
        segments = ["Δ vs targets", size_basis]
    else:
        head = [f"anchor: {ANCHOR_GLYPH}  {table.anchor}"]
        segments = ["Δ vs anchor", size_basis]
    if approx_active:
        segments.append(f"{NEGLIGIBLE_GLYPH} negligible")
    segments.append(f"{SELECTED_GLYPH} {table.mark_legend}")
    if table.size_tolerance_pct is not None:
        segments.append(f"size tolerance {table.size_tolerance_pct:g}%")
    return [*head, CONVENTION_SEPARATOR.join(segments)]


def render_comparison_table(table: ComparisonTable) -> list[str]:
    """Render the full table block: convention lines, blank, table, blank.

    Args:
        table: The complete render input (rows, basis, source, marks).

    Returns:
        Lines ending with one blank line (Req 1.6 — anything after comes
        from the caller, below the table). The two convention lines (basis
        and bases, then legend) sit one blank line above the table.
    """
    compared_by_targets = table.targets is not None
    sorted_rows = sorted(table.rows, key=lambda r: (r.size_bytes, r.strategy))
    metric_columns = _metric_columns(table)

    # --- metric lines: sub-values per (line, column), then shared widths ---
    has_carrier_line = table.source is not None or compared_by_targets
    carrier_values = (
        [_metric_sub_values(c, None, table) for c in metric_columns]
        if has_carrier_line else []
    )
    row_values = [[_metric_sub_values(c, row, table) for c in metric_columns] for row in sorted_rows]

    scan_lines = ([carrier_values] if carrier_values else []) + row_values
    sub_widths_per: list[dict[StatisticType, int]] = []
    for ci, column in enumerate(metric_columns):
        widths: dict[StatisticType, int] = {}
        for statistic in column.stats:
            # Value-driven widths only — the metric name lives in its own
            # left zone and never inflates the statistic widths.
            candidates = [len(statistic.display)]
            for line in scan_lines:
                candidates.append(len(line[ci].get(statistic, MISSING_VALUE)))
            widths[statistic] = max(candidates)
        sub_widths_per.append(widths)

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
    mb_width = max((len(s) for s in mb_strings), default=1)
    sub_width = max([len(SOURCE_CUE), *(len(s) for s in size_cells)], default=len(SOURCE_CUE))

    def _size_cell(mb: str, sub: str) -> str:
        return f"{mb:>{mb_width}} {sub:>{sub_width}}"

    table_rows: list[list[str]] = []

    # --- the carrier line (source row at merge / targets line at opt) ---
    if table.source is not None:
        name = f"{table.source.stem} (src)"
        if compared_by_targets:
            name = f"{ANCHOR_GLYPH} {name}"
        source_cell = _size_cell(fmt_size_mb(table.source.size_bytes), SOURCE_CUE)
        table_rows.append([
            name,
            source_cell,
            *[_compose_sub_columns(c, sub_widths_per[i], carrier_values[i])
              for i, c in enumerate(metric_columns)],
            "",
        ])
    elif compared_by_targets:
        table_rows.append([
            f"{ANCHOR_GLYPH} targets",
            _size_cell("", ""),
            *[_compose_sub_columns(c, sub_widths_per[i], carrier_values[i])
              for i, c in enumerate(metric_columns)],
            "",
        ])

    # --- strategy rows ---
    for index, (row, values) in enumerate(zip(sorted_rows, row_values)):
        name = row.strategy
        if table.anchor is not None and row.strategy == table.anchor:
            name = f"{ANCHOR_GLYPH} {name} {ANCHOR_SUFFIX}"
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
            Column(_metric_header_text(column, sub_widths_per[ci]), align="right")
        )
    columns_def.append(Column("", align="right"))

    lines = [
        "",
        *(f"  {line}" for line in _convention_lines(table)),
        "",
        *format_table(columns_def, table_rows, indent="  "),
        "",
    ]
    return lines
