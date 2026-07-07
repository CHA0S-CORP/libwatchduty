"""Dashboard chrome: status bar, keybind footer, and help overlay."""

from __future__ import annotations

import curses
import time

from .layout import _Layout, _addnstr
from .palette import _attr, _on_bg
from .state import _ERROR_TTL, _FOCUS_LIST, _TuiState


# ---------------------------------------------------------------------------
# status bar
# ---------------------------------------------------------------------------

def _refresh_meter(state: _TuiState) -> str:
    """`▓▓▓░ 48s` countdown to next auto-refresh."""
    if not state.auto_refresh:
        return "manual"
    if state.last_refresh_ts <= 0:
        return f"… {state.auto_refresh}s"
    age = time.monotonic() - state.last_refresh_ts
    left = max(0, int(state.auto_refresh - age))
    pct = 1.0 - (left / state.auto_refresh)
    cells = 4
    filled = max(0, min(cells, int(round(pct * cells))))
    bar = "▓" * filled + "░" * (cells - filled)
    return f"{bar} {left}s"


def _draw_header(stdscr, state: _TuiState, layout: _Layout, holder: dict) -> None:
    """Status bar — row 0 = dark strip, row 1 = thick divider."""
    cols = layout.cols
    fill = _on_bg("text", "status_bg", holder)
    div_attr = _on_bg("dimmer", "panel_bg", holder)
    _addnstr(stdscr, 0, 0, " " * cols, cols, fill)
    _addnstr(stdscr, 1, 0, "━" * cols, cols, div_attr)
    x = 2  # leading gutter

    def write(text: str, role: str = "text", bold: bool = False) -> None:
        nonlocal x
        if x >= cols:
            return
        attr = _on_bg(role, "status_bg", holder)
        if bold:
            attr |= curses.A_BOLD
        n = min(len(text), cols - x)
        _addnstr(stdscr, 0, x, text, n, attr)
        x += n

    def sep() -> None:
        nonlocal x
        if x >= cols - 3:
            return
        # blank-pipe-blank as a clear chunk divider.
        _addnstr(stdscr, 0, x, "    │    ", min(9, cols - x),
                 _on_bg("dimmer", "status_bg", holder))
        x += 9

    write("◉ watchduty", "green", bold=True)
    sep()
    write("filters ", "dimmer")
    write(",".join(state.types or ()) or "—", "text")
    sep()
    write("⌖ ", "dimmer")
    if state.near is not None:
        write(f"{state.near[0]:.2f},{state.near[1]:.2f}", "text", bold=True)
        write(f"  ≤{int(state.within_km)}km", "dimmer")
        if state.near_source:
            write(f"  ({state.near_source})", "cyan")
    else:
        write("no --near", "dimmer")
    sep()
    write("sort ", "dimmer")
    write(f"▼ {state.sort_key.upper()}", "amber", bold=True)
    if state.sort_reverse:
        write(" ↑", "amber")
    sep()
    write(f"{len(state.visible_fires)}", "text", bold=True)
    write(f" of {len(state.fires)}", "dimmer")
    sep()
    write("refresh ", "dimmer")
    write(_refresh_meter(state), "text")

    if state.live_mode and cols >= 10:
        pill = "  ● LIVE  "
        px = cols - len(pill) - 1
        if px > x + 1:
            _addnstr(stdscr, 0, px, pill, len(pill),
                     _attr("live", holder))


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

def _draw_footer(stdscr, state: _TuiState, layout: _Layout, holder: dict) -> None:
    """Bottom keybind row."""
    cols = layout.cols
    y = layout.lines - 1
    if state.filter_active:
        prompt = f"FILTER: {state.filter_buffer}_"
        _addnstr(stdscr, y, 0, prompt.ljust(cols), cols,
                 _attr("amber", holder))
        return
    if state.cmd_active:
        prompt = f":{state.cmd_buffer}_"
        _addnstr(stdscr, y, 0, prompt.ljust(cols), cols,
                 _attr("amber", holder))
        return
    if state.status_msg and (time.monotonic() - state.status_msg_ts) < _ERROR_TTL:
        attr = _attr("error", holder) if state.status_is_error \
            else _attr("dim", holder)
        _addnstr(stdscr, y, 0, state.status_msg.ljust(cols), cols, attr)
        return

    focus_chip = "[LIST]" if state.focus == _FOCUS_LIST else "[DETAIL]"
    if state.refresh_in_flight:
        left = f"{focus_chip} REFRESHING"
    elif state.filter_text:
        left = f"{focus_chip} FILTER: {state.filter_text}"
    else:
        left = f"{focus_chip}  Tab switches pane · j/k context-aware"
    _addnstr(stdscr, y, 0, " " * cols, cols, _attr("dim", holder))
    _addnstr(stdscr, y, 0, left, min(len(left), cols),
             _attr("amber", holder) | curses.A_BOLD)

    parts = [
        ("j/k", "move"),
        ("/", "filter"),
        ("⏎", "load"),
        ("r", "refresh"),
        ("L", "live"),
        ("i", "image"),
        ("t", "sort"),
        ("1-4", "tab"),
        ("?", "help"),
        ("q", "quit"),
    ]
    cursor = len(left) + 2
    for key, lab in parts:
        if cursor >= cols:
            break
        kn = min(len(key), cols - cursor)
        _addnstr(stdscr, y, cursor, key, kn,
                 _attr("amber", holder) | curses.A_BOLD)
        cursor += kn
        sn = min(1, cols - cursor)
        if sn:
            _addnstr(stdscr, y, cursor, " ", sn, _attr("dim", holder))
            cursor += sn
        ln = min(len(lab) + 2, cols - cursor)
        if ln <= 0:
            break
        _addnstr(stdscr, y, cursor, lab + "  ", ln, _attr("dim", holder))
        cursor += ln


def _draw_help_overlay(stdscr, layout: _Layout, holder: dict) -> None:
    """Floating help box; dismissed on next keypress."""
    lines_help = [
        "watchduty tui — keybindings",
        "",
        "  j / k / ↓ / ↑    focus-aware: list nav OR updates scroll",
        "  gg / G           top / bottom         Ctrl-d/u     half page",
        "  J / K            ALWAYS scroll updates feed",
        "  PgDn / PgUp      page-scroll updates feed",
        "  ←/→              cycle tabs back / fwd",
        "  Tab / Shift-Tab  cycle focus / tabs",
        "  1 / 2 / 3 / 4    jump tab: updates / radio / map / evac",
        "  R / c / e / u    alias jumps to radio / map / evac / updates",
        "  z                toggle compact fire list",
        "  m                fullscreen mapscii on selected fire",
        "  + / -            cycle inline image size (small/med/large)",
        "  /                filter   n / N      next / prev match",
        "  X / Ctrl-L       clear current filter",
        "  :                command prompt — :within :near :types :sort",
        "  [ / ]            ±50 km on --within",
        "  r                refresh fires + updates",
        "  L                toggle LIVE polling",
        "  t / T            cycle sort / reverse",
        "  i                fullscreen the live camera frame",
        "  P                toggle inline camera thumb",
        "  Esc / ⌫ / Home   back to fire list",
        "  ?                this help",
        "  q / Ctrl-C       quit",
        "",
        "  press any key to dismiss",
    ]
    h = min(len(lines_help) + 2, layout.lines)
    w = min(72, layout.cols)
    y0 = max(0, (layout.lines - h) // 2)
    x0 = max(0, (layout.cols - w) // 2)
    try:
        win = curses.newwin(h, w, y0, x0)
        win.box()
        for i, text in enumerate(lines_help[:h - 2]):
            _addnstr(win, i + 1, 2, text, w - 4, _attr("text", holder))
        win.refresh()
    except curses.error:
        pass
