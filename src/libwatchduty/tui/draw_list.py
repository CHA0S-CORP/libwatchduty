"""Fire-list pane: header, legend, card + compact rows, cell renderers."""

from __future__ import annotations

import curses

from .helpers import (
    _bearing_arrow,
    _initial_bearing,
    _is_planned,
    _threat_bar_glyphs,
    _threat_tier,
)
from .layout import _Layout, _addnstr
from .palette import _attr, _on_bg
from .state import _LIST_ROWS_PER_FIRE, _TuiState


# ---------------------------------------------------------------------------
# bar / chip renderers
# ---------------------------------------------------------------------------

def _draw_threat_cell(
    win, y: int, x: int, max_w: int, score: float | None,
    holder: dict, planned: bool,
) -> int:
    """Render `[score] ▰▰▱` in the threat column."""
    if max_w <= 0:
        return 0
    tier = "dimmer" if planned else _threat_tier(score)
    val = "—" if score is None else f"{int(round(score))}"
    bar = _threat_bar_glyphs(0.0 if planned else score)
    cell = f"{val:>3} {bar}"
    _addnstr(win, y, x, val.rjust(3), min(3, max_w),
             _attr(tier, holder) | curses.A_BOLD)
    bar_x = x + 4
    if bar_x < x + max_w:
        _addnstr(win, y, bar_x, bar, min(len(bar), max_w - 4),
                 _attr(tier, holder))
    return min(max_w, len(cell))


def _draw_containment_cell(
    win, y: int, x: int, max_w: int, cont: float | None,
    holder: dict, *, planned: bool,
) -> int:
    """10-cell containment bar with `%` / `n/a` / `PLANNED` suffix."""
    if max_w <= 0:
        return 0
    if planned:
        text = "PLANNED"
        _addnstr(win, y, x, text, min(len(text), max_w),
                 _attr("dimmer", holder) | curses.A_BOLD)
        return min(len(text), max_w)
    cells = 10
    if cont is None:
        track = "·" * cells
        _addnstr(win, y, x, track, min(cells, max_w),
                 _attr("dimmer", holder))
        suffix = " n/a"
        _addnstr(win, y, x + cells, suffix, max(0, max_w - cells),
                 _attr("dimmer", holder))
        return min(max_w, cells + len(suffix))
    p = max(0.0, min(100.0, float(cont)))
    full = int(round(p / 10.0))
    if full > 0:
        _addnstr(win, y, x, "█" * full, min(full, max_w),
                 _attr("green", holder))
    if full < cells:
        track = "·" * (cells - full)
        _addnstr(win, y, x + full, track, max(0, max_w - full),
                 _attr("dimmer", holder))
    suffix = f" {int(round(p))}%"
    _addnstr(win, y, x + cells, suffix, max(0, max_w - cells),
             _attr("text", holder))
    return min(max_w, cells + len(suffix))


def _draw_chip(
    win, y: int, x: int, max_w: int, label: str,
    role: str, holder: dict,
) -> int:
    """Pad with a space on each side; draw with chip color pair."""
    text = f" {label} "
    n = min(len(text), max(0, max_w))
    if n <= 0:
        return 0
    _addnstr(win, y, x, text, n, _attr(role, holder))
    return n


# ---------------------------------------------------------------------------
# fire list
# ---------------------------------------------------------------------------

# Shared column geometry — header AND data rows use these offsets so
# everything lines up vertically.
_LIST_PAD_L = 2
_LIST_THREAT_W = 8   # "100 ▰▰▰" + 1 pad
_LIST_DIR_W = 2
# Offsets within the "sub-line" / row B area (relative to NAME_X).
_LIST_SUB_DIST_OFF = 0
_LIST_SUB_DIST_W = 9     # "999.9 km " (8 chars + trailing space)
_LIST_SUB_SIZE_OFF = _LIST_SUB_DIST_OFF + _LIST_SUB_DIST_W
_LIST_SUB_SIZE_W = 9     # "9999 ac "
_LIST_SUB_CONT_OFF = _LIST_SUB_SIZE_OFF + _LIST_SUB_SIZE_W
_LIST_SUB_CONT_W = 16
_LIST_NAME_X = _LIST_PAD_L + _LIST_THREAT_W + _LIST_DIR_W + 1


def _list_pane_attr(role: str, bg: str | None, holder: dict) -> int:
    """Pick the appropriate fg-on-bg attribute for the given pane bg."""
    if bg is None:
        return _attr(role, holder)
    return _on_bg(role, bg, holder)


def _draw_list_header(stdscr, layout: _Layout, holder: dict) -> None:
    """Two-line column header at the top of the list pane."""
    top = layout.body_top
    width = layout.list_w
    if width <= 0:
        return
    attr = _on_bg("dimmer", "panel_bg", holder)
    _addnstr(stdscr, top, 0, " " * width, width, attr)
    _addnstr(stdscr, top + 1, 0, " " * width, width, attr)
    xn = _LIST_NAME_X
    _addnstr(stdscr, top, _LIST_PAD_L, "THREAT",
             min(6, width - _LIST_PAD_L), attr)
    _addnstr(stdscr, top, _LIST_PAD_L + _LIST_THREAT_W, "D", 1, attr)
    if xn < width:
        _addnstr(stdscr, top, xn, "INCIDENT", width - xn, attr)
    # Sub-line: DIST | SIZE | CONTAINMENT in their fixed columns.
    if xn < width:
        dx = xn + _LIST_SUB_DIST_OFF
        sx = xn + _LIST_SUB_SIZE_OFF
        cx = xn + _LIST_SUB_CONT_OFF
        if dx < width:
            _addnstr(stdscr, top + 1, dx,
                     "DIST".ljust(_LIST_SUB_DIST_W),
                     min(_LIST_SUB_DIST_W, width - dx), attr)
        if sx < width:
            _addnstr(stdscr, top + 1, sx,
                     "SIZE".ljust(_LIST_SUB_SIZE_W),
                     min(_LIST_SUB_SIZE_W, width - sx), attr)
        if cx < width:
            _addnstr(stdscr, top + 1, cx,
                     "CONTAINMENT".ljust(_LIST_SUB_CONT_W),
                     min(_LIST_SUB_CONT_W, width - cx), attr)
    # Bottom hairline.
    if top + 2 < layout.body_bot:
        _addnstr(stdscr, top + 2, 0, "─" * width, width,
                 _on_bg("dimmer", "panel_bg", holder))


def _draw_list_legend(stdscr, layout: _Layout, holder: dict) -> None:
    """Bottom legend strip — top border + 1-line legend."""
    width = layout.list_w
    if width <= 0:
        return
    y_bot = layout.body_bot - 1
    if y_bot - 1 >= layout.body_top:
        _addnstr(stdscr, y_bot - 1, 0, "─" * width, width,
                 _on_bg("dimmer", "panel_bg", holder))
    legend = "▰ threat = prox×size×(1−cont)×growth×wind  · ▲ growing"
    attr = _on_bg("dimmer", "panel_bg", holder)
    _addnstr(stdscr, y_bot, 0, " " * width, width, attr)
    _addnstr(stdscr, y_bot, _LIST_PAD_L, legend, max(0, width - _LIST_PAD_L), attr)


def _draw_list(stdscr, state: _TuiState, layout: _Layout, holder: dict) -> None:
    """Fire list — picks compact 1-row or card 2-row mode."""
    width = layout.list_w
    if width <= 0:
        return

    _draw_list_header(stdscr, layout, holder)
    _draw_list_legend(stdscr, layout, holder)

    # Body region.
    top = layout.body_top + 3   # +2 header rows + 1 hairline
    bot = layout.body_bot - 2   # -1 hairline -1 legend row
    body_h = max(0, bot - top)
    rows = state.visible_fires
    if not rows:
        msg = "no fires match your filters" if state.fires else "loading…"
        _addnstr(stdscr, top, _LIST_PAD_L, msg,
                 max(0, width - _LIST_PAD_L),
                 _on_bg("dimmer", "panel_bg", holder))
        for ry in range(top, bot):
            _addnstr(stdscr, ry, 0, " " * width, width,
                     _on_bg("text", "panel_bg", holder))
        return

    if state.list_compact:
        _draw_list_compact(stdscr, state, layout, holder,
                           top=top, bot=bot, width=width)
    else:
        _draw_list_cards(stdscr, state, layout, holder,
                         top=top, bot=bot, width=width, body_h=body_h)
    _draw_list_badge(stdscr, state, layout, holder)


def _draw_list_badge(
    stdscr, state: _TuiState, layout: _Layout, holder: dict,
) -> None:
    """Show "i/N" in the bottom-right of the list pane so the user can see
    selection position and total at a glance."""
    rows = state.visible_fires
    if not rows:
        return
    badge = f" {state.selected_idx + 1}/{len(rows)} "
    by = layout.body_bot - 2
    bx = max(0, layout.list_w - len(badge) - 1)
    if bx > 0 and by > layout.body_top:
        _addnstr(stdscr, by, bx, badge, len(badge),
                 _on_bg("dimmer", "panel_bg", holder) | curses.A_REVERSE)


def _draw_list_cards(
    stdscr, state: _TuiState, layout: _Layout, holder: dict,
    *, top: int, bot: int, width: int, body_h: int,
) -> None:
    """Original 2-row card-per-fire layout."""
    rows = state.visible_fires
    cards_per_view = max(1, body_h // _LIST_ROWS_PER_FIRE)
    state.list_scroll_max = max(0, len(rows) - cards_per_view)

    if state.selected_idx < state.list_scroll:
        state.list_scroll = state.selected_idx
    elif state.selected_idx >= state.list_scroll + cards_per_view:
        state.list_scroll = state.selected_idx - cards_per_view + 1
    state.list_scroll = max(0, min(state.list_scroll, state.list_scroll_max))

    needle = state.filter_text.strip().lower()

    for ry in range(top, bot):
        _addnstr(stdscr, ry, 0, " " * width, width,
                 _on_bg("text", "panel_bg", holder))

    for card_i in range(cards_per_view):
        idx = state.list_scroll + card_i
        if idx >= len(rows):
            break
        y_a = top + card_i * _LIST_ROWS_PER_FIRE
        y_b = y_a + 1
        if y_a >= bot:
            break
        e = rows[idx]
        eid = e.get("id")
        eid_i = int(eid) if eid is not None else -1
        is_sel = (idx == state.selected_idx)
        planned = _is_planned(e)
        cont_raw = (e.get("data") or {}).get("containment")
        cont = float(cont_raw) if isinstance(cont_raw, (int, float)) else None
        is_active = bool(e.get("is_active"))
        acres = (e.get("data") or {}).get("acreage")
        score = state.threat_scores.get(eid_i) if eid_i >= 0 else None

        # Zebra stripe between fires for clearer row separation.
        if is_sel:
            bg = "sel_bg"
        elif idx % 2 == 0:
            bg = "panel_bg"
        else:
            bg = "panel_alt_bg"
        row_fill = _on_bg("text", bg, holder)

        # Paint both rows with the card's bg first.
        for ry in (y_a, y_b):
            if ry < bot:
                _addnstr(stdscr, ry, 0, " " * width, width, row_fill)
        if is_sel:
            # Solid cyan-on-sel left accent block (2 cols, both rows).
            accent = _on_bg("cyan", "sel_bg", holder) | curses.A_BOLD
            for ry in (y_a, y_b):
                if ry < bot:
                    _addnstr(stdscr, ry, 0, " ", 1, accent | curses.A_REVERSE)

        # --- ROW A ---
        # THREAT cell: large bold score + bar
        tier = "dimmer" if planned else _threat_tier(score)
        if score is None:
            val = "—"
        else:
            val = f"{int(round(score))}"
        bar = _threat_bar_glyphs(0.0 if planned else score)
        t_attr = _on_bg(tier, bg, holder) | curses.A_BOLD
        _addnstr(stdscr, y_a, _LIST_PAD_L, val.rjust(3), 3, t_attr)
        _addnstr(stdscr, y_a, _LIST_PAD_L + 4, bar, len(bar),
                 _on_bg(tier, bg, holder))

        # DIR arrow
        arrow = "·"
        if (state.near is not None
                and isinstance(e.get("lat"), (int, float))
                and isinstance(e.get("lng"), (int, float))):
            brg = _initial_bearing(state.near, (float(e["lat"]), float(e["lng"])))
            arrow = _bearing_arrow(brg)
        _addnstr(stdscr, y_a, _LIST_PAD_L + _LIST_THREAT_W, arrow, 1,
                 _on_bg("dimmer" if planned else "nav", bg, holder)
                 | curses.A_BOLD)

        # NAME (large/bold)
        name = e.get("name") or "(unnamed)"
        grow = (eid_i in state.grown_fire_ids and not planned)
        if planned:
            name_role = "dimmer"
        elif is_active and (cont is None or cont < 50):
            name_role = "red"
        elif cont and cont >= 50:
            name_role = "dim"
        else:
            name_role = "bright"
        if is_sel:
            name_role = "bright"
        n_attr = _on_bg(name_role, bg, holder) | curses.A_BOLD
        name_x = _LIST_NAME_X
        name_w = max(0, width - name_x - 1)
        if grow:
            grow_str = "▲ "
            _addnstr(stdscr, y_a, name_x, grow_str, min(2, name_w),
                     _on_bg("red", bg, holder) | curses.A_BOLD)
            name_x += 2
            name_w = max(0, name_w - 2)
        _addnstr(stdscr, y_a, name_x, name[:name_w], name_w, n_attr)
        if needle and needle in name.lower():
            pos = name.lower().find(needle)
            if 0 <= pos < name_w:
                match_len = min(len(needle), name_w - pos)
                if match_len > 0:
                    _addnstr(stdscr, y_a, name_x + pos,
                             name[pos:pos + match_len], match_len,
                             n_attr | curses.A_UNDERLINE)

        # --- ROW B (sub line) ---
        if y_b >= bot:
            continue
        sub_x = _LIST_NAME_X
        # DIST col (right-aligned within its slot for tidy decimals).
        if eid_i in state.distances:
            dtxt = f"{state.distances[eid_i]:.1f} km"
        else:
            dtxt = "— km"
        dtxt = dtxt.rjust(_LIST_SUB_DIST_W - 1) + " "
        # SIZE col.
        if acres and float(acres) > 0:
            stxt = f"{int(float(acres))} ac"
        else:
            stxt = "— ac"
        stxt = stxt.rjust(_LIST_SUB_SIZE_W - 1) + " "

        sub_role = "dimmer" if planned else ("dim" if not is_sel else "bright")
        s_attr = _on_bg(sub_role, bg, holder)
        dx = sub_x + _LIST_SUB_DIST_OFF
        sx = sub_x + _LIST_SUB_SIZE_OFF
        cx = sub_x + _LIST_SUB_CONT_OFF
        _addnstr(stdscr, y_b, dx, dtxt,
                 max(0, min(len(dtxt), width - dx - 1)), s_attr)
        _addnstr(stdscr, y_b, sx, stxt,
                 max(0, min(len(stxt), width - sx - 1)), s_attr)

        # CONTAINMENT column (10 bar + suffix).
        cont_room = max(0, min(_LIST_SUB_CONT_W, width - cx - 1))
        cells = 10
        if planned:
            _addnstr(stdscr, y_b, cx, "PLANNED",
                     cont_room,
                     _on_bg("dimmer", bg, holder) | curses.A_BOLD)
        elif cont is None:
            _addnstr(stdscr, y_b, cx, "·" * cells,
                     min(cells, cont_room),
                     _on_bg("dimmer", bg, holder))
            _addnstr(stdscr, y_b, cx + cells, " n/a",
                     max(0, cont_room - cells),
                     _on_bg("dimmer", bg, holder))
        else:
            p = max(0.0, min(100.0, float(cont)))
            full = int(round(p / 10.0))
            if full > 0:
                _addnstr(stdscr, y_b, cx, "█" * full,
                         min(full, cont_room),
                         _on_bg("green", bg, holder))
            if full < cells:
                _addnstr(stdscr, y_b, cx + full, "·" * (cells - full),
                         max(0, cont_room - full),
                         _on_bg("dimmer", bg, holder))
            suffix = f" {int(round(p))}%"
            _addnstr(stdscr, y_b, cx + cells, suffix,
                     max(0, cont_room - cells), s_attr)


def _draw_list_compact(
    stdscr, state: _TuiState, layout: _Layout, holder: dict,
    *, top: int, bot: int, width: int,
) -> None:
    """One-row-per-fire compact mode: score · arrow · NAME · dist · cont."""
    rows = state.visible_fires
    body_h = max(0, bot - top)
    state.list_scroll_max = max(0, len(rows) - body_h)

    if state.selected_idx < state.list_scroll:
        state.list_scroll = state.selected_idx
    elif state.selected_idx >= state.list_scroll + body_h:
        state.list_scroll = state.selected_idx - body_h + 1
    state.list_scroll = max(0, min(state.list_scroll, state.list_scroll_max))

    needle = state.filter_text.strip().lower()

    for i in range(body_h):
        idx = state.list_scroll + i
        y_r = top + i
        if y_r >= bot:
            break
        if idx >= len(rows):
            _addnstr(stdscr, y_r, 0, " " * width, width,
                     _on_bg("text", "panel_bg", holder))
            continue
        e = rows[idx]
        eid = e.get("id")
        eid_i = int(eid) if eid is not None else -1
        is_sel = (idx == state.selected_idx)
        planned = _is_planned(e)
        cont_raw = (e.get("data") or {}).get("containment")
        cont = float(cont_raw) if isinstance(cont_raw, (int, float)) else None
        is_active = bool(e.get("is_active"))
        score = state.threat_scores.get(eid_i) if eid_i >= 0 else None

        # Zebra stripe.
        if is_sel:
            bg = "sel_bg"
        elif idx % 2 == 0:
            bg = "panel_bg"
        else:
            bg = "panel_alt_bg"
        _addnstr(stdscr, y_r, 0, " " * width, width,
                 _on_bg("text", bg, holder))
        if is_sel:
            accent = _on_bg("cyan", "sel_bg", holder) | curses.A_BOLD | curses.A_REVERSE
            _addnstr(stdscr, y_r, 0, " ", 1, accent)

        # Cursor for left→right packing.
        x = _LIST_PAD_L
        # THREAT score (3 chars) + 3-cell bar.
        tier = "dimmer" if planned else _threat_tier(score)
        val = "—" if score is None else f"{int(round(score))}"
        bar = _threat_bar_glyphs(0.0 if planned else score)
        _addnstr(stdscr, y_r, x, val.rjust(3), 3,
                 _on_bg(tier, bg, holder) | curses.A_BOLD)
        x += 4
        _addnstr(stdscr, y_r, x, bar, 3, _on_bg(tier, bg, holder))
        x += 4

        # DIR arrow.
        arrow = "·"
        if (state.near is not None
                and isinstance(e.get("lat"), (int, float))
                and isinstance(e.get("lng"), (int, float))):
            brg = _initial_bearing(state.near, (float(e["lat"]), float(e["lng"])))
            arrow = _bearing_arrow(brg)
        _addnstr(stdscr, y_r, x, arrow, 1,
                 _on_bg("dimmer" if planned else "nav", bg, holder)
                 | curses.A_BOLD)
        x += 2

        # Force NAME to start at the shared INCIDENT column so it aligns
        # with the card-mode rows and the header.
        x = _LIST_NAME_X
        right_budget = 22 if width >= 56 else 14
        name_w = max(8, width - x - right_budget - 1)
        name = e.get("name") or "(unnamed)"
        if planned:
            name_role = "dimmer"
        elif is_active and (cont is None or cont < 50):
            name_role = "red"
        elif cont and cont >= 50:
            name_role = "dim"
        else:
            name_role = "bright"
        if is_sel:
            name_role = "bright"
        n_attr = _on_bg(name_role, bg, holder) | curses.A_BOLD
        prefix = ""
        if eid_i in state.grown_fire_ids and not planned:
            prefix = "▲ "
            _addnstr(stdscr, y_r, x, prefix, 2,
                     _on_bg("red", bg, holder) | curses.A_BOLD)
            x += 2
            name_w = max(4, name_w - 2)
        _addnstr(stdscr, y_r, x, name[:name_w], name_w, n_attr)
        if needle and needle in name.lower():
            pos = name.lower().find(needle)
            if 0 <= pos < name_w:
                match_len = min(len(needle), name_w - pos)
                if match_len > 0:
                    _addnstr(stdscr, y_r, x + pos,
                             name[pos:pos + match_len], match_len,
                             n_attr | curses.A_UNDERLINE)

        # Right-flush metrics: DIST · CONT%.
        rx = width - 1
        # Containment % (5 cells).
        if planned:
            ct = "PLAN"
            ct_role = "dimmer"
        elif cont is None:
            ct = "n/a "
            ct_role = "dimmer"
        else:
            ct = f"{int(round(cont)):>3}%"
            ct_role = "green" if cont >= 50 else "amber"
        ct = ct.rjust(5)
        rx -= len(ct)
        if rx > x + 2:
            _addnstr(stdscr, y_r, rx, ct, len(ct),
                     _on_bg(ct_role, bg, holder))
        # Spacer.
        rx -= 3
        # DIST (8 cells).
        if eid_i in state.distances:
            dtxt = f"{state.distances[eid_i]:5.1f}km"
        else:
            dtxt = "    —  "
        if rx > x + len(dtxt):
            _addnstr(stdscr, y_r, rx - len(dtxt), dtxt, len(dtxt),
                     _on_bg("dim" if not is_sel else "bright", bg, holder))
