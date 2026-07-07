"""Color tables + curses attribute lookup (role → attr, pyte → pair)."""

from __future__ import annotations

import curses
from typing import Any


# Foreground colors per role (256-color index → exact design hex match).
_ROLE_256: dict[str, int] = {
    "text":     252,
    "bright":   255,
    "dim":      247,
    "dimmer":   243,
    "faint":    245,
    "red":      203,
    "amber":    179,
    "green":     78,
    "cyan":      75,
    "nav":      110,
    "magenta":  177,
}

# Semantic role → base-8 fallback fg.
_ROLE_BASE: dict[str, int] = {
    "text":     -1,
    "bright":   curses.COLOR_WHITE,
    "dim":      -1,
    "dimmer":   -1,
    "faint":    -1,
    "red":      curses.COLOR_RED,
    "amber":    curses.COLOR_YELLOW,
    "green":    curses.COLOR_GREEN,
    "cyan":     curses.COLOR_CYAN,
    "nav":      curses.COLOR_CYAN,
    "magenta":  curses.COLOR_MAGENTA,
}

_BOLD_ROLES = {"red", "amber", "magenta"}

# Inverse-style chips: (role_for_fg, role_for_bg).
_CHIP_PAIRS = {
    "chip_air":  ("bright", "cyan"),
    "chip_res":  ("bright", "amber"),
    "live":      ("bright", "green"),
    "new_chip":  ("bright", "red"),
    "sel_block": ("cyan",   "sel_bg"),  # cyan-on-#16314c left accent cell
}

# Distinct backgrounds (256-color indexes closest to design hex).
_BG_256: dict[str, int] = {
    "status_bg":     233,  # #11151b
    "panel_bg":      232,  # #0f1318
    "panel_alt_bg":  234,  # zebra stripe — slightly lighter than panel_bg
    "sel_bg":         17,  # #16314c
}

# Allocated palette indexes when curses.can_change_color() is True.
# Use indexes far above 16 to avoid stomping the user's theme.
_PALETTE_BASE = 100  # 100..120 reserved
_PALETTE_HEX: list[tuple[str, str]] = [
    ("text",     "#d4dae0"),
    ("bright",   "#e8eef4"),
    ("dim",      "#9aa4b0"),
    ("dimmer",   "#6b7681"),
    ("faint",    "#8a929c"),
    ("red",      "#ff6a5f"),
    ("amber",    "#e3b341"),
    ("green",    "#3fb950"),
    ("cyan",     "#58c5ff"),
    ("nav",      "#6fb3d6"),
    ("magenta",  "#c98bff"),
    ("status_bg",   "#11151b"),
    ("panel_bg",    "#0f1318"),
    ("panel_alt_bg","#161b22"),
    ("sel_bg",      "#16314c"),
]

# Bg-aware text pairs: each `<fg>_on_<bg>` (status/panel/sel) used across panes.
_ON_BG_PAIRS = (
    ("text",   "status_bg"), ("dim",    "status_bg"),
    ("dimmer", "status_bg"), ("amber",  "status_bg"),
    ("green",  "status_bg"), ("cyan",   "status_bg"),
    ("bright", "status_bg"),
    ("text",   "panel_bg"),  ("dim",    "panel_bg"),
    ("dimmer", "panel_bg"),  ("faint",  "panel_bg"),
    ("red",    "panel_bg"),  ("amber",  "panel_bg"),
    ("green",  "panel_bg"),  ("cyan",   "panel_bg"),
    ("nav",    "panel_bg"),  ("magenta","panel_bg"),
    ("bright", "panel_bg"),
    ("text",   "panel_alt_bg"),  ("dim",    "panel_alt_bg"),
    ("dimmer", "panel_alt_bg"),  ("faint",  "panel_alt_bg"),
    ("red",    "panel_alt_bg"),  ("amber",  "panel_alt_bg"),
    ("green",  "panel_alt_bg"),  ("cyan",   "panel_alt_bg"),
    ("nav",    "panel_alt_bg"),  ("magenta","panel_alt_bg"),
    ("bright", "panel_alt_bg"),
    ("text",   "sel_bg"),    ("dim",    "sel_bg"),
    ("dimmer", "sel_bg"),    ("red",    "sel_bg"),
    ("amber",  "sel_bg"),    ("green",  "sel_bg"),
    ("cyan",   "sel_bg"),    ("nav",    "sel_bg"),
    ("bright", "sel_bg"),
)


def _hex_to_curses(rgb: str) -> tuple[int, int, int]:
    """`#rrggbb` → (r, g, b) in 0..1000 scale that ``init_color`` expects."""
    h = rgb.lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return (r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)


def _init_colors(state_holder: dict) -> bool:
    """Build pairs + role-name → curses-attr map in ``state_holder['attr']``."""
    state_holder["attr"] = {}
    if not curses.has_colors():
        return False
    try:
        curses.start_color()
        try:
            curses.use_default_colors()
            term_bg = -1
        except curses.error:
            term_bg = curses.COLOR_BLACK
        use_256 = curses.COLORS >= 256

        # Allocate custom palette when supported so the colors hit the
        # exact design hex; otherwise fall back to the 256-cube indexes.
        custom = use_256 and curses.can_change_color() and curses.COLORS >= 256
        resolved: dict[str, int] = {}
        if custom:
            for i, (name, hexv) in enumerate(_PALETTE_HEX):
                idx = _PALETTE_BASE + i
                try:
                    curses.init_color(idx, *_hex_to_curses(hexv))
                    resolved[name] = idx
                except curses.error:
                    custom = False
                    break
        if not custom:
            for name in _ROLE_256:
                resolved[name] = _ROLE_256[name] if use_256 else _ROLE_BASE.get(name, -1)
            for name, idx in _BG_256.items():
                resolved[name] = idx if use_256 else (
                    curses.COLOR_BLUE if name == "sel_bg" else curses.COLOR_BLACK
                )

        pair_id = 1
        attr_map: dict[str, int] = {}

        # 1) Plain fg roles on default bg.
        for role in _ROLE_256:
            fg = resolved.get(role, -1)
            if fg == -1:
                fg = curses.COLOR_WHITE
            try:
                curses.init_pair(pair_id, fg, term_bg)
                a = curses.color_pair(pair_id)
                if role in _BOLD_ROLES:
                    a |= curses.A_BOLD
                attr_map[role] = a
            except curses.error:
                attr_map[role] = curses.A_NORMAL
            pair_id += 1

        # 2) Chip pairs (inverse-style fg on bg).
        for name, (fg_role, bg_role) in _CHIP_PAIRS.items():
            fg = resolved.get(fg_role, curses.COLOR_WHITE)
            bg_col = resolved.get(bg_role, curses.COLOR_BLUE)
            if fg == -1:
                fg = curses.COLOR_WHITE
            if bg_col == -1:
                bg_col = curses.COLOR_BLUE
            try:
                curses.init_pair(pair_id, fg, bg_col)
                attr_map[name] = curses.color_pair(pair_id) | curses.A_BOLD
            except curses.error:
                attr_map[name] = curses.A_REVERSE | curses.A_BOLD
            pair_id += 1

        # 3) Bg-aware fg-on-bg pairs (status / panel / sel).
        for fg_role, bg_role in _ON_BG_PAIRS:
            fg = resolved.get(fg_role, curses.COLOR_WHITE)
            bg_col = resolved.get(bg_role, curses.COLOR_BLACK)
            if fg == -1:
                fg = curses.COLOR_WHITE
            if bg_col == -1:
                bg_col = curses.COLOR_BLACK
            try:
                curses.init_pair(pair_id, fg, bg_col)
                a = curses.color_pair(pair_id)
                if fg_role in _BOLD_ROLES:
                    a |= curses.A_BOLD
                attr_map[f"{fg_role}_on_{bg_role}"] = a
            except curses.error:
                attr_map[f"{fg_role}_on_{bg_role}"] = curses.A_NORMAL
            pair_id += 1

        # Aliases.
        attr_map.setdefault("header", attr_map.get("dim", curses.A_DIM))
        attr_map["selected"] = attr_map.get(
            "bright_on_sel_bg", curses.A_REVERSE | curses.A_BOLD,
        )
        attr_map["sel_row"] = attr_map["selected"]
        attr_map["update"] = attr_map.get("bright", curses.A_NORMAL)
        attr_map["timestamp"] = attr_map.get("cyan", curses.A_DIM)
        attr_map["reporter"] = attr_map.get("magenta", curses.A_BOLD)
        attr_map["error"] = attr_map.get("red", curses.A_BOLD)
        attr_map["warn"] = attr_map.get("amber", curses.A_BOLD)
        attr_map["ok"] = attr_map.get("green", curses.A_NORMAL)
        attr_map["active"] = attr_map.get("red", curses.A_BOLD)

        state_holder["attr"] = attr_map
    except curses.error:
        return False
    return True


def _on_bg(role: str, bg: str, holder: dict) -> int:
    """Pick a bg-aware attribute, falling back to the plain fg role."""
    m = holder.get("attr") or {}
    return m.get(f"{role}_on_{bg}", m.get(role, curses.A_NORMAL))


def _attr(name: str, holder: dict) -> int:
    """Look up a role's curses attribute (no color → bold/dim/reverse fallback)."""
    m = holder.get("attr") or {}
    if name in m:
        return m[name]
    fb = {
        "header": curses.A_REVERSE,
        "dim": curses.A_DIM,
        "dimmer": curses.A_DIM,
        "faint": curses.A_DIM,
        "active": curses.A_BOLD,
        "error": curses.A_BOLD,
        "warn": curses.A_BOLD,
        "ok": curses.A_NORMAL,
        "bright": curses.A_BOLD,
        "selected": curses.A_REVERSE,
        "update": curses.A_BOLD,
        "reporter": curses.A_BOLD,
        "timestamp": curses.A_DIM,
        "red": curses.A_BOLD,
        "amber": curses.A_BOLD,
        "green": curses.A_NORMAL,
        "cyan": curses.A_DIM,
        "nav": curses.A_DIM,
        "magenta": curses.A_BOLD,
        "text": curses.A_NORMAL,
        "chip_air": curses.A_REVERSE,
        "chip_res": curses.A_REVERSE,
        "live": curses.A_REVERSE | curses.A_BOLD,
        "new_chip": curses.A_REVERSE | curses.A_BOLD,
        "sel_row": curses.A_REVERSE,
    }
    return fb.get(name, curses.A_NORMAL)


_PYTE_NAMED_COLOR = {
    "black":    0,
    "red":      1,
    "green":    2,
    "brown":    3, "yellow":  3,
    "blue":     4,
    "magenta":  5,
    "cyan":     6,
    "white":    7,
}
# (fg_idx, bg_idx) → curses pair_id; populated lazily.
_EMBED_PAIR_CACHE: dict[tuple[int, int], int] = {}
_EMBED_PAIR_NEXT = [128]  # start above all our pre-allocated pairs


def _pyte_color_index(color: Any) -> int:
    """Map a pyte cell color (`'default'`, `'red'`, `'ff6a5f'`, etc.) to a
    curses fg/bg index in the 256-color cube. Returns -1 for 'default'.
    """
    if color is None or color == "default":
        return -1
    if isinstance(color, str):
        s = color.lower()
        if s in _PYTE_NAMED_COLOR:
            return _PYTE_NAMED_COLOR[s]
        # Hex rrggbb (pyte normalises to lowercase 6-char hex).
        if len(s) == 6 and all(c in "0123456789abcdef" for c in s):
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
            # 6×6×6 color cube (16..231) — choose nearest step.
            rs = min(5, (r * 5 + 127) // 255)
            gs = min(5, (g * 5 + 127) // 255)
            bs = min(5, (b * 5 + 127) // 255)
            return 16 + 36 * rs + 6 * gs + bs
        # Could also be an int as string for indexed 256-color SGR.
        try:
            return max(-1, min(255, int(s)))
        except ValueError:
            return -1
    if isinstance(color, int):
        return max(-1, min(255, color))
    return -1


def _embed_pair_attr(fg_idx: int, bg_idx: int) -> int:
    """Get-or-allocate a curses color pair for this (fg, bg) combo."""
    key = (fg_idx, bg_idx)
    pair_id = _EMBED_PAIR_CACHE.get(key)
    if pair_id is None:
        if _EMBED_PAIR_NEXT[0] >= getattr(curses, "COLOR_PAIRS", 256):
            return 0
        pair_id = _EMBED_PAIR_NEXT[0]
        _EMBED_PAIR_NEXT[0] += 1
        try:
            curses.init_pair(pair_id, fg_idx, bg_idx)
        except curses.error:
            return 0
        _EMBED_PAIR_CACHE[key] = pair_id
    try:
        return curses.color_pair(pair_id)
    except curses.error:
        return 0
