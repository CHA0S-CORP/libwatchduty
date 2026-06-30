"""Key-handler tests — drive ``_handle_key`` with a fake request queue.

We use a real ``queue.Queue`` so any enqueue-side-effects are observable.
Curses key constants vary by platform; we read them from the curses
module rather than hard-coding values.
"""

from __future__ import annotations

import curses
import queue

import pytest

from libwatchduty import tui as _tui


@pytest.fixture
def req_q() -> "queue.Queue[tuple]":
    return queue.Queue()


@pytest.fixture
def layout():
    return _tui._compute_layout(40, 160)


def test_j_in_list_advances_selection(tui_state, req_q, layout):
    tui_state.focus = _tui._FOCUS_LIST
    start = tui_state.selected_idx
    assert _tui._handle_key(tui_state, req_q, layout, ord("j")) is True
    assert tui_state.selected_idx == start + 1


def test_k_in_list_moves_selection_up(tui_state, req_q, layout):
    tui_state.focus = _tui._FOCUS_LIST
    tui_state.selected_idx = 1
    assert _tui._handle_key(tui_state, req_q, layout, ord("k")) is True
    assert tui_state.selected_idx == 0


def test_j_in_detail_scrolls(tui_state, req_q, layout):
    tui_state.focus = _tui._FOCUS_DETAIL
    tui_state.detail_scroll_max = 5
    tui_state.detail_scroll = 0
    _tui._handle_key(tui_state, req_q, layout, ord("j"))
    assert tui_state.detail_scroll == 1


def test_shift_J_K_always_scroll_detail(tui_state, req_q, layout):
    tui_state.focus = _tui._FOCUS_LIST    # even when LIST has focus
    tui_state.detail_scroll_max = 10
    tui_state.detail_scroll = 0
    _tui._handle_key(tui_state, req_q, layout, ord("J"))
    assert tui_state.detail_scroll == 1
    _tui._handle_key(tui_state, req_q, layout, ord("K"))
    assert tui_state.detail_scroll == 0


def test_pgup_pgdn_scroll_detail(tui_state, req_q, layout):
    tui_state.detail_scroll_max = 100
    tui_state.detail_scroll = 50
    _tui._handle_key(tui_state, req_q, layout, curses.KEY_NPAGE)
    assert tui_state.detail_scroll > 50
    _tui._handle_key(tui_state, req_q, layout, curses.KEY_PPAGE)
    assert tui_state.detail_scroll <= 50


def test_tab_cycles_focus_when_list_focused(tui_state, req_q, layout):
    tui_state.focus = _tui._FOCUS_LIST
    _tui._handle_key(tui_state, req_q, layout, 9)   # Tab
    assert tui_state.focus == _tui._FOCUS_DETAIL


def test_tab_cycles_tabs_when_detail_focused(tui_state, req_q, layout):
    tui_state.focus = _tui._FOCUS_DETAIL
    tui_state.active_tab = "updates"
    _tui._handle_key(tui_state, req_q, layout, 9)
    assert tui_state.active_tab == "radio"


def test_number_keys_jump_to_tabs(tui_state, req_q, layout):
    for ch, expected in [(ord("1"), "updates"), (ord("2"), "radio"),
                          (ord("3"), "map"), (ord("4"), "evac")]:
        _tui._handle_key(tui_state, req_q, layout, ch)
        assert tui_state.active_tab == expected
        assert tui_state.focus == _tui._FOCUS_DETAIL


def test_z_toggles_list_compact(tui_state, req_q, layout):
    before = tui_state.list_compact
    _tui._handle_key(tui_state, req_q, layout, ord("z"))
    assert tui_state.list_compact != before


def test_plus_minus_cycle_image_size(tui_state, req_q, layout):
    tui_state.active_tab = "updates"        # not map
    tui_state.image_size = "med"
    _tui._handle_key(tui_state, req_q, layout, ord("+"))
    assert tui_state.image_size == "large"
    _tui._handle_key(tui_state, req_q, layout, ord("-"))
    assert tui_state.image_size == "med"
    # bottom is clamped at "small".
    _tui._handle_key(tui_state, req_q, layout, ord("-"))
    _tui._handle_key(tui_state, req_q, layout, ord("-"))
    assert tui_state.image_size == "small"


def test_X_and_ctrl_L_clear_filter(tui_state, req_q, layout):
    tui_state.filter_text = "pine"
    tui_state.filter_buffer = "pine"
    _tui._handle_key(tui_state, req_q, layout, ord("X"))
    assert tui_state.filter_text == ""
    assert tui_state.filter_buffer == ""
    # Ctrl-L (12) also clears — set up another filter and clear it.
    tui_state.filter_text = "oak"
    _tui._handle_key(tui_state, req_q, layout, 12)
    assert tui_state.filter_text == ""
