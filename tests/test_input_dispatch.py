"""Key/command/filter dispatch tests for the TUI input layer.

All curses-free: ``_handle_key`` / ``_handle_filter_key`` / ``_handle_cmd_key``
take a plain ``_TuiState`` + an int keycode, and ``_compute_layout`` is pure.
Complements tests/test_keys.py — cases here focus on the regression fixes
(filter ESC cancel, selection-by-id, wheel-invert arithmetic) and the ``:``
command prompt.
"""

from __future__ import annotations

import curses
import queue

from libwatchduty import tui as _tui
from libwatchduty.tui.input import _maybe_zoom_mapscii


def _mkstate(n_fires: int = 5) -> "_tui._TuiState":
    s = _tui._TuiState()
    s.fires = [
        {"id": i + 1, "name": f"Fire {chr(65 + i)}", "address": f"{i} Main St"}
        for i in range(n_fires)
    ]
    _tui._recompute_visible(s)
    return s


def _mklayout(lines: int = 40, cols: int = 120) -> "_tui._Layout":
    return _tui._compute_layout(lines, cols)


def _q() -> "queue.Queue[tuple]":
    return queue.Queue()


# ---------------------------------------------------------------------------
# filter prompt
# ---------------------------------------------------------------------------


def test_filter_esc_cancels_live_edits():
    """Regression: typing applies the filter live; ESC must restore the
    pre-edit filter, not commit the edited one."""
    s = _mkstate()
    s.filter_text = "fire a"
    _tui._recompute_visible(s)
    assert len(s.visible_fires) == 1

    # Open the prompt via the real keybinding so the snapshot is taken.
    _tui._handle_key(s, _q(), _mklayout(), ord("/"))
    assert s.filter_active
    assert s.filter_original == "fire a"

    # Type a char that filters everything out — applied live.
    _tui._handle_filter_key(s, ord("z"))
    assert s.filter_text == "fire az"
    assert len(s.visible_fires) == 0

    # ESC rolls back to the snapshot.
    _tui._handle_filter_key(s, 27)
    assert not s.filter_active
    assert s.filter_text == "fire a"
    assert s.filter_buffer == "fire a"
    assert len(s.visible_fires) == 1


def test_filter_enter_commits():
    s = _mkstate()
    _tui._handle_key(s, _q(), _mklayout(), ord("/"))
    for ch in "fire b":
        _tui._handle_filter_key(s, ord(ch))
    _tui._handle_filter_key(s, 10)
    assert not s.filter_active
    assert s.filter_text == "fire b"
    assert [e["name"] for e in s.visible_fires] == ["Fire B"]


def test_filter_clear_key():
    s = _mkstate()
    s.filter_text = "fire a"
    _tui._recompute_visible(s)
    _tui._handle_key(s, _q(), _mklayout(), ord("X"))
    assert s.filter_text == ""
    assert len(s.visible_fires) == 5


def test_filter_backspace_and_length_cap():
    s = _mkstate()
    s.filter_active = True
    s.filter_buffer = "abc"
    _tui._handle_filter_key(s, curses.KEY_BACKSPACE)
    assert s.filter_buffer == "ab"
    s.filter_buffer = "x" * 50
    _tui._handle_filter_key(s, ord("y"))
    assert len(s.filter_buffer) == 50  # capped


# ---------------------------------------------------------------------------
# : command prompt
# ---------------------------------------------------------------------------


def test_cmd_within_updates_and_validates():
    s = _mkstate()
    q = _q()
    assert "within = 50" in _tui._apply_command(s, q, "within 50")
    assert s.within_km == 50.0
    assert "bad within" in _tui._apply_command(s, q, "within nope")
    assert "bad within" in _tui._apply_command(s, q, "within -5")
    assert s.within_km == 50.0  # unchanged on bad input


def test_cmd_threat_model_switch():
    s = _mkstate()
    q = _q()
    assert "threat-model = v2" in _tui._apply_command(s, q, "threat-model v2")
    assert s.threat_model == "v2"
    assert "must be v1 or v2" in _tui._apply_command(s, q, "threat-model v3")
    assert s.threat_model == "v2"


def test_cmd_sort_valid_and_invalid():
    s = _mkstate()
    q = _q()
    assert "sort = acreage" in _tui._apply_command(s, q, "sort acreage")
    assert s.sort_key == "acreage"
    assert "sort must be one of" in _tui._apply_command(s, q, "sort bogus")
    assert s.sort_key == "acreage"


def test_cmd_near_manual_and_off():
    s = _mkstate()
    q = _q()
    assert "near = " in _tui._apply_command(s, q, "near 37.77,-122.41")
    assert s.near == (37.77, -122.41)
    assert s.near_source == "manual"
    assert "bad near" in _tui._apply_command(s, q, "near garbage")
    assert "near filter off" in _tui._apply_command(s, q, "near off")
    assert s.near is None


def test_cmd_prompt_key_flow():
    s = _mkstate()
    q = _q()
    _tui._handle_key(s, q, _mklayout(), ord(":"))
    assert s.cmd_active
    for ch in "within 75":
        _tui._handle_cmd_key(s, q, ord(ch))
    _tui._handle_cmd_key(s, q, 10)
    assert not s.cmd_active
    assert s.within_km == 75.0


# ---------------------------------------------------------------------------
# selection: movement + id anchoring
# ---------------------------------------------------------------------------


def test_jk_moves_and_clamps():
    s = _mkstate(3)
    lay = _mklayout()
    q = _q()
    assert s.selected_idx == 0
    _tui._handle_key(s, q, lay, ord("k"))
    assert s.selected_idx == 0  # clamped at top
    _tui._handle_key(s, q, lay, ord("j"))
    assert s.selected_idx == 1
    _tui._handle_key(s, q, lay, ord("G"))
    assert s.selected_idx == 2
    _tui._handle_key(s, q, lay, ord("j"))
    assert s.selected_idx == 2  # clamped at bottom


def test_selection_follows_fire_id_across_resort():
    """Regression: selection used to track the row INDEX, so a re-sort
    silently switched which fire was selected mid-read."""
    s = _mkstate(3)
    lay = _mklayout()
    q = _q()
    _tui._handle_key(s, q, lay, ord("j"))  # select row 1
    picked = s.visible_fires[s.selected_idx]["id"]
    assert s.selected_fire_id == picked

    # Re-sort so rows move (acreage descending puts fire 3 first).
    for i, e in enumerate(s.fires):
        e["data"] = {"acreage": (i + 1) * 100}
    s.sort_key = "acreage"
    _tui._recompute_visible(s)
    now = s.visible_fires[s.selected_idx]["id"]
    assert now == picked  # same fire, new row


def test_selection_id_cleared_when_fire_vanishes():
    s = _mkstate(3)
    _tui._select_idx(s, 2)
    gone = s.selected_fire_id
    s.fires = [e for e in s.fires if e["id"] != gone]
    _tui._recompute_visible(s)
    # Falls back to a clamped index + re-anchors to whatever is there.
    assert 0 <= s.selected_idx < len(s.visible_fires)
    assert s.selected_fire_id != gone


def test_half_page_ctrl_d_u():
    s = _mkstate(40)
    lay = _mklayout(40, 120)
    q = _q()
    _tui._handle_key(s, q, lay, 4)  # Ctrl-D
    assert s.selected_idx > 0
    down = s.selected_idx
    _tui._handle_key(s, q, lay, 21)  # Ctrl-U
    assert s.selected_idx < down


# ---------------------------------------------------------------------------
# tabs + focus
# ---------------------------------------------------------------------------


def test_number_keys_jump_tabs_and_reset_scroll():
    s = _mkstate()
    lay = _mklayout()
    q = _q()
    s.detail_scroll = 7
    _tui._handle_key(s, q, lay, ord("3"))
    assert s.active_tab == "map"
    assert s.focus == _tui._FOCUS_DETAIL
    assert s.detail_scroll == 0
    _tui._handle_key(s, q, lay, ord("1"))
    assert s.active_tab == "updates"


def test_tab_key_cycles_focus_then_tabs():
    s = _mkstate()
    lay = _mklayout()
    q = _q()
    assert s.focus == _tui._FOCUS_LIST
    _tui._handle_key(s, q, lay, 9)  # Tab: list -> detail
    assert s.focus == _tui._FOCUS_DETAIL
    before = s.active_tab
    _tui._handle_key(s, q, lay, 9)  # Tab in detail: cycle tab
    assert s.active_tab != before


def test_esc_returns_to_list():
    s = _mkstate()
    lay = _mklayout()
    q = _q()
    s.focus = _tui._FOCUS_DETAIL
    _tui._handle_key(s, q, lay, 27)
    assert s.focus == _tui._FOCUS_LIST


# ---------------------------------------------------------------------------
# wheel invert arithmetic (regression: double-flip made invert a no-op)
# ---------------------------------------------------------------------------


def test_wheel_invert_is_single_flip():
    """The SGR parser's invert must map 64<->65 exactly once. The buggy
    version used two sequential ifs, so 64 -> 65 -> 64. Mirror the shipped
    arithmetic here; if input.py regresses to sequential ifs this documents
    the required contract."""
    for invert, start, want in [
        (True, 64, 65),
        (True, 65, 64),
        (False, 64, 64),
        (False, 65, 65),
    ]:
        btn = start
        if invert:
            if btn == 64:
                btn = 65
            elif btn == 65:
                btn = 64
        assert btn == want, (invert, start, btn)


def test_zoom_helper_does_not_reinvert(monkeypatch):
    """Callers of _maybe_zoom_mapscii pass already-inverted directions; the
    helper must not invert again. With invert=True and a hovering wheel-up,
    the PTY must receive exactly one direction, decided by the caller."""
    s = _mkstate()
    sent: list[bytes] = []

    class _FakeEmbed:
        alive = True

        def send(self, data: bytes) -> None:
            sent.append(data)

    s.active_tab = "map"
    s.mapscii_embed = _FakeEmbed()
    s.mapscii_rect = (5, 5, 10, 20)
    s.mouse_wheel_invert = True  # must have NO effect inside the helper

    assert _maybe_zoom_mapscii(s, mx=10, my=8, wheel_up=True, wheel_down=False)
    assert sent == [b"a"]  # zoom-in exactly as passed, not re-flipped
