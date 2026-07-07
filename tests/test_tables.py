"""Pure-function tests for the tables renderers (no TTY, no network)."""

from __future__ import annotations

import pytest

from libwatchduty import tables
from libwatchduty.colors import (
    TREE_BLANK,
    TREE_BRANCH,
    TREE_LAST,
    TREE_PIPE,
    paint,
)
from libwatchduty.tables import (
    Column,
    _pad,
    _visible_len,
    render_bar,
    render_kv,
    render_table,
    render_tree,
)


# ---------------------------------------------------------------------------
# render_table — sizing & truncation
# ---------------------------------------------------------------------------

def test_render_table_natural_widths():
    rows = [{"id": 1, "name": "Oak Fire"}, {"id": 22, "name": "Pine"}]
    cols = [Column("ID", "id"), Column("NAME", "name")]
    out = render_table(rows, cols, color=False, max_width=200)
    lines = out.splitlines()
    # header + separator + 2 rows, each column at max(header, cells) width.
    assert lines[0] == "ID NAME"
    assert lines[1] == "-- --------"
    assert lines[2] == "1  Oak Fire"
    assert lines[3] == "22 Pine"


def test_render_table_width_hint_truncates_with_ellipsis():
    rows = [{"v": "abcdefgh"}]
    cols = [Column("N", "v", width=4)]
    out = render_table(rows, cols, color=False, max_width=200)
    assert out.splitlines()[-1] == "abc…"


def test_render_table_width_zero_truncate_false_keeps_natural_width():
    # Regression: aircraft Name column configured width=0 + truncate=False
    # used to vanish; it must render at natural width instead.
    rows = [{"name": "N123WD Tanker"}]
    cols = [Column("Name", "name", width=0, truncate=False)]
    out = render_table(rows, cols, color=False, max_width=200)
    assert "N123WD Tanker" in out


def test_render_table_truncate_false_ignores_width_hint():
    rows = [{"v": "abcdefgh"}]
    cols = [Column("N", "v", width=3, truncate=False)]
    out = render_table(rows, cols, color=False, max_width=200)
    assert out.splitlines()[-1] == "abcdefgh"


def test_render_table_header_never_clipped_below_hint():
    # Hint narrower than the header: the column stays exactly hint wide,
    # never narrower, and the header is ellipsized at the hint.
    rows = [{"v": "x"}]
    cols = [Column("STATUS", "v", width=4)]
    out = render_table(rows, cols, color=False, max_width=200)
    lines = out.splitlines()
    assert lines[0] == "STA…"
    assert lines[1] == "----"


def test_render_table_empty_rows_header_and_separator_only():
    cols = [Column("ID", "id"), Column("NAME", "name")]
    out = render_table([], cols, color=False, max_width=200)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0] == "ID NAME"
    assert set(lines[1]) == {"-", " "}


def test_render_table_max_width_shrinks_widest_truncatable_column():
    rows = [{"a": "aaaaaaaaaa", "b": "bbbbb"}]  # natural 10 + 1 + 5 = 16
    cols = [Column("A", "a"), Column("B", "b")]
    out = render_table(rows, cols, color=False, max_width=14)
    row = out.splitlines()[-1]
    # The wider A column absorbs the 2-col deficit; B is untouched.
    assert row == "aaaaaaa… bbbbb"


# ---------------------------------------------------------------------------
# render_table — getters & color
# ---------------------------------------------------------------------------

def test_render_table_string_vs_callable_get():
    rows = [{"name": "oak", "acres": 12}]
    cols = [
        Column("NAME", "name"),                            # string field name
        Column("LOUD", lambda r: r["name"].upper()),       # callable
        Column("MISSING", "nope"),                         # absent key -> ""
    ]
    out = render_table(rows, cols, color=False, max_width=200)
    row = out.splitlines()[-1]
    assert "oak" in row
    assert "OAK" in row
    # Missing field renders empty (row rstrip'd, no crash).
    assert row.rstrip().endswith("OAK")


def test_render_table_color_callable_exception_swallowed(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")

    def _boom(value, row):
        raise RuntimeError("palette bug")

    rows = [{"v": "cell"}]
    cols = [Column("V", "v", color=_boom)]
    out = render_table(rows, cols, color=True, max_width=200)
    # No exception propagates and the cell renders un-colored.
    assert out.splitlines()[-1] == "cell"


# ---------------------------------------------------------------------------
# _visible_len / _pad — ANSI awareness
# ---------------------------------------------------------------------------

def test_visible_len_ignores_ansi(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    colored = paint("abc", "1;31")
    assert colored != "abc"  # escape codes actually present
    assert _visible_len(colored) == 3
    assert _visible_len("") == 0


def test_pad_uses_visible_width_for_colored_cells(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    colored = paint("ab", "0;36")
    left = _pad(colored, 5, "left")
    right = _pad(colored, 5, "right")
    center = _pad(colored, 5, "center")
    assert _visible_len(left) == 5 and left.endswith("   ")
    assert _visible_len(right) == 5 and right.startswith("   ")
    assert _visible_len(center) == 5
    assert center.startswith(" ") and center.endswith("  ")
    # Already wide enough -> returned unchanged.
    assert _pad(colored, 2, "left") == colored


# ---------------------------------------------------------------------------
# render_bar
# ---------------------------------------------------------------------------

def test_render_bar_zero_half_full():
    assert render_bar(0, 100, width=10, color=False) == "░" * 10 + " 0%"
    assert render_bar(50, 100, width=10, color=False) == "█" * 5 + "░" * 5 + " 50%"
    assert render_bar(100, 100, width=10, color=False) == "█" * 10 + " 100%"


def test_render_bar_clamps_value_above_total():
    over = render_bar(250, 100, width=10, color=False)
    assert over == render_bar(100, 100, width=10, color=False)
    assert over.endswith(" 100%")


def test_render_bar_width_honored_and_show_pct_off():
    bar = render_bar(33, 100, width=7, color=False, show_pct=False)
    assert len(bar) == 7
    assert "%" not in bar


# ---------------------------------------------------------------------------
# render_kv
# ---------------------------------------------------------------------------

def test_render_kv_label_alignment():
    out = render_kv([("a", "1"), ("long", "2")], color=False)
    lines = out.splitlines()
    assert lines[0] == "   a : 1"
    assert lines[1] == "long : 2"
    # All separators land at the same column.
    assert lines[0].index(" : ") == lines[1].index(" : ")


def test_render_kv_wraps_long_value_with_hanging_indent(monkeypatch):
    monkeypatch.setattr(tables, "_term_width", lambda default=100: 20)
    out = render_kv([("k", "alpha beta gamma delta epsilon")], color=False)
    lines = out.splitlines()
    assert len(lines) > 1
    assert lines[0] == "k : alpha beta gamma"
    # Continuation lines hang under the value column (label_w + " : ").
    for cont in lines[1:]:
        assert cont.startswith("    ")
        assert len(cont) <= 20
    assert lines[1] == "    delta epsilon"


def test_render_kv_ansi_value_skips_wrapping(monkeypatch):
    monkeypatch.setattr(tables, "_term_width", lambda default=100: 20)
    value = "\x1b[31m" + "x" * 30 + "\x1b[0m"
    out = render_kv([("k", value)], color=False)
    # Visible width exceeds the wrap column but ANSI values stay one line.
    assert len(out.splitlines()) == 1
    assert value in out


# ---------------------------------------------------------------------------
# render_tree
# ---------------------------------------------------------------------------

def test_render_tree_length_mismatch_raises():
    with pytest.raises(ValueError):
        render_tree(["p1", "p2"], [["kid"]])


def test_render_tree_child_glyphs_and_continuation_prefixes():
    out = render_tree(["Parent"], [["a\nb", "c\nd"]])
    assert out.splitlines() == [
        "Parent",
        f"{TREE_BRANCH}a",
        f"{TREE_PIPE}b",
        f"{TREE_LAST}c",
        f"{TREE_BLANK}d",
    ]


def test_render_tree_parent_without_children():
    out = render_tree(["Lonely", "Busy"], [[], ["kid"]])
    assert out.splitlines() == ["Lonely", "Busy", f"{TREE_LAST}kid"]
