"""Keyboard / mouse / prompt handling + the `:` command interpreter."""

from __future__ import annotations

import curses
import os
import queue
import sys
import time

from .derive import (
    _jump_to_next_match,
    _recompute_distances,
    _recompute_threats,
    _recompute_visible,
    _select_idx,
    _set_status,
)
from .images_paint import _pick_image_url
from .layout import _Layout
from .state import (
    _CHORD_TIMEOUT,
    _FOCUS_DETAIL,
    _FOCUS_LIST,
    _IMG_SIZE_ORDER,
    _LIST_ROWS_PER_FIRE,
    _MIN_AUTO_REFRESH,
    _SORT_KEYS,
    _TAB_KEYS,
    _TABS,
    _TuiState,
)
from .worker import _enqueue_image, _enqueue_refresh, _enqueue_reports


# ---------------------------------------------------------------------------
# command + filter prompt
# ---------------------------------------------------------------------------

def _apply_command(state: _TuiState, req_q: "queue.Queue[tuple]", line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("within", "w"):
        try:
            n = float(arg)
            if n <= 0:
                raise ValueError
        except ValueError:
            return f"bad within: {arg!r}"
        state.within_km = n
        _recompute_threats(state)
        _recompute_visible(state)
        return f"within = {n:g} km"
    if cmd == "near":
        if arg.lower() == "off":
            state.near = None
            state.near_source = ""
            _recompute_distances(state)
            _recompute_threats(state)
            _recompute_visible(state)
            return "near filter off"
        if arg.lower() == "auto":
            try:
                from ..location import detect_location
                got = detect_location(timeout=3.0)
            except Exception as e:
                return f"auto-locate failed: {e}"
            if got is None:
                return "auto-locate failed"
            state.near = (got[0], got[1])
            state.near_source = got[2]
            _recompute_distances(state)
            _recompute_threats(state)
            _recompute_visible(state)
            return f"near = {got[0]:.2f},{got[1]:.2f} ({got[2]})"
        try:
            a, b = (x.strip() for x in arg.split(","))
            state.near = (float(a), float(b))
            state.near_source = "manual"
        except (ValueError, AttributeError):
            return f"bad near: {arg!r}"
        _recompute_distances(state)
        _recompute_threats(state)
        _recompute_visible(state)
        return f"near = {state.near[0]:.2f},{state.near[1]:.2f}"
    if cmd in ("types", "type", "ty"):
        types = tuple(t.strip() for t in arg.split(",") if t.strip())
        if not types:
            return "no types given"
        state.types = types
        _enqueue_refresh(state, req_q)
        return f"types = {','.join(types)}"
    if cmd == "sort":
        if arg not in _SORT_KEYS:
            return f"sort must be one of {','.join(_SORT_KEYS)}"
        state.sort_key = arg
        _recompute_visible(state)
        return f"sort = {arg}"
    if cmd == "reverse":
        state.sort_reverse = not state.sort_reverse
        _recompute_visible(state)
        return f"reverse = {state.sort_reverse}"
    if cmd in ("mouse-invert", "invert-wheel", "mouse"):
        state.mouse_wheel_invert = not state.mouse_wheel_invert
        return (
            f"mouse wheel inverted: {state.mouse_wheel_invert} "
            f"(bstate last seen: 0x{state.last_mouse_bstate:08x})"
        )
    if cmd == "mouse-debug":
        # Toggle a status hint whenever any mouse event arrives.
        state.mouse_debug = not getattr(state, "mouse_debug", False)
        return f"mouse debug: {state.mouse_debug}"
    if cmd == "refresh":
        try:
            n = int(arg)
        except ValueError:
            return f"bad refresh: {arg!r}"
        state.auto_refresh = max(_MIN_AUTO_REFRESH, n) if n else 0
        return f"refresh = {state.auto_refresh}s"
    if cmd in ("threat-model", "threat_model", "tm"):
        choice = arg.strip().lower()
        if choice not in ("v1", "v2"):
            return f"threat-model must be v1 or v2 (got {arg!r})"
        state.threat_model = choice
        _recompute_threats(state)
        _recompute_visible(state)
        return f"threat-model = {choice}"
    return f"unknown: {cmd}"


def _handle_cmd_key(state: _TuiState, req_q: "queue.Queue[tuple]", ch: int) -> bool:
    if ch in (10, 13, curses.KEY_ENTER):
        msg = _apply_command(state, req_q, state.cmd_buffer)
        state.cmd_active = False
        state.cmd_buffer = ""
        if msg:
            _set_status(state, msg)
        return True
    if ch == 27:
        state.cmd_active = False
        state.cmd_buffer = ""
        return True
    if ch in (curses.KEY_BACKSPACE, 127, 8):
        state.cmd_buffer = state.cmd_buffer[:-1]
        return True
    if 32 <= ch < 127:
        state.cmd_buffer += chr(ch)
        return True
    return False


def _handle_filter_key(state: _TuiState, ch: int) -> bool:
    if ch in (10, 13, curses.KEY_ENTER):
        state.filter_text = state.filter_buffer
        state.filter_active = False
        _recompute_visible(state)
        return True
    if ch == 27:
        # Cancel: restore the pre-edit filter. Typing applies the filter
        # live, so `filter_text` already holds the edited value — roll it
        # back to the snapshot taken when the prompt opened.
        state.filter_active = False
        state.filter_text = state.filter_original
        state.filter_buffer = state.filter_original
        _recompute_visible(state)
        return True
    if ch in (curses.KEY_BACKSPACE, 127, 8):
        state.filter_buffer = state.filter_buffer[:-1]
        return True
    if 32 <= ch < 127:
        # Cap so a dragged-in file path can only do so much damage on
        # terminals that don't honour bracketed-paste rejection.
        if len(state.filter_buffer) < 50:
            state.filter_buffer += chr(ch)
            state.filter_text = state.filter_buffer
            _recompute_visible(state)
        return True
    return False


# ---------------------------------------------------------------------------
# mouse + keys
# ---------------------------------------------------------------------------

def _maybe_consume_bracketed_paste(stdscr, ch: int, state: _TuiState) -> int:
    """Drop a bracketed-paste sequence so dragged-in files / pasted text
    can't fill the filter prompt with garbage.

    Bracketed paste is enabled at startup via ``\\x1b[?2004h``. When the
    terminal sees a paste it wraps the payload in
    ``\\x1b[200~ <bytes…> \\x1b[201~``. If we see the opening marker
    arriving as ``ESC``, read forward (non-blocking) until we either
    consume the closing marker or hit a short read. Returns ``-1`` when
    consumed so the caller skips dispatch.
    """
    if ch != 27:
        return ch
    # Peek the next few bytes non-blocking to see if this ESC starts a
    # paste marker. We only consume when we see the full ESC[200~ ;
    # otherwise we push the bytes back so curses sees a normal ESC + key.
    peeked: list[int] = []

    def _read() -> int:
        stdscr.nodelay(True)
        try:
            return stdscr.getch()
        finally:
            stdscr.nodelay(False)

    expect = (ord("["), ord("2"), ord("0"), ord("0"), ord("~"))
    for want in expect:
        nx = _read()
        if nx == -1:
            # Incomplete — push back what we have and bail.
            for b in reversed(peeked):
                try:
                    curses.ungetch(b)
                except curses.error:
                    pass
            return ch
        peeked.append(nx)
        if nx != want:
            for b in reversed(peeked):
                try:
                    curses.ungetch(b)
                except curses.error:
                    pass
            return ch
    # Full opening marker consumed. Now drain until we see the closing
    # marker ESC[201~, or until ~256 bytes of payload (paste safety cap).
    closing = (27, ord("["), ord("2"), ord("0"), ord("1"), ord("~"))
    tail: list[int] = []
    for _ in range(8192):
        nx = _read()
        if nx == -1:
            break
        tail.append(nx)
        if len(tail) >= len(closing) and tuple(tail[-len(closing):]) == closing:
            break
    if state.mouse_debug:
        _set_status(state,
                    f"dropped paste ({len(tail)} bytes incl. close marker)")
    return -1


def _maybe_consume_sgr_mouse(
    stdscr, ch: int, state: _TuiState,
    req_q: "queue.Queue[tuple]", layout: _Layout,
) -> int:
    """Detect and swallow an SGR mouse escape sequence.

    If ``ch`` looks like the start of `ESC [ < <btn> ; <x> ; <y> (M|m)`,
    read the rest of the sequence, dispatch it as a synthetic mouse
    event, and return ``-1`` so the caller doesn't process the ESC as a
    keystroke. Otherwise return ``ch`` unchanged.
    """
    if ch != 27:   # ESC
        return ch
    # Use a tiny non-blocking read to peek the next bytes. If they don't
    # match the SGR prefix, push them back so curses sees a plain ESC.
    def read_one(timeout_ms: int = 30) -> int:
        stdscr.nodelay(True)
        try:
            return stdscr.getch()
        finally:
            stdscr.nodelay(False)

    n1 = read_one()
    if n1 != ord("["):
        if n1 != -1:
            try:
                curses.ungetch(n1)
            except curses.error:
                pass
        return ch
    n2 = read_one()
    if n2 != ord("<"):
        if n2 != -1:
            try:
                curses.ungetch(n2)
            except curses.error:
                pass
        try:
            curses.ungetch(n1)
        except curses.error:
            pass
        return ch
    # Read digits + ';' until we see 'M' or 'm'.
    payload = bytearray()
    is_press = True
    for _ in range(32):
        nx = read_one()
        if nx == -1:
            break
        if nx == ord("M"):
            is_press = True
            break
        if nx == ord("m"):
            is_press = False
            break
        if 32 <= nx < 127:
            payload.append(nx)
    try:
        parts = bytes(payload).decode("ascii").split(";")
        btn = int(parts[0])
        mx = int(parts[1]) - 1
        my = int(parts[2]) - 1
    except (ValueError, IndexError):
        return -1
    # Synthesise a bstate matching our wheel/click code paths.
    bstate = 0
    if btn == 64:
        bstate = getattr(curses, "BUTTON4_PRESSED", 0)
    elif btn == 65:
        bstate = getattr(curses, "BUTTON5_PRESSED", 0)
    elif btn == 0 and is_press:
        bstate = getattr(curses, "BUTTON1_CLICKED", 0)
    state.last_mouse_bstate = bstate
    if state.mouse_debug:
        _set_status(state,
                    f"sgr mouse btn={btn} @({mx},{my}) bstate=0x{bstate:08x}")
    # Route into the existing wheel/click logic by calling a small inline
    # version of _handle_mouse with the parsed coordinates.
    over_list = mx < layout.list_w
    step = 3
    if state.mouse_wheel_invert:
        if btn == 64:   # wheel up
            btn = 65
        elif btn == 65:
            btn = 64
    if (btn in (64, 65) and is_press
            and _maybe_zoom_mapscii(state, mx, my,
                                    wheel_up=(btn == 64),
                                    wheel_down=(btn == 65))):
        return -1
    if btn == 64 and is_press:
        if over_list:
            _select_idx(state, max(0, state.selected_idx - 1))
        else:
            state.detail_scroll = max(0, state.detail_scroll - step)
    elif btn == 65 and is_press:
        if over_list and state.visible_fires:
            _select_idx(state, min(len(state.visible_fires) - 1,
                                   state.selected_idx + 1))
        else:
            state.detail_scroll = min(state.detail_scroll_max,
                                      state.detail_scroll + step)
    elif btn == 0 and is_press:
        # Reuse the existing click logic by populating a fake event
        # path: tab strip hit-test, list/detail focus shift, etc.
        for slot in state.update_image_slots:
            try:
                _rid, sy, sx, surl, sw, sh = slot
            except (ValueError, TypeError):
                continue
            if sy <= my < sy + sh and sx <= mx < sx + sw and surl:
                eid_i = -1
                if state.visible_fires:
                    cf = state.visible_fires[state.selected_idx].get("id")
                    if cf is not None:
                        eid_i = int(cf)
                if eid_i >= 0:
                    state.image_show_for = eid_i
                    state.image_show_url = surl
                    _enqueue_image(state, req_q, eid_i, surl)
                    _set_status(state, "loading fullscreen…")
                return -1
        for row, x1, x2, name in state.tab_rects:
            if my == row and x1 <= mx < x2:
                state.active_tab = name
                state.focus = _FOCUS_DETAIL
                state.detail_scroll = 0
                return -1
        if mx < layout.list_w:
            state.focus = _FOCUS_LIST
            list_body_top = layout.body_top + 3
            rel = my - list_body_top
            if rel >= 0:
                rows_per_card = 1 if state.list_compact else _LIST_ROWS_PER_FIRE
                new_idx = state.list_scroll + (rel // rows_per_card)
                if 0 <= new_idx < len(state.visible_fires):
                    _select_idx(state, new_idx)
        elif layout.show_detail:
            state.focus = _FOCUS_DETAIL
    return -1   # swallowed


def _mouse_over_mapscii(state: _TuiState, mx: int, my: int) -> bool:
    """True if (mx, my) is inside the embedded mapscii rectangle."""
    if (state.active_tab != "map"
            or state.mapscii_embed is None
            or not getattr(state.mapscii_embed, "alive", False)
            or not state.mapscii_rect):
        return False
    try:
        y0, x0, h, w = state.mapscii_rect
    except (ValueError, TypeError):
        return False
    return y0 <= my < y0 + h and x0 <= mx < x0 + w


def _maybe_zoom_mapscii(state: _TuiState, mx: int, my: int,
                        wheel_up: bool, wheel_down: bool) -> bool:
    """If wheel fires while hovering the embedded mapscii, send the
    zoom-in/out key to its PTY and report consumed=True.

    Callers pass directions with ``mouse_wheel_invert`` already applied —
    do NOT invert again here.
    """
    if not _mouse_over_mapscii(state, mx, my):
        return False
    if wheel_up:
        state.mapscii_embed.send(b"a")
        return True
    if wheel_down:
        state.mapscii_embed.send(b"z")
        return True
    return False


def _handle_mouse(state: _TuiState, req_q: "queue.Queue[tuple]", layout: _Layout) -> None:
    try:
        _, mx, my, _, bstate = curses.getmouse()
    except curses.error:
        return
    if my < layout.body_top or my >= layout.body_bot:
        return
    # Record raw bstate for debugging via `:mouse-debug` command.
    state.last_mouse_bstate = bstate
    if state.mouse_debug:
        _set_status(state,
                    f"mouse @ ({mx},{my}) bstate=0x{bstate:08x}")
    # Route wheel by mouse position, not focus: pointer in the left pane
    # scrolls the fire list; pointer in the right pane scrolls the detail.
    over_list = mx < layout.list_w
    step = 3   # wheel ticks tend to feel slow at 1 row

    # Some curses builds report wheel as PRESSED, others as RELEASED, and a
    # few only emit CLICKED. Treat any of those as a wheel tick.
    def _wheel_mask(button: int) -> int:
        return (
            getattr(curses, f"BUTTON{button}_PRESSED", 0)
            | getattr(curses, f"BUTTON{button}_RELEASED", 0)
            | getattr(curses, f"BUTTON{button}_CLICKED", 0)
        )

    wheel_up_mask = _wheel_mask(4)
    wheel_down_mask = _wheel_mask(5)
    # macOS terminals with natural scrolling sometimes invert the codes;
    # respect the user's preference via state.mouse_wheel_invert.
    if state.mouse_wheel_invert:
        wheel_up_mask, wheel_down_mask = wheel_down_mask, wheel_up_mask

    wheel_up = bool(wheel_up_mask and (bstate & wheel_up_mask))
    wheel_down = bool(wheel_down_mask and (bstate & wheel_down_mask))
    # Mapscii rect intercepts wheel events: up=zoom in, down=zoom out.
    if (wheel_up or wheel_down) and _maybe_zoom_mapscii(
            state, mx, my, wheel_up, wheel_down):
        return
    if wheel_up:
        if over_list:
            _select_idx(state, max(0, state.selected_idx - 1))
        else:
            state.detail_scroll = max(0, state.detail_scroll - step)
        return
    if wheel_down:
        if over_list and state.visible_fires:
            _select_idx(state, min(len(state.visible_fires) - 1,
                                   state.selected_idx + 1))
        else:
            state.detail_scroll = min(state.detail_scroll_max,
                                      state.detail_scroll + step)
        return
    if bstate & getattr(curses, "BUTTON1_CLICKED", 0):
        # Click on an inline update thumbnail → fullscreen preview.
        for slot in state.update_image_slots:
            try:
                _rid, sy, sx, surl, sw, sh = slot
            except (ValueError, TypeError):
                continue
            if sy <= my < sy + sh and sx <= mx < sx + sw and surl:
                # Selected fire id (needed by image_show_for).
                eid_i = -1
                if state.visible_fires:
                    cf = state.visible_fires[state.selected_idx].get("id")
                    if cf is not None:
                        eid_i = int(cf)
                if eid_i >= 0:
                    state.image_show_for = eid_i
                    state.image_show_url = surl
                    _enqueue_image(state, req_q, eid_i, surl)
                    _set_status(state, "loading fullscreen…")
                return
        # Tab strip (rects populated in _draw_tab_bar).
        for row, x1, x2, name in state.tab_rects:
            if my == row and x1 <= mx < x2:
                state.active_tab = name
                state.focus = _FOCUS_DETAIL
                state.detail_scroll = 0
                return
        if mx < layout.list_w:
            state.focus = _FOCUS_LIST
            # List body starts at body_top + 3 (header + sub + hairline).
            list_body_top = layout.body_top + 3
            rel = my - list_body_top
            if rel < 0:
                return
            rows_per_card = 1 if state.list_compact else _LIST_ROWS_PER_FIRE
            new_idx = state.list_scroll + (rel // rows_per_card)
            if 0 <= new_idx < len(state.visible_fires):
                _select_idx(state, new_idx)
                e = state.visible_fires[new_idx]
                eid = e.get("id")
                if eid is not None and int(eid) not in state.reports_cache:
                    _enqueue_reports(state, req_q, int(eid))
        elif layout.show_detail:
            state.focus = _FOCUS_DETAIL


def _handle_key(
    state: _TuiState, req_q: "queue.Queue[tuple]", layout: _Layout, ch: int,
) -> bool:
    if ch == -1:
        return False
    if ch == curses.KEY_RESIZE:
        return True
    if state.filter_active:
        return _handle_filter_key(state, ch)
    if state.cmd_active:
        return _handle_cmd_key(state, req_q, ch)

    height = layout.body_bot - layout.body_top
    rows = state.visible_fires

    if ch == ord("g"):
        now = time.monotonic()
        if now - state.last_g < _CHORD_TIMEOUT:
            _select_idx(state, 0)
            state.last_g = 0
            return True
        state.last_g = now
        return False
    else:
        state.last_g = 0

    if ch != curses.KEY_LEFT:
        state.last_left = 0.0

    if ch == ord("q"):
        state.quit = True
        return True
    if ch == ord("J"):
        state.detail_scroll = min(state.detail_scroll_max,
                                  state.detail_scroll + 1)
        return True
    if ch == ord("K"):
        state.detail_scroll = max(0, state.detail_scroll - 1)
        return True
    # When focused on the Map tab and mapscii is embedded, forward
    # cursor + zoom keys into the running mapscii process instead of
    # using them for dashboard scroll.
    _map_active = (
        state.focus == _FOCUS_DETAIL
        and state.active_tab == "map"
        and state.mapscii_embed is not None
        and getattr(state.mapscii_embed, "alive", False)
    )
    if _map_active:
        key_to_bytes = {
            curses.KEY_UP:    b"\x1b[A",
            curses.KEY_DOWN:  b"\x1b[B",
            curses.KEY_RIGHT: b"\x1b[C",
            curses.KEY_LEFT:  b"\x1b[D",
            ord("a"):         b"a",
            ord("z"):         b"z",
            ord("c"):         b"c",
        }
        if ch in key_to_bytes:
            state.mapscii_embed.send(key_to_bytes[ch])
            return True

    if ch in (ord("j"), curses.KEY_DOWN):
        # Focus-aware: in the detail pane j scrolls updates; in the list
        # it advances selection. J / PgDn always scroll the right pane.
        if state.focus == _FOCUS_DETAIL:
            state.detail_scroll = min(state.detail_scroll_max,
                                      state.detail_scroll + 1)
            return True
        if rows:
            _select_idx(state, min(len(rows) - 1, state.selected_idx + 1))
        return True
    if ch in (ord("k"), curses.KEY_UP):
        if state.focus == _FOCUS_DETAIL:
            state.detail_scroll = max(0, state.detail_scroll - 1)
            return True
        if rows:
            _select_idx(state, max(0, state.selected_idx - 1))
        return True
    if ch == curses.KEY_LEFT:
        now = time.monotonic()
        if (state.focus != _FOCUS_LIST
                and now - state.last_left < _CHORD_TIMEOUT):
            state.focus = _FOCUS_LIST
            state.last_left = 0.0
            return True
        state.last_left = now
        try:
            i = _TABS.index(state.active_tab)
        except ValueError:
            i = 0
        state.active_tab = _TABS[(i - 1) % len(_TABS)]
        state.detail_scroll = 0
        return True
    if ch == curses.KEY_RIGHT:
        try:
            i = _TABS.index(state.active_tab)
        except ValueError:
            i = -1
        state.active_tab = _TABS[(i + 1) % len(_TABS)]
        state.detail_scroll = 0
        return True
    if ch == ord("h"):
        state.focus = _FOCUS_LIST
        return True
    if ch == ord("l"):
        if state.focus == _FOCUS_LIST and layout.show_detail:
            state.focus = _FOCUS_DETAIL
            if rows:
                e = rows[state.selected_idx]
                eid = e.get("id")
                if eid is not None and int(eid) not in state.reports_cache:
                    _enqueue_reports(state, req_q, int(eid))
        return True
    if ch == ord("G"):
        if rows:
            _select_idx(state, len(rows) - 1)
        return True
    if ch == 4:  # Ctrl-D
        if rows:
            _select_idx(state, min(len(rows) - 1,
                                   state.selected_idx + height // 2))
        return True
    if ch == 21:  # Ctrl-U
        if rows:
            _select_idx(state, max(0, state.selected_idx - height // 2))
        return True
    if ch == curses.KEY_NPAGE:
        # When a fire is selected (we always have one if there are rows),
        # PgDn scrolls the right-pane updates feed rather than moving
        # selection. Use j/k or G/gg for list navigation.
        if rows:
            page = max(1, height - 4)
            state.detail_scroll = min(state.detail_scroll_max,
                                      state.detail_scroll + page)
        return True
    if ch == curses.KEY_PPAGE:
        if rows:
            page = max(1, height - 4)
            state.detail_scroll = max(0, state.detail_scroll - page)
        return True
    if ch == ord("/"):
        state.filter_active = True
        state.filter_buffer = state.filter_text
        state.filter_original = state.filter_text
        return True
    if ch == ord(":"):
        state.cmd_active = True
        state.cmd_buffer = ""
        return True
    if ch == ord("]"):
        state.within_km = min(5000.0, state.within_km + 50)
        _recompute_threats(state)
        _recompute_visible(state)
        _set_status(state, f"within = {state.within_km:g} km")
        return True
    if ch == ord("["):
        state.within_km = max(1.0, state.within_km - 50)
        _recompute_threats(state)
        _recompute_visible(state)
        _set_status(state, f"within = {state.within_km:g} km")
        return True
    if ch == 27:  # ESC
        state.focus = _FOCUS_LIST
        return True
    if ch in (curses.KEY_BACKSPACE, 8, 127, curses.KEY_HOME):
        state.focus = _FOCUS_LIST
        return True
    if ch == ord("n"):
        _jump_to_next_match(state, 1)
        return True
    if ch == ord("N"):
        _jump_to_next_match(state, -1)
        return True
    if ch in (10, 13, curses.KEY_ENTER):
        if rows:
            e = rows[state.selected_idx]
            eid = e.get("id")
            if eid is not None and int(eid) not in state.reports_cache:
                _enqueue_reports(state, req_q, int(eid))
            state.focus = _FOCUS_DETAIL
            state.detail_scroll = 0
        return True
    if ch == 9:  # Tab
        if state.focus == _FOCUS_DETAIL:
            try:
                i = _TABS.index(state.active_tab)
            except ValueError:
                i = -1
            state.active_tab = _TABS[(i + 1) % len(_TABS)]
            state.detail_scroll = 0
            return True
        order = [_FOCUS_LIST, _FOCUS_DETAIL]
        try:
            i = order.index(state.focus)
        except ValueError:
            i = -1
        state.focus = order[(i + 1) % len(order)]
        return True
    if ch == curses.KEY_BTAB:
        if state.focus == _FOCUS_DETAIL:
            try:
                i = _TABS.index(state.active_tab)
            except ValueError:
                i = 0
            state.active_tab = _TABS[(i - 1) % len(_TABS)]
            state.detail_scroll = 0
            return True
    if ch in _TAB_KEYS:
        state.active_tab = _TAB_KEYS[ch]
        state.focus = _FOCUS_DETAIL
        state.detail_scroll = 0
        if rows:
            e = rows[state.selected_idx]
            eid = e.get("id")
            if eid is not None and int(eid) not in state.reports_cache:
                _enqueue_reports(state, req_q, int(eid))
        return True
    if ch == ord("r"):
        _enqueue_refresh(state, req_q)
        if rows:
            e = rows[state.selected_idx]
            eid = e.get("id")
            if eid is not None:
                state.reports_cache.pop(int(eid), None)
                _enqueue_reports(state, req_q, int(eid))
        _set_status(state, "refreshing…")
        return True
    if ch == ord("t"):
        try:
            i = _SORT_KEYS.index(state.sort_key)
        except ValueError:
            i = -1
        nxt = _SORT_KEYS[(i + 1) % len(_SORT_KEYS)]
        if nxt == "distance" and state.near is None:
            nxt = _SORT_KEYS[(i + 2) % len(_SORT_KEYS)]
            _set_status(state, "no --near; distance sort skipped")
        state.sort_key = nxt
        _recompute_visible(state)
        return True
    if ch == ord("T"):
        state.sort_reverse = not state.sort_reverse
        _recompute_visible(state)
        return True
    if ch == ord("?"):
        state.status_msg = "__HELP__"
        state.status_msg_ts = time.monotonic()
        state.status_is_error = False
        return True
    if ch == ord("L"):
        state.live_mode = not state.live_mode
        _set_status(state, "LIVE on" if state.live_mode else "live off")
        if state.live_mode:
            state.last_live_poll_ts = 0.0
        return True
    if ch in (ord("i"), ord("F")):
        from .. import images as _img
        if not _img.supports_inline_images(sys.stdout):
            term = os.environ.get("TERM", "")
            tp = os.environ.get("TERM_PROGRAM", "")
            _set_status(state,
                        f"terminal lacks inline images "
                        f"(TERM={term} TERM_PROGRAM={tp}); needs kitty/ghostty/"
                        f"iTerm2/VS Code, or set WATCHDUTY_INLINE_IMAGES=iterm2",
                        is_error=True)
            return True
        if not rows:
            return True
        e = rows[state.selected_idx]
        eid = e.get("id")
        if eid is None:
            return True
        eid_i = int(eid)
        url = state.header_image_url or _pick_image_url(state, e)
        if not url:
            _set_status(state, "no image yet — fetching, retry in a moment")
            return True
        _enqueue_image(state, req_q, eid_i, url)
        state.image_show_for = eid_i
        state.image_show_url = url
        _set_status(state, f"loading fullscreen… ({url[-40:]})")
        return True
    if ch == ord("X") or ch == 12:  # X or Ctrl-L
        if state.filter_text or state.filter_buffer:
            state.filter_text = ""
            state.filter_buffer = ""
            state.filter_active = False
            _recompute_visible(state)
            _set_status(state, "filter cleared")
        else:
            _set_status(state, "no filter to clear")
        return True
    if ch == ord("m"):
        # Fullscreen mapscii — defer the suspend/exec to the main loop so
        # it can hand in `stdscr`.
        if not rows:
            return True
        e = rows[state.selected_idx]
        lat, lng = e.get("lat"), e.get("lng")
        if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
            _set_status(state, "selected fire has no lat/lng",
                        is_error=True)
            return True
        state.pending_mapscii = (float(lat), float(lng))
        _set_status(state,
                    f"launching mapscii @ {float(lat):.3f},{float(lng):.3f}")
        return True
    if ch == ord("z"):
        state.list_compact = not state.list_compact
        _set_status(state,
                    "compact list" if state.list_compact else "card list")
        return True
    if ch in (ord("+"), ord("=")):
        # +/- zoom mapscii when it's the active map; otherwise cycle the
        # inline-image preset.
        if _map_active:
            state.mapscii_embed.send(b"a")
            return True
        try:
            i = _IMG_SIZE_ORDER.index(state.image_size)
        except ValueError:
            i = 1
        state.image_size = _IMG_SIZE_ORDER[
            min(len(_IMG_SIZE_ORDER) - 1, i + 1)
        ]
        _set_status(state, f"image size: {state.image_size}")
        return True
    if ch in (ord("-"), ord("_")):
        if _map_active:
            state.mapscii_embed.send(b"z")
            return True
        try:
            i = _IMG_SIZE_ORDER.index(state.image_size)
        except ValueError:
            i = 1
        state.image_size = _IMG_SIZE_ORDER[max(0, i - 1)]
        _set_status(state, f"image size: {state.image_size}")
        return True
    if ch in (ord("p"), ord("P")):
        state.header_image_enabled = not state.header_image_enabled
        state.header_image_last_fire = None
        state.header_image_last_paint_ts = 0.0
        _set_status(state,
                    "camera ON" if state.header_image_enabled else "camera off")
        return True
    if ch == curses.KEY_MOUSE:
        _handle_mouse(state, req_q, layout)
        return True
    return False
