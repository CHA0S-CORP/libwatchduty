"""Detail pane: title banner, KV block, camera frame, tab bar + tab panels."""

from __future__ import annotations

import curses
import shutil
import sys
import textwrap
from math import cos, radians, sin
from typing import Any

from .. import aircraft as _aircraft
from .derive import _set_status
from .draw_list import _draw_chip, _draw_containment_cell
from .helpers import (
    _bearing_arrow,
    _bearing_compass,
    _format_age,
    _initial_bearing,
    _is_planned,
    _seconds_since_iso,
    _sparkline,
    _split_html_lines,
    _strip_html,
    _threat_bar_glyphs,
    _threat_tier,
    _wrap_around_image,
)
from .images_paint import _nearby_cameras
from .layout import _Layout, _addnstr
from .mapscii_embed import _MapsciiEmbed, _bundled_mapscii
from .palette import _attr
from .state import (
    _CHIP_CACHE_MAX,
    _DETAIL_MAX_CONTENT,
    _IMG_SIZE_PRESETS,
    _REPORTS_RENDER_LIMIT,
    _TABS,
    _TuiState,
)


# ---------------------------------------------------------------------------
# detail pane
# ---------------------------------------------------------------------------

_DETAIL_PAD_X = 1


def _delta_str(history: list[tuple[float, float]]) -> tuple[str, str, str]:
    """Return ``(delta_arrow_text, role, parens_history_str)``."""
    if not history or len(history) < 2:
        return "", "dimmer", ""
    first_t, first_v = history[0]
    last_t, last_v = history[-1]
    delta = last_v - first_v
    age = max(1, int(last_t - first_t))
    age_s = _format_age(age)
    if abs(delta) < 1e-6:
        return f"= 0 / {age_s}", "dimmer", _parens(history)
    if delta > 0:
        sign = "▲"
        pct = (delta / max(1e-3, first_v)) * 100.0 if first_v > 0 else 0.0
        if first_v > 0 and pct >= 50:
            txt = f"{sign} +{pct:.0f}% / {age_s}"
        else:
            txt = f"{sign} +{delta:.1f} / {age_s}"
        role = "red"
    else:
        sign = "▼"
        txt = f"{sign} {delta:.1f} / {age_s}"
        role = "red" if first_v - last_v > 0.5 else "dimmer"
    return txt, role, _parens(history)


def _parens(history: list[tuple[float, float]]) -> str:
    """`(2.5→3→5→11)` from a history list, last few values."""
    if not history:
        return ""
    vs = [v for _, v in history][-4:]
    parts = [f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{int(v)}"
             for v in vs]
    return "(" + "→".join(parts) + ")"


def _draw_detail_title(
    stdscr, e: dict, x0: int, y: int, width: int, holder: dict,
) -> int:
    """Big banner title block: subheader · ▔ over · NAME · ▁ under · URL."""
    eid = e.get("id")
    sub = f"#{eid}" if eid is not None else "#?"
    _addnstr(stdscr, y, x0, sub, min(len(sub), width),
             _attr("dimmer", holder))
    y += 1

    # Overline + underline rows make the name read "tall" without needing
    # a bitmap font. Box rules are 1 cell tall but visually frame the row.
    name = (e.get("name") or "(unnamed)").upper()
    name_attr = _attr("red", holder) | curses.A_BOLD
    rule_attr = _attr("red", holder) | curses.A_BOLD
    indent = "  "
    title_text = f"{indent}{name}"
    arrow = " ↗"
    rule_len = min(width, max(len(title_text) + len(arrow) + 2, 24))

    # Top overline.
    _addnstr(stdscr, y, x0, "▄" * rule_len, rule_len, rule_attr)
    y += 1
    # Name row.
    _addnstr(stdscr, y, x0, " " * rule_len, rule_len, name_attr)
    _addnstr(stdscr, y, x0 + len(indent), name,
             min(len(name), max(0, rule_len - len(indent) - len(arrow))),
             name_attr)
    arrow_x = x0 + len(indent) + len(name) + 1
    if arrow_x < x0 + rule_len:
        _addnstr(stdscr, y, arrow_x, "↗", 1,
                 _attr("cyan", holder) | curses.A_BOLD)
    y += 1
    # Bottom underline.
    _addnstr(stdscr, y, x0, "▀" * rule_len, rule_len, rule_attr)
    y += 1

    # URL row + spacer for breathing.
    url = f"https://app.watchduty.org/i/{eid}" if eid is not None else ""
    if url:
        _addnstr(stdscr, y, x0, url, min(len(url), width),
                 _attr("cyan", holder) | curses.A_UNDERLINE)
    y += 1
    y += 1
    return y


def _draw_threat_breakdown(
    stdscr, y: int, x0: int, width: int,
    factors: dict, holder: dict,
) -> None:
    """Render the inline breakdown `proximity 0.62 × size 0.30 …`."""
    pieces = [
        ("proximity", factors.get("proximity", 0.0), False),
        ("size",      factors.get("size", 0.0), False),
        ("uncontained", factors.get("uncontained", 0.0),
         factors.get("uncontained", 0.0) >= 0.9),
        ("growth",    factors.get("growth", 1.0),
         factors.get("growth", 1.0) > 1.05),
        ("wind",      factors.get("wind", 1.0),
         factors.get("wind", 1.0) > 1.05),
        ("bearing",   factors.get("bearing", 1.0),
         factors.get("bearing", 1.0) > 1.05),
    ]
    cursor = x0
    sep = " × "
    sep_role = "faint"
    for i, (label, val, hot) in enumerate(pieces):
        if cursor >= x0 + width:
            break
        if i > 0:
            n = min(len(sep), x0 + width - cursor)
            _addnstr(stdscr, y, cursor, sep, n, _attr(sep_role, holder))
            cursor += n
        if cursor >= x0 + width:
            break
        text = f"{label} {val:.2f}"
        if label in ("growth", "wind", "bearing"):
            text = f"{label} {val:.1f}×"
        n = min(len(text), x0 + width - cursor)
        role = "red" if hot else "faint"
        _addnstr(stdscr, y, cursor, text, n, _attr(role, holder))
        cursor += n


def _draw_kv_block(
    stdscr, state: _TuiState, e: dict,
    x0: int, y: int, width: int, body_bot: int, holder: dict,
) -> int:
    """KV grid: label (12 cols) + value (rest). Returns next y."""
    eid = e.get("id")
    eid_i = int(eid) if eid is not None else -1
    d = e.get("data") or {}
    label_w = 12

    def label(text: str) -> None:
        _addnstr(stdscr, y, x0, (text + ":").ljust(label_w),
                 min(label_w, width), _attr("dimmer", holder))

    def value_x() -> int:
        return x0 + label_w

    def value_w() -> int:
        return max(0, width - label_w)

    # threat (score + bar + breakdown next line)
    factors = state.threat_factors.get(eid_i)
    if factors is not None and y < body_bot:
        label("threat")
        score = factors["score"]
        tier = "dimmer" if factors.get("planned") else _threat_tier(score)
        bar = _threat_bar_glyphs(0.0 if factors.get("planned") else score)
        vtxt = f"{int(round(score))} "
        _addnstr(stdscr, y, value_x(), vtxt,
                 min(len(vtxt), value_w()),
                 _attr(tier, holder) | curses.A_BOLD)
        _addnstr(stdscr, y, value_x() + len(vtxt), bar,
                 max(0, value_w() - len(vtxt)),
                 _attr(tier, holder))
        y += 1
        if y < body_bot:
            _addnstr(stdscr, y, x0, "".ljust(label_w),
                     min(label_w, width), _attr("dimmer", holder))
            _draw_threat_breakdown(
                stdscr, y, value_x(), value_w(), factors, holder,
            )
            y += 1

    # spacer
    if y < body_bot:
        y += 1

    # address
    addr = e.get("address")
    if addr and y < body_bot:
        label("address")
        _addnstr(stdscr, y, value_x(), str(addr),
                 value_w(), _attr("text", holder))
        y += 1

    # coords
    lat, lng = e.get("lat"), e.get("lng")
    if lat is not None and lng is not None and y < body_bot:
        label("coords")
        _addnstr(stdscr, y, value_x(),
                 f"{float(lat):.4f}, {float(lng):.4f}",
                 value_w(), _attr("text", holder))
        y += 1

    # bearing
    if (
        state.near is not None
        and isinstance(lat, (int, float))
        and isinstance(lng, (int, float))
        and y < body_bot
    ):
        brg = _initial_bearing(state.near, (float(lat), float(lng)))
        arrow = _bearing_arrow(brg)
        compass = _bearing_compass(brg)
        d_km = state.distances.get(eid_i)
        label("bearing")
        line = f"{arrow} {compass}"
        _addnstr(stdscr, y, value_x(), line,
                 value_w(), _attr("cyan", holder))
        if d_km is not None:
            suf = f"  · {d_km:.1f} km from you"
            _addnstr(stdscr, y, value_x() + len(line), suf,
                     max(0, value_w() - len(line)),
                     _attr("dimmer", holder))
        y += 1

    # spacer before metrics group
    if y < body_bot:
        y += 1

    # wind (if known)
    wind = state.wind.get(eid_i)
    if wind and y < body_bot:
        label("wind")
        wb = wind.get("bearing")
        speed = wind.get("speed") or wind.get("mph") or 0
        gust = wind.get("gust")
        compass = _bearing_compass(float(wb)) if wb is not None else ""
        arrow = _bearing_arrow(float(wb)) if wb is not None else ""
        line = f"{arrow} {int(speed)} mph {compass}"
        if gust is not None:
            line += f", gusts {int(gust)}"
        _addnstr(stdscr, y, value_x(), line,
                 value_w(), _attr("amber", holder) | curses.A_BOLD)
        note = wind.get("note")
        if note:
            suf = f"  · {note}"
            _addnstr(stdscr, y, value_x() + len(line), suf,
                     max(0, value_w() - len(line)),
                     _attr("dimmer", holder))
        y += 1

    # distance (with sparkline)
    if eid_i in state.distances and y < body_bot:
        label("distance")
        d_km = state.distances[eid_i]
        hist = state.distance_history.get(eid_i) or []
        spark = _sparkline([v for _, v in hist], width=6)
        delta, drole, parens = _delta_str(hist)
        cursor = value_x()
        val = f"{d_km:.1f} km"
        _addnstr(stdscr, y, cursor, val,
                 min(len(val), value_w()),
                 _attr("bright", holder) | curses.A_BOLD)
        cursor += len(val) + 2
        if spark and cursor < value_x() + value_w():
            _addnstr(stdscr, y, cursor, spark,
                     min(len(spark), value_x() + value_w() - cursor),
                     _attr("amber", holder))
            cursor += len(spark) + 2
        if delta and cursor < value_x() + value_w():
            _addnstr(stdscr, y, cursor, delta,
                     min(len(delta), value_x() + value_w() - cursor),
                     _attr(drole, holder) | curses.A_BOLD)
            cursor += len(delta) + 2
        if parens and cursor < value_x() + value_w():
            _addnstr(stdscr, y, cursor, "closing " + parens
                     if hist and hist[-1][1] < hist[0][1] else parens,
                     max(0, value_x() + value_w() - cursor),
                     _attr("dimmer", holder))
        y += 1

    # acreage (with sparkline)
    acres = d.get("acreage")
    if y < body_bot:
        label("acreage")
        if acres is not None:
            val = f"{int(float(acres))} ac"
        else:
            val = "— ac"
        hist = state.acreage_history.get(eid_i) or []
        spark = _sparkline([v for _, v in hist], width=6)
        delta, drole, parens = _delta_str(hist)
        cursor = value_x()
        _addnstr(stdscr, y, cursor, val,
                 min(len(val), value_w()),
                 _attr("amber", holder) | curses.A_BOLD)
        cursor += len(val) + 2
        if spark and cursor < value_x() + value_w():
            _addnstr(stdscr, y, cursor, spark,
                     min(len(spark), value_x() + value_w() - cursor),
                     _attr("amber", holder))
            cursor += len(spark) + 2
        if delta and cursor < value_x() + value_w():
            _addnstr(stdscr, y, cursor, delta,
                     min(len(delta), value_x() + value_w() - cursor),
                     _attr(drole, holder) | curses.A_BOLD)
            cursor += len(delta) + 2
        if parens and cursor < value_x() + value_w():
            _addnstr(stdscr, y, cursor, parens,
                     max(0, value_x() + value_w() - cursor),
                     _attr("dimmer", holder))
        y += 1

    # spacer before status group
    if y < body_bot:
        y += 1

    # containment
    cont = d.get("containment")
    if y < body_bot:
        label("containment")
        cont_f = float(cont) if isinstance(cont, (int, float)) else None
        planned = _is_planned(e)
        used = _draw_containment_cell(
            stdscr, y, value_x(),
            max(0, min(20, value_w())),
            cont_f, holder, planned=planned,
        )
        # Trailing description.
        cursor = value_x() + used
        if not planned:
            tail = " — uncontained" if (cont_f is None or cont_f == 0) else ""
            if tail and cursor < value_x() + value_w():
                _addnstr(stdscr, y, cursor, tail,
                         max(0, value_x() + value_w() - cursor),
                         _attr("dimmer", holder))
        y += 1

    # modified
    if e.get("date_modified") and y < body_bot:
        label("modified")
        ts = str(e["date_modified"])[:19]
        _addnstr(stdscr, y, value_x(), ts,
                 min(len(ts), value_w()),
                 _attr("text", holder))
        age = _format_age(_seconds_since_iso(e["date_modified"]))
        suf = f"  · {age} ago"
        if value_w() > len(ts):
            _addnstr(stdscr, y, value_x() + len(ts), suf,
                     value_w() - len(ts),
                     _attr("dimmer", holder))
        y += 1

    # status (pill)
    if y < body_bot:
        label("status")
        is_active = bool(e.get("is_active"))
        if is_active:
            pill = " ACTIVE "
            attr = _attr("chip_res", holder)
        else:
            pill = " inactive "
            attr = _attr("dimmer", holder) | curses.A_REVERSE
        _addnstr(stdscr, y, value_x(), pill,
                 min(len(pill), value_w()), attr)
        y += 1

    # spacer before resources
    if y < body_bot:
        y += 1

    # resources
    if eid_i >= 0 and y < body_bot:
        radio = state.radio_cache.get(eid_i)
        cams = state.cameras_cache.get(eid_i)
        fps = state.fps_cache.get(eid_i)
        reports = state.reports_cache.get(eid_i) or []
        photo_count = sum(len(r.get("media") or []) for r in reports
                          if isinstance(r, dict))
        bits: list[tuple[str, str]] = []
        if radio is not None:
            bits.append((f"📻 {len(radio)} feeds", "cyan"))
        if cams is not None:
            near_cams = _nearby_cameras(cams, float(lat), float(lng)) \
                if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) \
                else cams
            bits.append((f"📷 {len(near_cams)} cams", "cyan"))
        if photo_count:
            bits.append((f"📸 {photo_count} photos", "cyan"))
        if fps:
            bits.append((f"🔥 {len(fps)} fps run", "red"))
        if bits:
            label("resources")
            cursor = value_x()
            for txt, role in bits:
                n = min(len(txt) + 2, value_x() + value_w() - cursor)
                if n <= 0:
                    break
                _addnstr(stdscr, y, cursor, txt + "  ", n,
                         _attr(role, holder))
                cursor += len(txt) + 2
            y += 1

    return y


def _draw_camera_frame(
    stdscr, state: _TuiState, e: dict, holder: dict,
    cam_x: int, cam_y: int, cam_w: int, cam_h: int,
) -> None:
    """Draw the live-camera frame container in the detail pane top-right.

    The actual image bytes are blitted post-paint by ``_paint_header_image``
    using the kitty/iTerm2 escape; this just paints the border + caption rows.
    """
    if cam_w < 12 or cam_h < 4:
        return
    border = _attr("dimmer", holder)
    _addnstr(stdscr, cam_y, cam_x,
             "┌" + "─" * (cam_w - 2) + "┐", cam_w, border)
    for ry in range(cam_y + 1, cam_y + cam_h - 1):
        _addnstr(stdscr, ry, cam_x, "│", 1, border)
        _addnstr(stdscr, ry, cam_x + cam_w - 1, "│", 1, border)
        # Diagonal hatch placeholder when no image is available.
        if not state.header_image_url or state.header_image_url not in state.image_cache:
            pat = ("╲" * (cam_w - 2))
            _addnstr(stdscr, ry, cam_x + 1, pat, cam_w - 2,
                     _attr("dimmer", holder))
    _addnstr(stdscr, cam_y + cam_h - 1, cam_x,
             "└" + "─" * (cam_w - 2) + "┘", cam_w, border)
    # Caption rows below the frame.
    cap_y = cam_y + cam_h
    if cap_y < stdscr.getmaxyx()[0]:
        url = state.header_image_url or ""
        tag = "▶ live cam"
        _addnstr(stdscr, cap_y, cam_x, tag, min(len(tag), cam_w),
                 _attr("cyan", holder))
        if cam_w > len(tag) + 2:
            sub = "  press i fullscreen · press c map"
            _addnstr(stdscr, cap_y, cam_x + len(tag), sub,
                     min(len(sub), cam_w - len(tag)),
                     _attr("dimmer", holder))
        if cap_y + 1 < stdscr.getmaxyx()[0] and url:
            short = url.rsplit("/", 1)[-1][:cam_w]
            _addnstr(stdscr, cap_y + 1, cam_x, short, cam_w,
                     _attr("dimmer", holder))


def _draw_tab_bar(
    stdscr, state: _TuiState, x0: int, y: int, width: int, holder: dict,
) -> int:
    """Draw the tab strip; populate ``state.tab_rects`` for mouse hits.

    Returns the y of the row immediately below the tab strip.
    """
    state.tab_rects = []
    cursor = x0
    counts = {
        "updates": len(state.reports_cache.get(
            int(state.visible_fires[state.selected_idx].get("id")), [])
        ) if state.visible_fires else 0,
        "radio": len(state.radio_cache.get(
            int(state.visible_fires[state.selected_idx].get("id")), [])
        ) if state.visible_fires else 0,
        "map": 0,
        "evac": _evac_count(state.visible_fires[state.selected_idx])
                if state.visible_fires else 0,
    }
    for tab in _TABS:
        label = tab.capitalize()
        count = counts.get(tab, 0)
        text = f"{label}"
        if count:
            text += f" ({count})"
        if tab == "updates" and state.live_mode:
            text += " ●"
        chunk = f"  {text}  "
        if cursor + len(chunk) > x0 + width:
            break
        active = (state.active_tab == tab)
        role = "cyan" if active else "dim"
        if tab == "evac":
            role = "red" if active else ("red" if count else "dim")
        attr = _attr(role, holder)
        if active:
            attr |= curses.A_BOLD
        _addnstr(stdscr, y, cursor, chunk, len(chunk), attr)
        if active:
            under = "─" * len(chunk)
            _addnstr(stdscr, y + 1, cursor, under, len(under),
                     _attr(role, holder) | curses.A_BOLD)
        else:
            _addnstr(stdscr, y + 1, cursor, "·" * len(chunk),
                     len(chunk), _attr("dimmer", holder))
        state.tab_rects.append((y, cursor, cursor + len(chunk), tab))
        cursor += len(chunk)
    # Fill remainder of underline row.
    if cursor < x0 + width:
        _addnstr(stdscr, y + 1, cursor, "·" * (x0 + width - cursor),
                 x0 + width - cursor, _attr("dimmer", holder))
    return y + 2


def _evac_count(fire: dict) -> int:
    """Number of populated evac fields on a fire (orders + warnings)."""
    d = fire.get("data") or {}
    n = 0
    for k in ("evacuation_orders", "evacuation_warnings",
              "evacuation_advisories"):
        if d.get(k):
            n += 1
    return n


def _camera_rect(
    layout: _Layout, title_h: int = 6,
) -> tuple[int, int, int, int] | None:
    """Camera-frame geometry in the detail pane's top-right corner.

    Single source of truth for the sizing math shared by
    :func:`_draw_detail` (frame + caption) and
    ``images_paint._paint_header_image`` (the post-doupdate byte blit).
    Dynamic scaling with two bounds:
      - width:  ≤ 55% of detail width (KV gets ≥ kv_min cols).
      - height: ≤ 50% of body height minus the title rows.
    Whichever bound is tighter wins; the other dimension follows the
    16:9 aspect (chars are ~2:1, so cam_h ≈ cam_w * 0.32).

    Returns ``(cam_y, cam_x, cam_h, cam_w)``, or ``None`` when the pane
    is too small for a frame.
    """
    width = layout.detail_w - _DETAIL_PAD_X
    body_h = layout.body_bot - layout.body_top
    tab_bar_h = 2
    content_min = max(8, body_h // 2)
    upper_max = max(6, body_h - content_min - tab_bar_h)
    upper_avail = max(0, upper_max - title_h)
    kv_min = 36
    aspect = 0.32
    cam_w = 0
    cam_h = 0
    if width >= 60 and upper_avail >= 4:
        w_cap = max(0, min(width - kv_min, int(width * 0.55)))
        h_cap = upper_avail
        # Try filling width: derived height.
        h_from_w = max(4, int(w_cap * aspect))
        if h_from_w <= h_cap:
            cam_w, cam_h = w_cap, h_from_w
        else:
            # Width-derived height overflows the 50% bound — start from height.
            cam_h = h_cap
            cam_w = min(w_cap, max(20, int(cam_h / aspect)))
    if cam_w < 20 or cam_h < 4:
        return None
    cam_x = layout.list_w + _DETAIL_PAD_X + (width - cam_w)
    cam_y = layout.body_top + title_h
    return (cam_y, cam_x, cam_h, cam_w)


def _draw_detail(stdscr, state: _TuiState, layout: _Layout, holder: dict) -> None:
    """Right-pane redraw entry point."""
    if not layout.show_detail or layout.detail_w <= 0:
        return
    top = layout.body_top
    bot = layout.body_bot
    x0 = layout.list_w + _DETAIL_PAD_X
    width = layout.detail_w - _DETAIL_PAD_X

    # Vertical separator.
    for y in range(top, bot):
        _addnstr(stdscr, y, layout.list_w, "│", 1, _attr("dimmer", holder))

    if not state.visible_fires or width <= 0:
        return

    e = state.visible_fires[state.selected_idx]

    # Reserve ≥50% of the detail height for the tab panel (Updates/etc.).
    # The upper region (title + KV + camera) is clamped to the remainder.
    body_h = bot - top
    tab_bar_h = 2
    content_min = max(8, body_h // 2)
    upper_max = max(6, body_h - content_min - tab_bar_h)

    y = top
    y = _draw_detail_title(stdscr, e, x0, y, width, holder)
    title_h = y - top

    # Camera frame in top-right; KV block on the left. The sizing math is
    # shared with the post-doupdate blit via :func:`_camera_rect`.
    upper_avail = max(0, upper_max - title_h)
    rect = _camera_rect(layout, title_h=title_h)
    if rect is not None:
        cam_y, cam_x, cam_h, cam_w = rect
    else:
        cam_y, cam_x, cam_h, cam_w = y, x0, 0, 0
    kv_w = width - cam_w - (2 if cam_w else 0)
    if cam_w:
        _draw_camera_frame(stdscr, state, e, holder,
                           cam_x, cam_y, cam_w, cam_h)

    # Hard ceiling for the KV block so it can't push the tab panel below 50%.
    kv_bot = top + title_h + upper_avail
    y_after_kv = _draw_kv_block(
        stdscr, state, e, x0, y, kv_w, kv_bot, holder,
    )
    next_y = max(
        y_after_kv,
        (cam_y + cam_h) if cam_h else y_after_kv,
    )
    if next_y >= bot - tab_bar_h - 4:
        next_y = bot - tab_bar_h - 4

    # Tab bar.
    next_y = _draw_tab_bar(stdscr, state, x0, next_y, width, holder)
    if next_y >= bot:
        return

    tab = state.active_tab
    if tab == "updates":
        _draw_updates_tab(stdscr, state, e, x0, next_y, width, bot, holder)
    elif tab == "radio":
        _draw_radio_tab(stdscr, state, e, x0, next_y, width, bot, holder)
    elif tab == "map":
        # Embedded mapscii inside the tab rectangle. Falls back to the
        # zero-dep quadrant when no mapscii binary is present.
        lat, lng = e.get("lat"), e.get("lng")
        ms = _bundled_mapscii() or shutil.which("mapscii")
        # Top-of-pane hint.
        if ms:
            hint = "mapscii (m fullscreen · r refresh tile)"
        else:
            hint = ("install mapscii: "
                    "`watchduty-install-mapscii`")
        _addnstr(stdscr, next_y, x0, hint, width,
                 _attr("dimmer", holder))
        rect_y0 = next_y + 1
        rect_h = max(4, bot - rect_y0)
        rect_w = max(20, width - 1)   # leave 1 col for the separator
        if (ms and isinstance(lat, (int, float))
                and isinstance(lng, (int, float))
                and rect_h >= 6):
            zoom = 13
            need_spawn = (
                state.mapscii_embed is None
                or not getattr(state.mapscii_embed, "alive", False)
                or not state.mapscii_embed.matches(
                    float(lat), float(lng), zoom)
            )
            if need_spawn:
                if state.mapscii_embed is not None:
                    state.mapscii_embed.close()
                state.mapscii_embed = _MapsciiEmbed(
                    ms, float(lat), float(lng), zoom,
                    rows=rect_h, cols=rect_w,
                )
                if state.mapscii_embed.unavailable:
                    _set_status(state,
                                state.mapscii_embed.unavailable,
                                is_error=True)
            # Always sync size with the current rect — handles terminal
            # resizes, compact-list toggles, and tab content shrinking
            # below mapscii after a KV-block expansion. The resize is a
            # no-op when dims didn't change.
            state.mapscii_embed.resize(rect_h, rect_w)
            state.mapscii_rect = (rect_y0, x0, rect_h, rect_w)

            # Drain + paint into curses cells (composes cleanly with the
            # rest of the dashboard; no need for post-doupdate blits).
            embed = state.mapscii_embed
            if embed.screen is not None:
                embed.poll()
                embed.paint(stdscr, rect_y0, x0, holder)
            else:
                # pyte unavailable — fall back to quadrant in the rect.
                _draw_map_tab(stdscr, state, e, x0, rect_y0,
                              width, bot, holder)
        else:
            # No mapscii or rect too small — quadrant fallback.
            if state.mapscii_embed is not None:
                state.mapscii_embed.close()
                state.mapscii_embed = None
                state.mapscii_rect = ()
            _draw_map_tab(stdscr, state, e, x0, rect_y0, width, bot, holder)
    elif tab == "evac":
        _draw_evac_tab(stdscr, state, e, x0, next_y, width, bot, holder)


# ---------------------------------------------------------------------------
# Updates tab
# ---------------------------------------------------------------------------

def _draw_scrollbar(
    stdscr, x: int, y0: int, h: int, scroll: int, total: int, holder: dict,
) -> None:
    """One-cell right-flush scrollbar."""
    if h <= 0 or total <= 0:
        return
    track = _attr("dimmer", holder)
    thumb = _attr("dim", holder) | curses.A_BOLD
    for ry in range(h):
        _addnstr(stdscr, y0 + ry, x, "│", 1, track)
    visible = min(h, total)
    thumb_h = max(1, int(h * visible / max(visible, total)))
    rng = max(1, total - visible)
    pos = int((scroll / rng) * (h - thumb_h)) if rng > 0 else 0
    for ry in range(thumb_h):
        _addnstr(stdscr, y0 + pos + ry, x, "█", 1, thumb)


def _draw_updates_tab(
    stdscr, state: _TuiState, fire: dict,
    x0: int, y: int, width: int, body_bot: int, holder: dict,
) -> None:
    """Wrapped update cards with right-flush scrollbar."""
    eid = fire.get("id")
    reports = state.reports_cache.get(int(eid)) if eid is not None else None
    if state.loading_reports_for == eid:
        _addnstr(stdscr, y, x0, "loading updates…", width,
                 _attr("dimmer", holder))
        return
    if reports is None:
        _addnstr(stdscr, y, x0, "(press Enter to load updates)",
                 width, _attr("dimmer", holder))
        return
    if not reports:
        _addnstr(stdscr, y, x0, "(no updates)", width,
                 _attr("dimmer", holder))
        return

    shown = reports[:_REPORTS_RENDER_LIMIT]
    pane_h = body_bot - y
    sb_x = x0 + width - 1
    # Cap wrap width per spec (max-width: 760px ~= 96 cells).
    content_w = min(_DETAIL_MAX_CONTENT, max(20, width - 2))
    body_indent = "│ "
    body_indent_w = len(body_indent)

    # Inline thumbnails — size from user preset (+ / - to cycle).
    preset_h, preset_w = _IMG_SIZE_PRESETS.get(
        state.image_size, _IMG_SIZE_PRESETS["med"],
    )
    img_slot_h = preset_h
    img_slot_w = min(preset_w, max(20, content_w - 24))
    narrow_body_w = max(16, content_w - body_indent_w - img_slot_w - 3)
    wide_body_w = max(20, content_w - body_indent_w)

    # Build wrapped lines for the entire feed first so the scrollbar is honest.
    rendered: list[tuple[str, Any, int, dict | None]] = []
    # tuple kinds:
    #   head · chips · body · body_img · body_narrow · img · img_pad · foot · spacer
    state.update_image_slots = []
    from .. import images as _img_mod
    inline_ok = _img_mod.supports_inline_images(sys.stdout)
    for r in shown:
        ts = r.get("date_created") or ""
        when = ts[:16].replace("T", " ")
        rel = _format_age(_seconds_since_iso(ts)) if ts else ""
        who = (r.get("user_created") or {}).get("display_name") or "?"
        msg = _strip_html(r.get("message") or "")
        rid = r.get("id")
        flash = isinstance(rid, int) and rid in state.flash_report_ids

        rendered.append(("head", (when, rel, who, flash), 0, r))

        chips = state.chip_cache.get(int(rid)) if isinstance(rid, int) else None
        if chips is None and isinstance(rid, int):
            raw = _aircraft.extract_chips(msg)
            chips = (_aircraft.enrich_chips(raw, state.aircraft_catalog)
                     if state.aircraft_catalog else raw)
            state.chip_cache[int(rid)] = chips
            # FIFO cap — dicts preserve insertion order, drop oldest.
            while len(state.chip_cache) > _CHIP_CACHE_MAX:
                state.chip_cache.pop(next(iter(state.chip_cache)))
        if chips:
            rendered.append(("chips", chips[:8], 0, r))

        media = r.get("media") or []
        first_url: str | None = None
        for m in media:
            if isinstance(m, dict):
                first_url = m.get("thumbnail_url") or m.get("url")
                if first_url:
                    break
        has_inline_img = bool(media) and inline_ok and first_url is not None

        if has_inline_img:
            # Body wraps to a narrow column for the first `img_slot_h` rows,
            # then returns to full width below the image.
            body_lines = _wrap_around_image(
                msg, narrow_body_w, wide_body_w, img_slot_h,
            )
            slot_payload = (img_slot_w, img_slot_h, first_url, len(media))
            for i, ln in enumerate(body_lines):
                if i == 0:
                    rendered.append(("body_img", (ln, slot_payload), 0, r))
                elif i < img_slot_h:
                    rendered.append(("body_narrow", ln, 0, r))
                else:
                    rendered.append(("body", ln, 0, r))
            # If body is shorter than the image slot, pad with blank rows so
            # the image has whitespace under it (no text bleed-through).
            shortfall = img_slot_h - min(len(body_lines), img_slot_h)
            for _ in range(shortfall):
                rendered.append(("img_pad", None, 0, r))
        else:
            for ln in textwrap.wrap(msg, width=wide_body_w,
                                    replace_whitespace=False,
                                    drop_whitespace=False) or [""]:
                rendered.append(("body", ln, 0, r))
            if media:
                rendered.append(("img", (len(media), first_url, False), 0, r))
            else:
                rendered.append(("foot", "", 0, r))
        # Trailing spacer between updates for visual breathing.
        rendered.append(("spacer", None, 0, r))

    total = len(rendered)
    state.detail_scroll_max = max(0, total - pane_h)
    if state.detail_scroll < 0:
        state.detail_scroll = 0
    if state.detail_scroll > state.detail_scroll_max:
        state.detail_scroll = state.detail_scroll_max

    # Paint visible window.
    for i in range(pane_h):
        idx = state.detail_scroll + i
        if idx >= total:
            break
        kind, payload, _, r = rendered[idx]
        ry = y + i
        if kind == "head":
            when, rel, who, flash = payload
            tsb = f"┌─ {when}"
            _addnstr(stdscr, ry, x0, tsb, min(len(tsb), content_w),
                     _attr("cyan", holder))
            rel_x = x0 + len(tsb) + 1
            if rel and rel_x < x0 + content_w:
                relb = f"{rel} ago"
                _addnstr(stdscr, ry, rel_x, relb,
                         min(len(relb), content_w - (rel_x - x0)),
                         _attr("dimmer", holder))
                rel_x += len(relb) + 1
            if rel_x < x0 + content_w:
                _addnstr(stdscr, ry, rel_x, who,
                         min(len(who), content_w - (rel_x - x0)),
                         _attr("magenta", holder) | curses.A_BOLD)
            if flash:
                chip = " NEW "
                cx = x0 + content_w - len(chip)
                if cx > rel_x:
                    _addnstr(stdscr, ry, cx, chip, len(chip),
                             _attr("new_chip", holder))
        elif kind == "chips":
            cursor = x0 + 2
            for chip in payload:
                label = chip.get("label", "?")
                hit = chip.get("catalog_hit")
                if hit and chip.get("kind") == "aircraft":
                    model = hit.get("model") or hit.get("type") or ""
                    if model:
                        label = f"{label} · {model[:14]}"
                role = "chip_air" if chip.get("kind") == "aircraft" else "chip_res"
                used = _draw_chip(stdscr, ry, cursor,
                                  max(0, x0 + content_w - cursor),
                                  label, role, holder)
                if used <= 0:
                    break
                cursor += used + 1
        elif kind == "body":
            _addnstr(stdscr, ry, x0, body_indent, body_indent_w,
                     _attr("dimmer", holder))
            _addnstr(stdscr, ry, x0 + body_indent_w, payload,
                     max(0, wide_body_w),
                     _attr("bright", holder))
        elif kind == "body_narrow":
            _addnstr(stdscr, ry, x0, body_indent, body_indent_w,
                     _attr("dimmer", holder))
            _addnstr(stdscr, ry, x0 + body_indent_w, payload,
                     max(0, narrow_body_w),
                     _attr("bright", holder))
        elif kind == "body_img":
            text, (slot_w, slot_h, url, count) = payload
            _addnstr(stdscr, ry, x0, body_indent, body_indent_w,
                     _attr("dimmer", holder))
            _addnstr(stdscr, ry, x0 + body_indent_w, text,
                     max(0, narrow_body_w),
                     _attr("bright", holder))
            rid_i = int(r.get("id")) if isinstance(r.get("id"), int) else -1
            slot_x = x0 + content_w - slot_w - 1
            if (ry + slot_h <= body_bot and slot_w >= 14 and rid_i >= 0):
                state.update_image_slots.append(
                    (rid_i, ry, slot_x, url, slot_w, slot_h),
                )
            if url and url not in state.image_cache and rid_i >= 0:
                state.update_image_pending.append((rid_i, url))
        elif kind == "img":
            count, first_url, inline_ok = payload
            tail = f"└ 📷 {count} image{'s' if count != 1 else ''}"
            _addnstr(stdscr, ry, x0, tail, min(len(tail), content_w),
                     _attr("dimmer", holder))
            if inline_ok and first_url:
                rid_i = int(r.get("id")) if isinstance(r.get("id"), int) else -1
                # Slot is anchored to the FIRST `img_pad` row below this one;
                # `img_slot_h` reserved rows guarantee no collision with the
                # next update header.
                slot_y = ry + 1
                if (slot_y + img_slot_h <= body_bot
                        and img_slot_w >= 12 and rid_i >= 0):
                    state.update_image_slots.append(
                        (rid_i, slot_y, x0 + 4, first_url,
                         img_slot_w, img_slot_h),
                    )
                if first_url not in state.image_cache and rid_i >= 0:
                    state.update_image_pending.append((rid_i, first_url))
        elif kind == "img_pad":
            # Reserved row under the thumbnail — keep cells blank so the
            # blit lands on whitespace, no text bleed-through.
            _addnstr(stdscr, ry, x0, " " * content_w, content_w, 0)
        elif kind == "spacer":
            _addnstr(stdscr, ry, x0, " " * content_w, content_w, 0)
        elif kind == "foot":
            _addnstr(stdscr, ry, x0, "└", 1, _attr("dimmer", holder))

    # Right-flush scrollbar.
    _draw_scrollbar(stdscr, sb_x, y, pane_h,
                    state.detail_scroll, total, holder)
    # Bottom-right scroll-position badge so the user can see scroll moving.
    if total > pane_h:
        badge = f" {state.detail_scroll}/{state.detail_scroll_max} "
        bx = sb_x - len(badge) - 1
        by = y + pane_h - 1
        if bx > x0:
            _addnstr(stdscr, by, bx, badge, len(badge),
                     _attr("dimmer", holder) | curses.A_REVERSE)


# ---------------------------------------------------------------------------
# Radio tab
# ---------------------------------------------------------------------------

def _draw_radio_tab(
    stdscr, state: _TuiState, fire: dict,
    x0: int, y: int, width: int, body_bot: int, holder: dict,
) -> None:
    """Broadcastify feeds with right-flush scrollbar (3 rows per feed)."""
    eid = fire.get("id")
    feeds = state.radio_cache.get(int(eid)) if eid is not None else None
    if feeds is None:
        _addnstr(stdscr, y, x0, "loading radio feeds…", width,
                 _attr("dimmer", holder))
        return
    if not feeds:
        _addnstr(stdscr, y, x0,
                 "(no scanner feeds near this fire)", width,
                 _attr("dimmer", holder))
        return

    pane_h = body_bot - y
    sb_x = x0 + width - 1
    content_w = max(20, width - 2)

    # Build a flat list of (kind, payload) tuples — one per output row —
    # so we can honor state.detail_scroll uniformly with the updates tab.
    rendered: list[tuple[str, Any]] = []
    for f in feeds:
        rendered.append(("head", f))
        rendered.append(("url", f))
        rendered.append(("spacer", None))

    total = len(rendered)
    state.detail_scroll_max = max(0, total - pane_h)
    if state.detail_scroll < 0:
        state.detail_scroll = 0
    if state.detail_scroll > state.detail_scroll_max:
        state.detail_scroll = state.detail_scroll_max

    for i in range(pane_h):
        idx = state.detail_scroll + i
        if idx >= total:
            break
        ry = y + i
        kind, payload = rendered[idx]
        if kind == "head":
            f = payload
            on = bool(f.get("online"))
            pill = "  ON  " if on else "  off  "
            pill_attr = _attr("live", holder) if on \
                else (_attr("dimmer", holder) | curses.A_REVERSE)
            _addnstr(stdscr, ry, x0, pill, len(pill), pill_attr)
            cx = x0 + len(pill) + 2
            fid = f.get("feed_id") or f.get("id") or "?"
            fid_s = f"{fid}"
            _addnstr(stdscr, ry, cx, fid_s,
                     min(len(fid_s), max(0, content_w - (cx - x0))),
                     _attr("dimmer", holder))
            cx += len(fid_s) + 3
            name = f.get("name") or "(unnamed)"
            name_room = max(0, content_w - (cx - x0) - 18)
            _addnstr(stdscr, ry, cx, name[:name_room], name_room,
                     _attr("bright" if on else "dim", holder) | curses.A_BOLD)
            listeners = f.get("listeners") or 0
            lstr = f"{listeners} listeners"
            lx = x0 + content_w - len(lstr) - 1
            if lx > cx:
                _addnstr(stdscr, ry, lx, lstr, len(lstr),
                         _attr("dimmer", holder))
        elif kind == "url":
            f = payload
            url = f.get("listen_url")
            on = bool(f.get("online"))
            if url:
                sub = f"    ▶ {url}"
                _addnstr(stdscr, ry, x0, sub, content_w,
                         _attr("cyan", holder) | curses.A_UNDERLINE)
            elif not on:
                last = f.get("last_heard") or "—"
                sub = f"    offline · last heard {last}"
                _addnstr(stdscr, ry, x0, sub, content_w,
                         _attr("dimmer", holder))
        elif kind == "spacer":
            _addnstr(stdscr, ry, x0, " " * content_w, content_w, 0)

    _draw_scrollbar(stdscr, sb_x, y, pane_h,
                    state.detail_scroll, total, holder)


# ---------------------------------------------------------------------------
# Map tab — quadrant/radar plot (zero-dep, default)
# ---------------------------------------------------------------------------

def _range_rings_km(within_km: float) -> tuple[float, float]:
    """Auto-scale rings from `--within`: (inner = within/2, outer = within)."""
    outer = max(1.0, float(within_km))
    inner = max(1.0, outer / 2.0)
    return (inner, outer)


def _draw_map_tab(
    stdscr, state: _TuiState, fire: dict,
    x0: int, y: int, width: int, body_bot: int, holder: dict,
) -> None:
    """Quadrant plot: user ◎ at centre, fires by bearing + clamped distance."""
    near = state.near
    if near is None:
        _addnstr(stdscr, y, x0,
                 "(set --near or :near to enable the map)", width,
                 _attr("dimmer", holder))
        return
    height = body_bot - y - 1
    if height < 8 or width < 30:
        _addnstr(stdscr, y, x0, "(detail pane too small for map)",
                 width, _attr("dimmer", holder))
        return

    plot_w = max(20, width - 24)  # leave ~22 cols for legend
    plot_h = height
    if plot_w < 20:
        plot_w = width
        legend_w = 0
    else:
        legend_w = width - plot_w - 1

    # Clear plot area.
    for ry in range(plot_h):
        _addnstr(stdscr, y + ry, x0, " " * plot_w, plot_w, 0)

    cx = x0 + plot_w // 2
    cy = y + plot_h // 2
    radius = min(plot_w // 2 - 1, plot_h // 2 - 1)
    if radius < 4:
        _addnstr(stdscr, y, x0, "(map too small)", width,
                 _attr("dimmer", holder))
        return

    inner_km, outer_km = _range_rings_km(state.within_km)
    max_km = outer_km

    # Two dashed range rings — scale with --within.
    for _r_km, ring_radius_frac in (
        (inner_km, 0.5),
        (outer_km, 1.0),
    ):
        ring_r = max(2, int(radius * ring_radius_frac))
        steps = max(36, int(2 * 3.14159 * ring_r * 2))
        for i in range(steps):
            ang = (2 * 3.14159 * i) / steps
            px = cx + int(round(ring_r * sin(ang) * 2.0))  # 2:1 cell aspect
            py = cy + int(round(ring_r * -cos(ang)))
            if i % 3 != 0:
                continue
            if x0 <= px < x0 + plot_w and y <= py < y + plot_h:
                _addnstr(stdscr, py, px, "·", 1,
                         _attr("dimmer", holder))

    # Crosshair.
    _addnstr(stdscr, cy, x0, "─" * plot_w, plot_w,
             _attr("dimmer", holder))
    for ry in range(y, y + plot_h):
        _addnstr(stdscr, ry, cx, "│", 1, _attr("dimmer", holder))

    # Compass labels.
    _addnstr(stdscr, y, cx, "N", 1,
             _attr("dim", holder) | curses.A_BOLD)
    _addnstr(stdscr, y + plot_h - 1, cx, "S", 1,
             _attr("dim", holder) | curses.A_BOLD)
    _addnstr(stdscr, cy, x0, "W", 1,
             _attr("dim", holder) | curses.A_BOLD)
    _addnstr(stdscr, cy, x0 + plot_w - 1, "E", 1,
             _attr("dim", holder) | curses.A_BOLD)

    # User ◎ in centre.
    _addnstr(stdscr, cy, cx, "◎", 1,
             _attr("green", holder) | curses.A_BOLD)

    # Plot fires.
    selected_id = fire.get("id")
    legend_rows: list[tuple[str, str, str]] = []
    for f in state.fires:
        fid = f.get("id")
        flat = f.get("lat")
        flng = f.get("lng")
        if fid is None or not isinstance(flat, (int, float)) \
                or not isinstance(flng, (int, float)):
            continue
        fid_i = int(fid)
        dist = state.distances.get(fid_i)
        if dist is None:
            continue
        brg = _initial_bearing(near, (float(flat), float(flng)))
        ratio = min(1.0, dist / max_km)
        px = cx + int(round(radius * ratio * sin(radians(brg)) * 2.0))
        py = cy + int(round(radius * ratio * -cos(radians(brg))))
        if not (x0 <= px < x0 + plot_w and y <= py < y + plot_h):
            continue
        score = state.threat_scores.get(fid_i, 0.0)
        tier = _threat_tier(score)
        if fid == selected_id:
            glyph = "◆"
            attr = _attr(tier, holder) | curses.A_BOLD | curses.A_REVERSE
        elif score >= 60:
            glyph = "▲"
            attr = _attr("red", holder) | curses.A_BOLD
        elif score >= 20:
            glyph = "●"
            attr = _attr("amber", holder)
        else:
            glyph = "○"
            attr = _attr("dim", holder)
        _addnstr(stdscr, py, px, glyph, 1, attr)
        if len(legend_rows) < plot_h - 1:
            legend_rows.append((
                glyph,
                f.get("name") or "(unnamed)",
                f"{_bearing_compass(brg)} · {dist:.0f}km",
            ))

    # Legend column on the right.
    if legend_w >= 16:
        lx = x0 + plot_w + 1
        _addnstr(stdscr, y, lx, "TARGETS", legend_w,
                 _attr("dimmer", holder))
        for i, (g, n, _sub) in enumerate(legend_rows[:plot_h - 1]):
            ry = y + 1 + i
            if ry >= body_bot:
                break
            _addnstr(stdscr, ry, lx, g, 1,
                     _attr("amber", holder) | curses.A_BOLD)
            name = n[:max(0, legend_w - 4)]
            _addnstr(stdscr, ry, lx + 2, name, legend_w - 2,
                     _attr("text", holder))
            if legend_w > len(name) + 4:
                _addnstr(stdscr, ry, lx + 2, name, legend_w - 2,
                         _attr("text", holder))
                # Second line removed for brevity; show sub inline if room.
                pass

    # Ring legend at bottom of plot.
    legend_y = y + plot_h - 1
    rings_legend = (
        f"  inner {int(inner_km)}km · outer {int(outer_km)}km  "
        f"· ◆ selected · ▲ high · ● med · ○ low · ◎ you"
    )
    _addnstr(stdscr, legend_y, x0, rings_legend, plot_w,
             _attr("dimmer", holder))


# ---------------------------------------------------------------------------
# Evac tab
# ---------------------------------------------------------------------------

def _draw_evac_tab(
    stdscr, state: _TuiState, fire: dict,
    x0: int, y: int, width: int, body_bot: int, holder: dict,
) -> None:
    """Evac orders + warnings — each zone on its own bulleted line."""
    d = fire.get("data") or {}
    sections = (
        ("⛔ EVACUATION ORDERS", "red", d.get("evacuation_orders")),
        ("⚠ EVACUATION WARNINGS", "amber", d.get("evacuation_warnings")),
        ("⚠ EVACUATION ADVISORIES", "amber", d.get("evacuation_advisories")),
    )
    bullet = "● "
    indent = "  "
    text_w = max(20, width - len(indent) - len(bullet))
    any_seen = False
    for title, role, body in sections:
        if not body:
            continue
        any_seen = True
        if y >= body_bot:
            return
        _addnstr(stdscr, y, x0, title, width,
                 _attr(role, holder) | curses.A_BOLD)
        y += 1
        for line in _split_html_lines(body):
            wrapped = textwrap.wrap(line, width=text_w) or [""]
            for j, w in enumerate(wrapped):
                if y >= body_bot:
                    return
                if j == 0:
                    _addnstr(stdscr, y, x0 + len(indent), bullet,
                             len(bullet),
                             _attr(role, holder) | curses.A_BOLD)
                    _addnstr(stdscr, y,
                             x0 + len(indent) + len(bullet), w,
                             max(0, width - len(indent) - len(bullet)),
                             _attr("text", holder))
                else:
                    _addnstr(stdscr, y,
                             x0 + len(indent) + len(bullet), w,
                             max(0, width - len(indent) - len(bullet)),
                             _attr("dim", holder))
                y += 1
            # spacer between zones
            if y < body_bot:
                y += 1
        y += 1
    if not any_seen:
        _addnstr(stdscr, y, x0,
                 "(no active evacuation orders or warnings)",
                 width, _attr("dimmer", holder))
    if y < body_bot - 1:
        _addnstr(stdscr, body_bot - 1, x0,
                 "source: watchduty.org · strip-html applied",
                 width, _attr("dimmer", holder))
