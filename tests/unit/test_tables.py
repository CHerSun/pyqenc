"""Unit tests for ``format_table`` — the single plain-text table mechanism
(unified-quality-summaries Req 10, TODO §119).

The util's exact output IS its contract: alignment, gaps, rule rows, bare
columns, suffix-safe truncation, and pipe-preserving rules are each pinned
on representative inputs, with property tests covering the general laws.
"""
# CHerSun 2026

from hypothesis import given
from hypothesis import strategies as st

from pyqenc.utils.tables import Column, format_table

_cell_text = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=0x2FFF, exclude_characters="|"),
    min_size=1, max_size=8,
)
"""Narrow, pipe-free cell text — keeps generated tables free of accidental rule pipes."""


class TestBasics:
    """Header/rule/row structure, gaps, widths, alignment."""

    def test_structure_gaps_and_alignment(self) -> None:
        """The table renders header, dash rule, then rows — 2-space gaps,
        names left-aligned, numerics right-aligned, 2-space indent."""
        lines = format_table(
            [Column("Strategy"), Column("Size (MB)", align="right"), Column("", align="right")],
            [["h265+ultrafast", "456.7 (+0.0%)", "◀"]],
        )
        assert lines == [
            "  Strategy            Size (MB)",
            "  --------------  -------------",
            "  h265+ultrafast  456.7 (+0.0%)  ◀",
        ]

    def test_dynamic_width_is_max_of_header_content_and_floor(self) -> None:
        """Width follows the widest of header, content, and the floor — the
        rule row exposes it exactly."""
        lines = format_table([Column("A", min_width=7, align="right")], [["bb"]])
        assert lines == [
            "        A",
            "  -------",
            "       bb",
        ]

    def test_header_aligns_with_its_column(self) -> None:
        """A right-aligned column right-aligns its header too."""
        lines = format_table([Column("MB", align="right")], [["1.5"]])
        assert lines == [
            "   MB",
            "  ---",
            "  1.5",
        ]

    def test_empty_table_renders_header_and_rule_only(self) -> None:
        """Zero rows still produce the header and rule lines."""
        assert format_table([Column("A")], []) == ["  A", "  -"]


class TestBareColumns:
    """Content-only zones: no header text, no rule-row segment."""

    def test_bare_column_renders_marks_without_header_or_rule(self) -> None:
        """The empty header marks a bare column: header blank, rule ends
        before the marks zone, content aligned."""
        lines = format_table(
            [Column("VMAF 10% |  50%", align="right"), Column("", align="right")],
            [["    43.0 | 45.0", "◀ ✔"]],
        )
        assert lines == [
            "  VMAF 10% |  50%",
            "  ---------+-----",
            "      43.0 | 45.0  ◀ ✔",
        ]

    def test_mid_table_bare_column_interrupts_the_rule(self) -> None:
        """A bare column mid-table leaves a blank gap in the rule row at its
        position — the rule ends before the bare zone and resumes after."""
        lines = format_table(
            [Column("A"), Column(""), Column("C", align="right")],
            [["a1", "x", "c1"]],
        )
        assert lines == [
            "  A       C",
            "  --     --",
            "  a1  x  c1",
        ]


class TestTruncation:
    """Suffix-safe truncation under a column's width ceiling."""

    def test_suffix_group_survives_truncation(self) -> None:
        """A trailing parenthesized group always survives; the ellipsis marks
        the cut in the head."""
        lines = format_table(
            [Column("Strategy", max_width=20)],
            [["h265+ultrafast+verylong (anchor)"]],
        )
        assert lines[2] == "  h265+ultra… (anchor)"
        assert len(lines[2]) - 2 == 20

    def test_plain_cell_truncates_with_ellipsis(self) -> None:
        """Without a suffix group the cell cuts at the ceiling."""
        lines = format_table([Column("S", max_width=10)], [["h265+ultrafast"]])
        assert lines[2] == "  h265+ultr…"

    def test_short_cells_pass_through_untouched(self) -> None:
        """Cells under the ceiling never gain padding from truncation."""
        lines = format_table([Column("S", max_width=10)], [["short"]])
        assert lines[2] == "  short"


class TestPipePreservingRules:
    """Rule rows mark in-cell divider crossings."""

    def test_piped_column_rules_at_the_cell_offsets(self) -> None:
        """Sub-column cells and their header produce a rule with ``+`` at the
        same offsets — the sub-column effect."""
        lines = format_table(
            [Column("VMAF 10% |  50%", align="right", min_width=15)],
            [["    43.0 | 45.0"], ["    -0.7 | -1.2"]],
        )
        assert lines == [
            "  VMAF 10% |  50%",
            "  ---------+-----",
            "      43.0 | 45.0",
            "      -0.7 | -1.2",
        ]

    def test_header_is_the_template_when_cells_lack_pipes(self) -> None:
        """A plain-valued column still gets the piped rule from its piped
        header."""
        lines = format_table([Column("A | B", align="right")], [["43.0"]])
        assert lines == [
            "  A | B",
            "  --+--",
            "   43.0",
        ]

    def test_pipe_free_column_keeps_a_solid_rule(self) -> None:
        """Columns without any pipe render solid dash spans."""
        lines = format_table([Column("Size (MB)", align="right")], [["456.7 (+0.0%)"]])
        assert lines[1] == "  -------------"


class TestProperties:
    """General laws over generated tables."""

    @given(
        headers=st.lists(_cell_text, min_size=1, max_size=4),
        rows=st.lists(st.lists(_cell_text, min_size=1, max_size=4), max_size=5),
    )
    def test_lines_never_carry_trailing_whitespace(self, headers: list[str], rows: list[list[str]]) -> None:
        """Every line is right-stripped — bare-column blank space never leaks."""
        n = len(headers)
        rows = [row[:n] + [""] * (n - len(row)) for row in rows]
        columns = [Column(h, align="right") for h in headers]
        for line in format_table(columns, rows):
            assert line == line.rstrip()

    @given(
        headers=st.lists(_cell_text, min_size=1, max_size=4),
        rows=st.lists(st.lists(_cell_text, min_size=1, max_size=4), max_size=5),
    )
    def test_rule_uses_only_rule_characters(self, headers: list[str], rows: list[list[str]]) -> None:
        """The rule row contains only dashes, crossings, and gap/blank space."""
        n = len(headers)
        rows = [row[:n] + [""] * (n - len(row)) for row in rows]
        columns = [Column(h, align="right") for h in headers]
        rule = format_table(columns, rows)[1]
        assert set(rule) <= {"-", "+", " "}

    @given(
        header=_cell_text,
        cells=st.lists(_cell_text, min_size=1, max_size=5),
        floor=st.integers(min_value=0, max_value=12),
    )
    def test_single_column_width_law(self, header: str, cells: list[str], floor: int) -> None:
        """A single pipe-free column's rule length equals the width law:
        max(header, content, floor)."""
        rule = format_table([Column(header, min_width=floor, align="right")], [[c] for c in cells])[1]
        expected = max(len(header), *(len(c) for c in cells), floor)
        assert len(rule) == 2 + expected
