"""Pane geometry (:class:`_Layout`) + the bounds-safe ``_addnstr`` writer."""

from __future__ import annotations

import curses
from dataclasses import dataclass

from .helpers import _safe_str
from .state import _LIST_W_MAX, _MIN_COLS, _MIN_LINES


@dataclass
class _Layout:
    """Rectangular extents after each draw."""
    lines: int
    cols: int
    list_w: int
    detail_w: int
    body_top: int
    body_bot: int  # exclusive
    too_small: bool
    show_detail: bool


def _compute_layout(lines: int, cols: int) -> _Layout:
    """Compute pane geometry. List ≈ 42% of cols, capped at `_LIST_W_MAX`."""
    cols = max(0, int(cols))
    lines = max(0, int(lines))
    too_small = lines < _MIN_LINES or cols < _MIN_COLS
    show_detail = cols >= 80
    if not show_detail:
        list_w = cols
        detail_w = 0
    else:
        list_target = int(cols * 0.42)
        list_w = max(34, min(list_target, _LIST_W_MAX, cols - 40))
        detail_w = max(0, cols - list_w)
    return _Layout(
        lines=lines,
        cols=cols,
        list_w=list_w,
        detail_w=detail_w,
        body_top=2,                 # status row + divider row
        body_bot=max(3, lines - 1),
        too_small=too_small,
        show_detail=show_detail,
    )


def _addnstr(win, y: int, x: int, s: str, n: int, attr: int = 0) -> None:
    """Bounded :meth:`addnstr` that swallows the bottom-right curses error."""
    if n <= 0 or y < 0 or x < 0:
        return
    s = _safe_str(s)
    if len(s) > n:
        s = s[:n]
    try:
        win.addnstr(y, x, s, n, attr)
    except curses.error:
        pass
