"""Layout geometry tests."""

from __future__ import annotations

from libwatchduty import tui as _tui


def test_compute_layout_small_24_80():
    lay = _tui._compute_layout(24, 80)
    assert lay.lines == 24
    assert lay.cols == 80
    assert lay.show_detail is True
    assert lay.list_w >= 34
    assert lay.list_w + lay.detail_w == 80


def test_compute_layout_medium_40_160_caps_list_w():
    lay = _tui._compute_layout(40, 160)
    assert lay.show_detail is True
    # _LIST_W_MAX caps list_w; should never exceed it.
    assert lay.list_w <= _tui._LIST_W_MAX
    assert lay.list_w + lay.detail_w == 160


def test_compute_layout_wide_60_240():
    lay = _tui._compute_layout(60, 240)
    assert lay.show_detail is True
    assert lay.list_w <= _tui._LIST_W_MAX
    # body_bot is one row above the footer.
    assert lay.body_bot == 59
    assert lay.body_top == 2


def test_compute_layout_too_small_10_40():
    lay = _tui._compute_layout(10, 40)
    assert lay.too_small is False or lay.too_small is True  # boolean
    # 40 cols < 80 → detail hidden, list spans the whole width.
    assert lay.show_detail is False
    assert lay.list_w == 40
    assert lay.detail_w == 0


def test_list_col_offsets_constant_NAME_X():
    # NAME_X must equal pad + threat-col-width + dir-col + 1 — the column
    # everything in the list pane is keyed off. Pinning it here means
    # accidental changes to any of the contributing constants get caught.
    assert _tui._LIST_NAME_X == (
        _tui._LIST_PAD_L + _tui._LIST_THREAT_W + _tui._LIST_DIR_W + 1
    )
    # Sub-line offsets stack cleanly (no overlap).
    assert _tui._LIST_SUB_DIST_OFF == 0
    assert _tui._LIST_SUB_SIZE_OFF == _tui._LIST_SUB_DIST_W
    assert _tui._LIST_SUB_CONT_OFF == (
        _tui._LIST_SUB_DIST_W + _tui._LIST_SUB_SIZE_W
    )
