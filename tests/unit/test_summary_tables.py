"""Unit tests for the unified comparison table (unified-quality-summaries
Req 1, 2, 4, 6).

``StrategyRow`` is THE persisted shape and ``render_comparison_table`` the
single renderer. Tests assert the table's SEMANTIC content — row order,
basis cues, delta signs, marks, caveated missing values, the convention
line, the blank-line contract — not byte-level cosmetics of wide tables.
"""
# CHerSun 2026

from hypothesis import given
from hypothesis import strategies as st

from pyqenc.models import QualityTarget
from pyqenc.quality_metrics import MetricStatKey, MetricType, StatisticType
from pyqenc.summary_tables import (
    ABSOLUTE_MARKER,
    ANCHOR_GLYPH,
    ANCHOR_SUFFIX,
    MISSING_VALUE,
    NAME_HEADER,
    SELECTED_GLYPH,
    SOURCE_CUE,
    ComparisonTable,
    Mark,
    SourceInfo,
    StrategyRow,
    render_comparison_table,
)

K = MetricStatKey

_TARGETS = tuple(QualityTarget.parse(t) for t in [
    "vmaf-med:96.0", "psnr-med:45.0", "vmaf-p10:93.0", "psnr-p10:43.0",
])


def _row(
    name: str,
    size: int,
    *,
    vmaf_med: float = 97.0, vmaf_p10: float = 93.0,
    psnr_med: float = 45.0, psnr_p10: float = 43.0,
) -> StrategyRow:
    return StrategyRow(strategy=name, size_bytes=size, metrics={
        K(MetricType.VMAF, StatisticType.MEDIAN): vmaf_med,
        K(MetricType.VMAF, StatisticType.P10): vmaf_p10,
        K(MetricType.PSNR, StatisticType.MEDIAN): psnr_med,
        K(MetricType.PSNR, StatisticType.P10): psnr_p10,
    })


_ROWS = [
    _row("h265+ultrafast", 456_000_000, vmaf_med=97.1, vmaf_p10=93.2),
    _row("h264+ultrafast", 512_000_000, vmaf_med=97.5, vmaf_p10=93.6),
    _row("h265-aq+ultrafast", 457_000_000, vmaf_med=96.4, vmaf_p10=92.6),
]


def _lines_for(table: ComparisonTable) -> list[str]:
    lines = render_comparison_table(table)
    assert lines[-1] == ""  # Req 1.6 — blank line after the table block
    return lines


def _strategy_rows(lines: list[str]) -> list[str]:
    """The strategy data lines: table body minus the carrier rows."""
    header = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith(NAME_HEADER))
    return [
        ln for ln in lines[header + 2:-1]
        if "targets" not in ln and "(src)" not in ln
    ]


class TestStrategyRow:
    """THE persisted shape: the targeted role in memory, canonical on disk."""

    def test_metrics_round_trip(self) -> None:
        """A save→load cycle preserves the measured values and pair
        identities (Req 6.3)."""
        row = _row("s", 1)
        assert StrategyRow.model_validate(row.model_dump()) == row


class TestSearchMerge:
    """Targets ride the source row; strategies show deltas and verdicts."""

    def test_convention_lines_state_bar_bases_and_legend(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, targets=_TARGETS,
            source=SourceInfo(stem="Movie", size_bytes=1_000_000_000),
            mark_legend="recommended",
        ))
        assert lines[1] == (
            "  targets: ◎  configured (VMAF · PSNR)"
        )
        assert lines[2] == (
            "  Δ vs targets · size Δ% vs source · ≈ negligible · ◀ recommended"
        )
        assert lines[3] == ""  # one blank line between the info block and the table

    def test_source_row_carries_glyph_cue_and_target_absolutes(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, targets=_TARGETS,
            source=SourceInfo(stem="Movie", size_bytes=1_000_000_000),
        ))
        source_line = next(ln for ln in lines if "(src)" in ln)
        assert f"{ANCHOR_GLYPH} Movie (src)" in source_line
        assert SOURCE_CUE in source_line
        assert "◆ 93.0 | 96.0" in source_line  # the targeted absolutes ride it, ◆-marked

    def test_rows_sorted_by_size_with_deltas_and_marks(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, targets=_TARGETS,
            source=SourceInfo(stem="Movie", size_bytes=1_000_000_000),
            marks={
                "h265+ultrafast": Mark(selected=True, verdict=True),
                "h264+ultrafast": Mark(verdict=False),
            },
        ))
        strategy_lines = _strategy_rows(lines)
        assert [ln.lstrip().split()[0] for ln in strategy_lines if ln.lstrip().startswith("h26")] == [
            "h265+ultrafast", "h265-aq+ultrafast", "h264+ultrafast",
        ]
        first = next(ln for ln in strategy_lines if "h265+ultrafast" in ln)
        assert first.rstrip().endswith(f"{SELECTED_GLYPH} ✔")
        middle = next(ln for ln in strategy_lines if "h265-aq+ultrafast" in ln)
        assert not {SELECTED_GLYPH, "✔", "✘"} & set(middle)  # no marks passed — no glyphs
        assert "-0.4 | +0.4" in middle  # Δp10 | Δmed vs the targets
        last = next(ln for ln in strategy_lines if "h264+ultrafast" in ln)
        assert last.rstrip().endswith("✘")

    def test_size_delta_percent_vs_source(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, targets=_TARGETS,
            source=SourceInfo(stem="Movie", size_bytes=1_000_000_000),
        ))
        h265 = next(ln for ln in lines if ln.startswith("  h265+ultrafast"))
        assert "(-54.4%)" in h265  # (456_000_000 - 1e9) / 1e9


class TestSearchOptimization:
    """A standalone targets row leads; the best size is the %Δ basis."""

    def test_targets_row_leads_with_no_size(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, targets=_TARGETS,
            mark_legend="selected", size_tolerance_pct=5.0,
        ))
        assert lines[1] == (
            "  targets: ◎  configured (VMAF · PSNR)"
        )
        assert lines[2] == (
            "  Δ vs targets · size Δ% vs best size · ≈ negligible · ◀ selected · size tolerance 5%"
        )
        assert f"{ANCHOR_GLYPH} targets" in lines[6]  # the leading targets line

    def test_smallest_row_is_the_bare_basis(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, targets=_TARGETS,
        ))
        basis = next(ln for ln in lines if ln.startswith("  h265+ultrafast"))
        assert "(" not in basis  # no self-delta on the basis row


class TestFixedMode:
    """The anchor row rules: ◎ cue, absolutes, bare size at optimization."""

    def test_anchor_row_carries_glyph_suffix_and_absolutes(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, anchor="h265+ultrafast",
            mark_legend="selected",
        ))
        anchor_line = next(ln for ln in _strategy_rows(lines) if ANCHOR_SUFFIX in ln)
        assert f"{ANCHOR_GLYPH} h265+ultrafast {ANCHOR_SUFFIX}" in anchor_line
        assert "◆ 93.2 | 97.1" in anchor_line  # ◆-marked absolutes, not deltas
        assert "%" not in anchor_line  # smallest = basis = bare size cell

    def test_other_rows_show_deltas_vs_anchor(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, anchor="h265+ultrafast",
        ))
        h264 = next(ln for ln in _strategy_rows(lines) if "h264+ultrafast" in ln)
        assert "+0.4 | +0.4" in h264          # Δp10 | Δmed (VMAF)
        assert "(+12.3%)" in h264             # size Δ% vs the best size

    def test_merge_source_row_renders_dashes_and_anchor_deltas_vs_source(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, anchor="h265+ultrafast",
            source=SourceInfo(stem="Movie", size_bytes=1_000_000_000),
            mark_legend="recommended",
        ))
        source_line = next(ln for ln in lines if "(src)" in ln)
        assert source_line.count(MISSING_VALUE) == 4
        anchor_line = next(ln for ln in _strategy_rows(lines) if ANCHOR_SUFFIX in ln)
        assert "(-54.4%)" in anchor_line      # %Δ basis is the source at merge


class TestNegligibleDeltas:
    """Deltas inside the metric's negligible band (acceptance_delta) render ≈."""

    def _delta_line(self, vmaf_med: float) -> str:
        single = (QualityTarget.parse("vmaf-med:96.0"),)
        lines = _lines_for(ComparisonTable(
            rows=[_row("s", 1000, vmaf_med=vmaf_med)], targets=single,
        ))
        return next(ln for ln in lines if ln.startswith("  s"))

    def test_true_zero_renders_bare(self) -> None:
        line = self._delta_line(96.0)
        assert "0.0" in line
        assert "≈" not in line and "+" not in line

    def test_minor_positive_delta_renders_approx(self) -> None:
        assert "≈0.0" in self._delta_line(96.02)  # rounds to 0.0, inside the VMAF band
        assert "≈0.1" in self._delta_line(96.06)  # rounds to 0.1, still inside the band

    def test_non_ignored_positive_delta_keeps_the_sign(self) -> None:
        assert "+0.4" in self._delta_line(96.4)   # beyond the VMAF band (0.15)

    def test_negative_deltas_mirror_the_band(self) -> None:
        assert "≈0.0" in self._delta_line(95.98)  # negative dust — sign collapsed at zero
        assert "≈-0.1" in self._delta_line(95.94)
        assert "-0.4" in self._delta_line(95.6)

    def test_band_boundary_is_per_metric(self) -> None:
        ssim = (QualityTarget.parse("ssim-med:97.0"),)
        row = StrategyRow(strategy="s", size_bytes=1000, metrics={
            K(MetricType.SSIM, StatisticType.MEDIAN): 97.06,
        })
        lines = _lines_for(ComparisonTable(rows=[row], targets=ssim))
        line = next(ln for ln in lines if ln.startswith("  s"))
        assert "+0.1" in line  # 0.06 ≥ the SSIM band (0.05) — signed, not ≈


class TestNegligibleSizeDeltas:
    """Positive size deltas inside the stated tolerance render ≈ (one-sided band)."""

    def _row_line(self, table: ComparisonTable, name: str) -> str:
        return next(ln for ln in _lines_for(table) if ln.startswith(f"  {name}"))

    def test_in_tolerance_positive_delta_renders_approx(self) -> None:
        line = self._row_line(ComparisonTable(
            rows=_ROWS, size_tolerance_pct=5.0,
        ), "h265-aq+ultrafast")
        assert "(≈0.2%)" in line  # +0.22% vs the smallest — inside the 5% band

    def test_out_of_tolerance_delta_keeps_the_sign(self) -> None:
        line = self._row_line(ComparisonTable(
            rows=_ROWS, size_tolerance_pct=5.0,
        ), "h264+ultrafast")
        assert "(+12.3%)" in line

    def test_without_a_stated_tolerance_nothing_is_marked(self) -> None:
        line = self._row_line(ComparisonTable(rows=_ROWS), "h265-aq+ultrafast")
        assert "(+0.2%)" in line
        assert "≈" not in line  # no basis, no tolerance — no ≈ anywhere, legend included

    def test_legend_states_approx_even_ruler_less_with_tolerance(self) -> None:
        lines = _lines_for(ComparisonTable(rows=_ROWS, size_tolerance_pct=5.0))
        assert "≈ negligible" in lines[1]


class TestValueDrivenWidths:
    """Widths track the values; the header phrase rides above without inflating."""

    def test_metric_header_is_compact_and_crossings_align(self) -> None:
        lines = _lines_for(ComparisonTable(
            rows=_ROWS, anchor="h265+ultrafast",
        ))
        header, rule = lines[4], lines[5]
        assert "VMAF  10% |  50%" in header  # first label flush after the name
        assert [i for i, ch in enumerate(header) if ch == "|"] == \
               [i for i, ch in enumerate(rule) if ch == "+"]


class TestCaveatedRendering:
    """Rendering adapts to data available (Req 1.4)."""

    def test_missing_metric_values_render_dashes(self) -> None:
        sparse = StrategyRow(strategy="sparse", size_bytes=1, metrics={})
        lines = _lines_for(ComparisonTable(
            rows=[_ROWS[0], sparse], targets=_TARGETS,
        ))
        sparse_line = next(ln for ln in lines if ln.startswith("  sparse"))
        assert sparse_line.count(MISSING_VALUE) == 4

    def test_single_statistic_targets_render_without_divider(self) -> None:
        single = (QualityTarget.parse("vmaf-med:96.0"),)
        lines = _lines_for(ComparisonTable(
            rows=_ROWS[:1], targets=single,
        ))
        assert "VMAF" in lines[4]
        assert "|" not in lines[4]  # header line: no divider for one statistic

    def test_long_names_truncate_suffix_safe(self) -> None:
        long_name = "h265-some-extremely-long-profile-name+ultrafast"
        rows = [
            _row(long_name, 456_000_000),
            _row("h264+ultrafast", 512_000_000),
        ]
        lines = _lines_for(ComparisonTable(
            rows=rows, anchor=long_name,
        ))
        anchor_line = next(ln for ln in _strategy_rows(lines) if ANCHOR_SUFFIX in ln)
        assert "…" in anchor_line
        assert f"… {ANCHOR_SUFFIX}" in anchor_line  # the cut keeps the suffix intact
        assert long_name not in anchor_line         # and truncation actually happened


class TestUncompared:
    """Uncompared: absolute comparison statistics, no delta noise."""

    def test_absolutes_render_with_the_comparison_statistics(self) -> None:
        lines = _lines_for(ComparisonTable(rows=_ROWS))
        assert lines[1] == "  size Δ% vs best size · ◀ selected"  # legend only — no basis line
        assert "≈" not in lines[1]  # no deltas — no negligible glyph in the legend
        h265 = next(ln for ln in lines if ln.startswith("  h265+ultrafast"))
        assert "93.2 | 97.1" in h265  # absolute, not delta
        assert ABSOLUTE_MARKER not in "\n".join(lines)  # no deltas — nothing to distinguish


class TestProperties:
    """General laws over generated populations."""

    @given(
        sizes=st.lists(st.integers(min_value=1, max_value=10_000_000_000), min_size=2, max_size=5),
    )
    def test_rows_render_in_ascending_size_order(self, sizes: list[int]) -> None:
        """Sorted by size ascending regardless of input order."""
        rows = [_row(f"s{i}", size) for i, size in enumerate(sizes)]
        lines = _lines_for(ComparisonTable(rows=rows))
        rendered_names = [ln.split()[0] for ln in _strategy_rows(lines)]
        expected = [f"s{i}" for i in sorted(range(len(sizes)), key=lambda i: (sizes[i], f"s{i}"))]
        assert rendered_names == expected

    @given(
        verdicts=st.lists(st.booleans() | st.none(), min_size=1, max_size=4),
        selected=st.lists(st.booleans(), min_size=1, max_size=4),
    )
    def test_mark_zone_only_ever_carries_known_glyphs(self, verdicts: list[bool | None], selected: list[bool]) -> None:
        """The marks zone contains exactly the glyphs the marks requested —
        marks are data-driven, nothing else leaks into the zone."""
        n = max(len(verdicts), len(selected))
        rows = [_row(f"s{i}", (i + 1) * 1000) for i in range(n)]
        marks = {
            f"s{i}": Mark(
                selected=selected[i % len(selected)],
                verdict=verdicts[i % len(verdicts)],
            )
            for i in range(n)
        }
        lines = _lines_for(ComparisonTable(rows=rows, marks=marks))
        for line in _strategy_rows(lines):
            row_name = line.lstrip().split()[0]
            mark = marks[row_name]
            expected = (SELECTED_GLYPH if mark.selected else "") + (
                f" {'✔' if mark.verdict else '✘'}" if mark.verdict is not None else ""
            )
            if expected:
                assert line.rstrip().endswith(expected)
            else:
                assert not {SELECTED_GLYPH, "✔", "✘"} & set(line)
